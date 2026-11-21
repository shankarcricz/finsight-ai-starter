"""Firebase Authentication for the API.

The browser signs in with the Firebase JS SDK and sends its ID token as `Authorization: Bearer <token>`.
We verify the token ourselves (signature against Google's rotating public certs, audience = project id,
issuer, expiry) and use the token's `sub` (Firebase uid) as user_id. No service-account key is needed.
"""
import logging
import re
import threading
import time
from fastapi import Depends, HTTPException, Request
from google.auth import jwt
from app.config import settings

log = logging.getLogger(__name__)
CERTS_URL = 'https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com'
_cache: dict = {'certs': {}, 'expires': 0.0}
_lock = threading.Lock()


def _fetch_certs(force: bool = False) -> dict[str, str]:
    with _lock:
        if not force and _cache['certs'] and time.time() < _cache['expires']:
            return _cache['certs']
        import requests
        resp = requests.get(CERTS_URL, timeout=10)
        resp.raise_for_status()
        m = re.search(r'max-age=(\d+)', resp.headers.get('Cache-Control', ''))
        _cache['certs'] = resp.json()
        _cache['expires'] = time.time() + (int(m.group(1)) if m else 3600)
        return _cache['certs']


def verify_firebase_token(token: str, project_id: str) -> dict:
    """Return verified claims or raise ValueError."""
    try:
        claims = jwt.decode(token, certs=_fetch_certs(), audience=project_id, clock_skew_in_seconds=10)
    except ValueError as exc:
        if 'not found' not in str(exc).lower():  # unknown key id -> Google rotated keys; refetch once
            raise
        claims = jwt.decode(token, certs=_fetch_certs(force=True), audience=project_id, clock_skew_in_seconds=10)
    if claims.get('iss') != f'https://securetoken.google.com/{project_id}':
        raise ValueError('Wrong token issuer')
    sub = claims.get('sub')
    if not isinstance(sub, str) or not 0 < len(sub) <= 128:
        raise ValueError('Token has no valid subject')
    return claims


def check_auth_config() -> None:
    if settings.auth_enabled:
        log.info('Firebase auth enabled: any verified Google account can sign in; each user sees only their data.')
    else:
        log.warning('Firebase auth disabled: running as single demo user "%s". Do not expose this server.',
                    settings.demo_user_id)


def current_user(request: Request) -> str:
    """FastAPI dependency returning the authenticated user id. Sign-in is open to any Google account with a
    verified email; that email is where the user's notifications go."""
    if not settings.auth_enabled:
        request.state.user_id = settings.demo_user_id
        return settings.demo_user_id
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        raise HTTPException(401, 'Sign in required', headers={'WWW-Authenticate': 'Bearer'})
    try:
        claims = verify_firebase_token(header.removeprefix('Bearer ').strip(), settings.firebase_project_id)
    except Exception:
        log.info('Rejected invalid Firebase token')  # never log the token itself
        raise HTTPException(401, 'Invalid or expired session', headers={'WWW-Authenticate': 'Bearer'})
    email = str(claims.get('email', '')).lower()
    if not email or not claims.get('email_verified'):
        # Google accounts are always verified; this blocks e.g. unverified email/password sign-ups.
        raise HTTPException(403, f'The email {email or "(none)"} is not verified. Sign in with Google.')
    request.state.claims = {'email': email, 'email_verified': True}
    request.state.user_id = claims['sub']  # picked up by request tracing
    return claims['sub']


def is_admin(request: Request) -> bool:
    if not settings.auth_enabled:
        return True  # single local user
    email = (getattr(request.state, 'claims', None) or {}).get('email')
    return bool(email) and email in settings.admin_email_set


def admin_user(request: Request, user_id: str = Depends(current_user)) -> str:
    """For writes to data shared by all users (e.g. FII/DII history)."""
    if not is_admin(request):
        raise HTTPException(403, 'Only administrators can change shared market data (ADMIN_EMAILS)')
    return user_id


CurrentUser = Depends(current_user)
