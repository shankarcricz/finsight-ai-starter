"""LangSmith tracing: what is sent, how runs nest, and what is masked.

The LangSmith client here talks to an in-memory HTTP session, so nothing leaves the machine.
"""
import json
import pytest
import langsmith as ls
from langchain_core.messages import AIMessage
from fastapi.testclient import TestClient
from langsmith import Client
from app import main, observability
from app.agent import make_agent
from app.config import settings
from app.main import app
from app.observability import anonymize
from tests.investment_fixtures import groww_holdings_xlsx
from tests.test_tools_agent import ScriptedModel


class _Resp:
    status_code, headers, text, ok = 200, {}, '{}', True

    def json(self):
        return {'id': '00000000-0000-0000-0000-000000000000'}

    def raise_for_status(self):
        pass


class _RecordingSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return _Resp()

    def mount(self, *a, **k):
        pass

    def close(self):
        pass


@pytest.fixture
def traces():
    http = _RecordingSession()
    client = Client(api_key='test', api_url='http://127.0.0.1:9', auto_batch_tracing=False,
                    anonymizer=anonymize, session=http)
    observability.configure(client)
    runs: dict[str, dict] = {}

    def collect(path='/runs'):
        client.flush()
        out = []
        for method, url, kw in http.calls:
            if url.split('127.0.0.1:9')[-1].startswith(path):
                body = json.loads(kw.get('data') or kw.get('json') or b'{}')
                if path == '/runs':
                    runs.setdefault(body['id'], {}).update({k: v for k, v in body.items() if v is not None})
                else:
                    out.append(body)
        return list(runs.values()) if path == '/runs' else out
    try:
        yield collect
    finally:
        observability.configure(None)  # back to disabled (no key in tests)


def by_name(runs, name):
    found = [r for r in runs if r.get('name') == name]
    assert found, f'no run named {name!r}; got {sorted({r.get("name") for r in runs})}'
    return found[0]


def meta(run):
    return (run.get('extra') or {}).get('metadata') or {}


# ---- masking -----------------------------------------------------------------------------------------------

def test_pii_is_masked_but_financial_figures_survive():
    text = ('mail a.b+x@gmail.com, upi shankar@okhdfcbank, PAN ABCDE1234F, phone +91 98765 43210 / 9876543210, '
            'aadhaar 1234 5678 9012, card 4111 1111 1111 1111, a/c 123456789012, BO 1200000000001234')
    out = anonymize({'t': text})['t']
    for secret in ('a.b+x@gmail.com', 'okhdfcbank', 'ABCDE1234F', '98765', '9876543210', '1234 5678 9012',
                   '4111', '123456789012', '1200000000001234'):
        assert secret not in out, secret
    assert {'<email>', '<pan>', '<phone>', '<aadhaar>', '<card>', '<number>'} <= set(
        w.strip(',./') for w in out.split())
    kept = 'INR 960000.00 on 2026-11-21, ISIN INE001A01036, a/c XXXX9876, 1234567890.50, ₹1,28,112.56, NIFTY 25800.6'
    assert anonymize({'t': kept})['t'] == kept


def test_tracing_is_off_without_a_key():
    assert settings.langsmith_api_key == '' and observability.configure() is False
    assert observability.enabled() is False and ls.utils.tracing_is_enabled() is False


# ---- flows -------------------------------------------------------------------------------------------------

