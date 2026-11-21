"""First-login onboarding: show the setup wizard until the user finishes or skips it."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.auth import current_user
from app.db import get_session
from app.jobs import ensure_user
from app.models import Document

router = APIRouter(prefix='/api/onboarding', tags=['onboarding'])


@router.get('')
def status(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    user = ensure_user(session, user_id)
    counts = dict(session.execute(select(Document.category, func.count()).where(Document.user_id == user_id)
                                  .group_by(Document.category)).all())
    if user.onboarded_at is None and counts:
        user.onboarded_at = datetime.now(timezone.utc)  # existing users with documents never see the wizard
    return {'completed': user.onboarded_at is not None,
            'documents': {'bank': counts.get('bank', 0), 'investment': counts.get('investment', 0)}}


@router.post('/complete')
def complete(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    user = ensure_user(session, user_id)
    user.onboarded_at = user.onboarded_at or datetime.now(timezone.utc)
    return {'completed': True}
