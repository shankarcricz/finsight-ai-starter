"""Firebase auth: real RS256 signature verification with a throwaway key (Google's cert fetch is mocked)."""
import time
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from google.auth import crypt, jwt
import app.auth as auth
import app.ingest as ingest
from app.config import settings
from app.main import app

PROJECT = 'finsight-test'
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
PUBLIC_PEM = _key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
_other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_PEM = _other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()


def token(uid='uid-alice', email='alice@example.com', verified=True, aud=PROJECT, iss=None, exp_in=3600,
          key=PRIVATE_PEM, kid='kid-1'):
    now = int(time.time())
    payload = {'iss': iss or f'https://securetoken.google.com/{PROJECT}', 'aud': aud, 'sub': uid, 'user_id': uid,
               'iat': now - 10, 'exp': now + exp_in, 'auth_time': now - 10, 'email': email, 'email_verified': verified}
    return jwt.encode(crypt.RSASigner.from_string(key, key_id=kid), payload).decode()


@pytest.fixture
def firebase(monkeypatch):
    monkeypatch.setattr(settings, 'firebase_project_id', PROJECT)
    monkeypatch.setattr(settings, 'admin_emails', 'Admin@Example.com')
    monkeypatch.setattr(auth, '_fetch_certs', lambda force=False: {'kid-1': PUBLIC_PEM})
    monkeypatch.setattr(ingest, 'index_in_pinecone', lambda chunks, ids: None)
    with TestClient(app) as client:
        yield client


def hdr(t):
    return {'Authorization': f'Bearer {t}'}


def test_valid_token_maps_to_firebase_uid(firebase):
    r = firebase.get('/api/me', headers=hdr(token()))
    assert r.status_code == 200
    assert r.json() == {'user_id': 'uid-alice', 'auth_enabled': True, 'is_admin': False, 'feedback_enabled': False}


def test_any_verified_google_account_can_sign_in_and_gets_emails_there(firebase):
    from app.db import session_scope
    from app.models import User
    from app.notifications import recipient_for
    r = firebase.get('/api/me', headers=hdr(token(uid='uid-stranger', email='Stranger@Gmail.com')))
    assert r.status_code == 200 and r.json()['user_id'] == 'uid-stranger'
    with session_scope() as s:  # notifications go to the address Google verified for this account
        assert recipient_for(s.get(User, 'uid-stranger')) == ('stranger@gmail.com', None)


def test_only_admins_can_change_shared_market_data(firebase):
    csv = {'file': ('f.csv', b'Date,FII Net,DII Net\n19-11-2026,5,6\n', 'text/csv')}
    assert firebase.post('/api/markets/fii-dii/import', headers=hdr(token()), files=csv).status_code == 403
    admin = hdr(token(uid='uid-admin', email='admin@example.com'))  # case-insensitive match
    assert firebase.get('/api/me', headers=admin).json()['is_admin'] is True
    assert firebase.post('/api/markets/fii-dii/import', headers=admin, files=csv).json()['imported'] == 2


@pytest.mark.parametrize('headers,status', [
    ({}, 401),
    ({'Authorization': 'Basic abc'}, 401),
    (hdr('not-a-jwt'), 401),
    (hdr(token(key=OTHER_PEM)), 401),                                  # forged signature
    (hdr(token(aud='someone-elses-project')), 401),
    (hdr(token(iss='https://evil.example.com')), 401),
    (hdr(token(exp_in=-120)), 401),                                    # expired
    (hdr(token(verified=False)), 403),                                 # unverified email
    (hdr(token(email='')), 403),                                       # no email to notify
])
def test_rejected_requests(firebase, headers, status):
    assert firebase.get('/api/analytics/summary', headers=headers).status_code == status


def test_public_endpoints_stay_public(firebase):
    assert firebase.get('/health').status_code == 200
    cfg = firebase.get('/api/auth/config').json()
    assert cfg['enabled'] is True and cfg['firebase']['projectId'] == PROJECT


def test_users_cannot_see_each_others_data(firebase, demo_pdf_bytes):
    r = firebase.post('/api/upload', headers=hdr(token()), data={'category': 'bank'},
                      files={'file': ('s.pdf', demo_pdf_bytes, 'application/pdf')})
    assert r.json()['structured']['transactions_inserted'] == 47
    alice = firebase.get('/api/analytics/summary', headers=hdr(token())).json()
    bob = firebase.get('/api/analytics/summary', headers=hdr(token(uid='uid-bob', email='bob@example.com'))).json()
    assert alice['income'] == '960000.00' and bob['income'] == '0.00'
    assert firebase.get('/api/transactions', headers=hdr(token(uid='uid-bob', email='bob@example.com'))).json()['total_matching'] == 0
    tid = firebase.get('/api/transactions?limit=1', headers=hdr(token())).json()['transactions'][0]['id']
    r = firebase.patch(f'/api/transactions/{tid}', json={'category': 'Food'},
                       headers=hdr(token(uid='uid-bob', email='bob@example.com')))
    assert r.status_code == 404  # Bob cannot even confirm Alice's transaction exists


def test_unknown_key_id_triggers_one_cert_refresh(monkeypatch):
    calls = []

    def certs(force=False):
        calls.append(force)
        return {'kid-2': PUBLIC_PEM} if force else {'kid-1': PUBLIC_PEM}
    monkeypatch.setattr(auth, '_fetch_certs', certs)
    claims = auth.verify_firebase_token(token(kid='kid-2'), PROJECT)  # Google rotated keys
    assert claims['sub'] == 'uid-alice' and calls == [False, True]


def test_auth_enabled_starts_without_any_allowlist(monkeypatch):
    monkeypatch.setattr(settings, 'firebase_project_id', PROJECT)
    auth.check_auth_config()  # open sign-in is a valid configuration


def test_dev_mode_uses_demo_user():
    with TestClient(app) as client:
        me = client.get('/api/me').json()
        assert (me['user_id'], me['auth_enabled'], me['is_admin']) == (settings.demo_user_id, False, True)
        assert client.get('/api/auth/config').json() == {'enabled': False}