def test_chat_trace_nests_agent_llm_and_tools_under_the_request(traces, monkeypatch):
    model = ScriptedModel(messages=iter([
        AIMessage(content='', tool_calls=[{'name': 'get_spending_summary', 'id': 'c1', 'args': {}}]),
        AIMessage(content='You spent INR 0.00. I will email a.b@gmail.com'),
    ]))
    monkeypatch.setattr(main, 'make_agent', lambda user_id: make_agent(user_id, model=model))
    with TestClient(app) as c:
        r = c.post('/api/chat', json={'question': 'What did I spend? My PAN is ABCDE1234F',
                                      'conversation_id': 'conv-1234-abcd'})
    assert r.status_code == 200
    trace = r.json()['trace']
    runs = traces()

    root = by_name(runs, 'POST /api/chat')
    chat = by_name(runs, 'assistant_chat')
    tool = by_name(runs, 'get_spending_summary')
    llm_runs = [x for x in runs if x.get('run_type') == 'llm']
    assert chat['id'] == trace['run_id'] and root['id'] == trace['trace_id']
    assert chat['parent_run_id'] == root['id']                       # nested across the threadpool boundary
    assert tool['trace_id'] == root['id'] and llm_runs and all(x['trace_id'] == root['id'] for x in llm_runs)
    assert meta(root)['user_id'] == settings.demo_user_id and meta(root)['status_code'] == 200
    assert meta(chat)['thread_id'] == 'conv-1234-abcd' and meta(chat)['user_id'] == settings.demo_user_id
    sent = json.dumps(runs)
    assert 'ABCDE1234F' not in sent and 'a.b@gmail.com' not in sent and '<pan>' in sent and '<email>' in sent


def test_upload_traces_metadata_only(traces):
    with TestClient(app) as c:
        r = c.post('/api/upload', data={'category': 'investment'},
                   files={'file': ('Test Person holdings.xlsx', groww_holdings_xlsx(), 'application/octet-stream')})
    assert r.json()['structured']['status'] == 'imported'
    runs = traces()
    ingest = by_name(runs, 'ingest_spreadsheet')
    parse = by_name(runs, 'import_investment_sheet')
    assert ingest['inputs']['file_type'] == '.xlsx' and ingest['inputs']['size_bytes'] > 1000
    assert ingest['outputs']['structured']['reconciliation_status'] == 'reconciled'
    assert parse['outputs']['rows_inserted'] == 2 and {c['status'] for c in parse['outputs']['checks']} == {'pass'}
    sent = json.dumps(runs)
    for leaked in ('ALPHA POWER', 'INE001A01036', '1003.3', 'Test Person', 'XXXX', '3003.6', 'UEsDB'):
        assert leaked not in sent, leaked                     # no rows, ISINs, amounts, names, accounts or bytes


def test_feedback_is_recorded_against_the_run_with_masking(traces):
    run_id = '6f1c2f0e-5a7b-4c1d-9e2f-3a4b5c6d7e8f'
    with TestClient(app) as c:
        assert c.get('/api/me').json()['feedback_enabled'] is True
        r = c.post('/api/feedback', json={'run_id': run_id, 'score': 0, 'comment': 'wrong, mail me x@y.com'})
    assert r.json() == {'recorded': True}
    fb = traces('/feedback')[0]
    assert fb['run_id'] == run_id and fb['key'] == 'user_rating' and fb['score'] == 0
    assert fb['comment'] == 'wrong, mail me <email>'


def test_feedback_without_tracing_is_a_no_op():
    with TestClient(app) as c:
        r = c.post('/api/feedback', json={'run_id': '6f1c2f0e-5a7b-4c1d-9e2f-3a4b5c6d7e8f', 'score': 1})
    assert r.json()['recorded'] is False


def test_polling_and_static_requests_are_not_traced_and_reads_can_be_switched_off(traces, monkeypatch):
    with TestClient(app) as c:
        c.get('/api/notifications'), c.get('/health'), c.get('/static/app.css'), c.get('/api/categories')
        c.get('/api/transactions')
        monkeypatch.setattr(settings, 'langsmith_trace_reads', False)
        c.get('/api/analytics/summary')
        c.put('/api/settings', json={'email_enabled': True, 'weekly_review_enabled': True,
                                     'price_alerts_enabled': True, 'price_move_threshold_pct': 5})
    names = {r.get('name') for r in traces() if r.get('parent_run_id') is None}
    assert names == {'GET /api/transactions', 'PUT /api/settings'}


def test_route_templates_group_requests_and_errors_are_flagged(traces):
    with TestClient(app) as c:
        c.get('/api/reviews/does-not-exist')
    root = by_name(traces(), 'GET /api/reviews/{review_id}')
    assert meta(root)['status_code'] == 404 and 'http_4xx' in root['tags']
    assert root['inputs']['path'] == '/api/reviews/does-not-exist'
