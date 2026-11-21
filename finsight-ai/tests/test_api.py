"""HTTP-level acceptance test with Pinecone mocked: upload -> extract -> reconcile -> SQL -> analytics."""
import pytest
from fastapi.testclient import TestClient
import app.ingest as ingest
from app.main import app


@pytest.fixture
def indexed(monkeypatch):
    calls = []
    monkeypatch.setattr(ingest, 'index_in_pinecone', lambda chunks, ids: calls.append((chunks, ids)))
    return calls


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def upload(client, data: bytes, category='bank', name='statement.pdf'):
    return client.post('/api/upload', data={'category': category},
                       files={'file': (name, data, 'application/pdf')})


def test_first_acceptance_flow(client, indexed, demo_pdf_bytes):
    r = upload(client, demo_pdf_bytes)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['duplicate'] is False and body['chunks'] > 0
    assert len(indexed) == 1  # RAG pipeline still indexes the document
    chunk_meta = indexed[0][0][0].metadata
    assert chunk_meta['document_id'] == body['document_id'] and chunk_meta['page'] == 1
    s = body['structured']
    assert (s['status'], s['reconciliation_status'], s['transactions_inserted']) == ('imported', 'reconciled', 47)

    summary = client.get('/api/analytics/summary').json()
    assert summary['income'] == '960000.00' and summary['spending'] == '267100.00'
    assert client.get('/api/analytics/monthly').json()['months'][0]['month'] == '2026-01'
    assert client.get('/api/analytics/categories').json()['categories'][0]['category'] == 'Rent'
    assert len(client.get('/api/analytics/recurring').json()['recurring']) == 2
    assert client.get('/api/statements').json()[0]['account'] == 'XXXX0042'


def test_reupload_is_not_reindexed_or_reimported(client, indexed, demo_pdf_bytes):
    upload(client, demo_pdf_bytes)
    r = upload(client, demo_pdf_bytes, name='renamed-copy.pdf')
    assert r.json()['duplicate'] is True
    assert r.json()['structured']['transactions_inserted'] == 47  # same import, returned again
    assert len(indexed) == 1
    assert client.get('/api/transactions?limit=1').json()['total_matching'] == 47


def test_manual_category_correction_applies_to_merchant(client, indexed, demo_pdf_bytes):
    upload(client, demo_pdf_bytes)
    txns = client.get('/api/transactions', params={'search': 'LAPTOP'}).json()['transactions']
    assert txns[0]['category'] == 'Other' and txns[0]['needs_review'] is True
    r = client.patch(f"/api/transactions/{txns[0]['id']}", json={'category': 'Shopping', 'apply_to_merchant': True})
    assert r.status_code == 200 and r.json()['updated'] == 1
    fixed = client.get('/api/transactions', params={'search': 'LAPTOP'}).json()['transactions'][0]
    assert (fixed['category'], fixed['category_source'], fixed['needs_review']) == ('Shopping', 'manual', False)
    cats = {c['category']: c['amount'] for c in client.get('/api/analytics/categories').json()['categories']}
    assert cats['Shopping'] == '17300.00' and 'Other' not in cats


def test_invalid_category_correction_rejected(client, indexed, demo_pdf_bytes):
    upload(client, demo_pdf_bytes)
    tid = client.get('/api/transactions?limit=1').json()['transactions'][0]['id']
    assert client.patch(f'/api/transactions/{tid}', json={'category': 'Crypto'}).status_code == 422
    assert client.patch('/api/transactions/does-not-exist', json={'category': 'Food'}).status_code == 404


def test_bank_statement_uploaded_as_investment_is_not_misparsed(client, indexed, demo_pdf_bytes):
    body = upload(client, demo_pdf_bytes, category='investment').json()
    assert body['structured']['status'] == 'unsupported_layout' and len(indexed) == 1
    assert client.get('/api/transactions').json()['total_matching'] == 0


@pytest.mark.parametrize('data,name,status', [
    (b'not a pdf', 'statement.pdf', 400),
    (b'%PDF-1.4 broken', 'statement.pdf', 400),
    (b'%PDF-1.4', 'statement.txt', 400),
])
def test_bad_uploads_rejected(client, indexed, data, name, status):
    assert upload(client, data, name=name).status_code == status
    assert indexed == []


def test_bad_query_params(client):
    assert client.get('/api/analytics/summary?start=2026-12-01&end=2026-01-01').status_code == 400
    assert client.get('/api/analytics/trends?month=2026-13').status_code == 422
    assert client.get('/health').json() == {'status': 'ok'}
