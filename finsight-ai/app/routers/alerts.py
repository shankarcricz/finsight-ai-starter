"""Live market data, portfolio reviews, notifications, and notification settings."""
import json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.advisor import live_portfolio, portfolio_performance, review_to_dict
from app import ratelimit
from app.analytics import to_jsonable
from app.auth import current_user
from app.config import settings
from app.db import get_session
from app.jobs import ensure_user, run_review_for_user
from app.market_data import MarketDataError, get_provider, quote
from app.models import Notification, NotificationDelivery, PortfolioReview
from app.notifications import deliver_pending, recipient_for, smtp_configured

router = APIRouter(prefix='/api', tags=['alerts'])
MANUAL_REVIEW_COOLDOWN = timedelta(minutes=10)


@router.get('/market/quote')
def market_quote(q: str = Query(min_length=1, max_length=60), user_id: str = ratelimit.QUOTE):
    try:
        return quote(q)
    except MarketDataError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/investments/live')
def live(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return to_jsonable(live_portfolio(session, user_id))


@router.get('/investments/performance')
def performance(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return to_jsonable(portfolio_performance(session, user_id))


# ---- reviews ----------------------------------------------------------------------------------

def _own_review(session, user_id, review_id) -> PortfolioReview:
    r = session.scalar(select(PortfolioReview).where(PortfolioReview.id == review_id,
                                                     PortfolioReview.user_id == user_id))
    if r is None:
        raise HTTPException(404, 'Review not found')
    return r


@router.get('/reviews/latest')
def latest_review(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    r = session.scalar(select(PortfolioReview).where(PortfolioReview.user_id == user_id,
                                                     PortfolioReview.status == 'ready')
                       .order_by(PortfolioReview.created_at.desc()).limit(1))
    return review_to_dict(r) if r else None


@router.get('/reviews/{review_id}')
def get_review(review_id: str, session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return review_to_dict(_own_review(session, user_id, review_id))


@router.post('/reviews/run')
def run_review(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    """Generate a review now (also used to test email). Rate limited: LLM + market calls cost money/quota."""
    last = session.scalar(select(func.max(PortfolioReview.created_at)).where(
        PortfolioReview.user_id == user_id, PortfolioReview.period_key.like('manual-%')))
    if last is not None:
        last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last < MANUAL_REVIEW_COOLDOWN:
            raise HTTPException(429, 'A review was generated a few minutes ago. Please wait 10 minutes.')
    ensure_user(session, user_id)
    key = 'manual-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    review = run_review_for_user(session, user_id, key)
    if review is None:
        raise HTTPException(400, 'No holdings to review yet. Import a holdings statement or add a holding first.')
    deliveries = deliver_pending(session)
    out = review_to_dict(review)
    out['email'] = deliveries
    return out


# ---- notifications ------------------------------------------------------------------------------

def _serialize(n: Notification, delivery: NotificationDelivery | None) -> dict:
    return {'id': n.id, 'kind': n.kind, 'title': n.title, 'body': n.body, 'created_at': n.created_at.isoformat(),
            'read': n.read_at is not None, 'payload': json.loads(n.payload_json or '{}'),
            'email_status': delivery.status if delivery else None,
            'email_note': delivery.last_error if delivery else None}


@router.get('/notifications')
def notifications(limit: int = Query(30, ge=1, le=100), session: Session = Depends(get_session),
                  user_id: str = Depends(current_user)):
    rows = session.execute(
        select(Notification, NotificationDelivery)
        .outerjoin(NotificationDelivery, NotificationDelivery.notification_id == Notification.id)
        .where(Notification.user_id == user_id).order_by(Notification.created_at.desc()).limit(limit)).all()
    items = [_serialize(n, d) for n, d in rows]
    return {'items': items, 'unread': sum(not i['read'] for i in items)}


@router.post('/notifications/{notification_id}/read')
def mark_read(notification_id: str, session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    n = session.scalar(select(Notification).where(Notification.id == notification_id,
                                                  Notification.user_id == user_id))
    if n is None:
        raise HTTPException(404, 'Notification not found')
    n.read_at = n.read_at or datetime.now(timezone.utc)
    return {'read': True}


@router.post('/notifications/read-all')
def mark_all_read(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    now = datetime.now(timezone.utc)
    for n in session.scalars(select(Notification).where(Notification.user_id == user_id,
                                                        Notification.read_at.is_(None))):
        n.read_at = now
    return {'read': True}


# ---- settings -------------------------------------------------------------------------------------

class SettingsIn(BaseModel):
    email_enabled: bool
    weekly_review_enabled: bool
    price_alerts_enabled: bool
    price_move_threshold_pct: float = Field(ge=1, le=25)


@router.get('/settings')
def get_settings(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    u = ensure_user(session, user_id)
    to, reason = recipient_for(u)
    return {'email_enabled': u.email_enabled, 'weekly_review_enabled': u.weekly_review_enabled,
            'price_alerts_enabled': u.price_alerts_enabled, 'price_move_threshold_pct': u.price_move_threshold_pct,
            'email_recipient': to, 'email_unavailable_reason': reason, 'smtp_configured': smtp_configured(),
            'scheduler_enabled': settings.enable_scheduler,
            'schedule': {'weekly_review': f'{settings.review_day.title()} {settings.review_hour:02d}:00 IST',
                         'price_check': 'Mon-Fri 16:15 IST'},
            'market_data_source': getattr(get_provider(), '__class__').__name__}


@router.put('/settings')
def put_settings(body: SettingsIn, session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    u = ensure_user(session, user_id)
    for k, v in body.model_dump().items():
        setattr(u, k, v)
    return {'saved': True}
