import pytest
from fastapi.testclient import TestClient
import app.categorization as categorization
import app.ingest as ingest
from app.config import settings
from app.main import app
from app.redact import main as redact_main, redact_line


@pytest.fixture
def indexed(monkeypatch):
    calls = []
    monkeypatch.setattr(ingest, 'index_in_pinecone', lambda chunks, ids: calls.append(len(chunks)))
    return calls


@pytest.fixture
def llm_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, 'llm_categorization', True)
    monkeypatch.setattr(categorization, 'openai_classifier', lambda items: calls.append(items) or {})
    return calls


def upload(client, data, local_only=None):
    form = {'category': 'bank'}
    if local_only is not None:
        form['local_only'] = 'true' if local_only else 'false'
    return client.post('/api/upload', data=form, files={'file': ('s.pdf', data, 'application/pdf')}).json()


def test_local_only_sends_nothing_to_pinecone_or_llm(indexed, llm_calls, demo_pdf_bytes):
    with TestClient(app) as client:
        body = upload(client, demo_pdf_bytes, local_only=True)
        assert body['indexed_in_rag'] is False and body['chunks'] == 0
        assert body['structured']['reconciliation_status'] == 'reconciled'
        assert body['structured']['transactions_inserted'] == 47
        assert indexed == [] and llm_calls == []
        # Structured analytics still work fully offline.
        assert client.get('/api/analytics/summary').json()['income'] == '960000.00'


def test_opting_in_later_indexes_exactly_once(indexed, demo_pdf_bytes):
    with TestClient(app) as client:
        upload(client, demo_pdf_bytes, local_only=True)
        later = upload(client, demo_pdf_bytes, local_only=False)
        assert later['duplicate'] is True and later['indexed_in_rag'] is True and later['chunks'] > 0
        again = upload(client, demo_pdf_bytes)
        assert again['chunks'] == 0 and len(indexed) == 1
        assert later['structured']['transactions_inserted'] == 47  # import not repeated


def test_normal_upload_uses_llm_only_for_unmatched_rows(indexed, llm_calls, demo_pdf_bytes):
    with TestClient(app) as client:
        upload(client, demo_pdf_bytes)
    assert len(indexed) == 1
    assert [i['description'] for i in llm_calls[0]] == ['LAPTOP REPAIR']


def test_redaction_masks_names_and_digits_but_keeps_layout():
    line = '  15/03/2026   UPI/402112345678/RAVI KUMAR/okhdfc      1,250.00            98,765.43'
    out = redact_line(line, salt='s')
    assert len(out) == len(line)                     # column positions preserved
    assert out.startswith('  15/03/2026   UPI/')     # date + rail vocabulary kept
    assert 'RAVI' not in out and 'KUMAR' not in out and 'okhdfc' not in out
    assert '402112345678' not in out and '1,250.00' not in out and '98,765.43' not in out
    assert out.count(',') == line.count(',') and out.count('.') == line.count('.')  # amount shape kept


def test_redaction_keeps_header_words():
    header = 'Date      Narration      Chq./Ref.No.   Value Dt   Withdrawal Amt.   Deposit Amt.   Closing Balance'
    assert redact_line(header, 's').replace('Dt', 'XX') == header.replace('Dt', 'XX')


def test_redact_cli_writes_diagnosis(tmp_path):
    from tests.conftest import FIXTURES
    out = tmp_path / 'r.txt'
    assert redact_main([str(FIXTURES / 'demo_statement_2026.pdf'), '-o', str(out)]) == 0
    text = out.read_text(encoding='utf-8')
    assert 'transactions extracted: 47' in text and 'reconciliation: reconciled' in text
    assert 'Alex' not in text and 'ACME' not in text and '632,900.00' not in text
