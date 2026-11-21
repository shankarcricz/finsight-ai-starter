"""Background jobs (APScheduler in the web process for local dev).

  weekly_reviews       Saturdays 09:00 IST: AI portfolio review -> notification + email
  daily_price_check    Mon-Fri 16:15 IST (after NSE close): alert on holdings moving >= threshold
  deliver_emails       every 30 min: send queued / retry failed emails

All jobs are idempotent (dedupe keys + unique constraints), so a restart or double run cannot spam.
NOTE: with several web workers each would run the scheduler; production needs one dedicated worker.
"""
import logging
from datetime import date, datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.advisor import ReviewWriter, generate_review, live_portfolio
from app.config import settings
from app.db import session_scope
from app.market_data import MarketDataProvider
from app.models import PortfolioReview, User
from app.notifications import deliver_pending, notify
from app.observability import only, traceable

log = logging.getLogger(__name__)


def ensure_user(session: Session, user_id: str, email: str | None = None, email_verified: bool = False) -> User:
    user = session.get(User, user_id)
    if user is None:
        user = User(id=user_id)
        session.add(user)
    if email:
        user.email, user.email_verified = email[:254], email_verified
    user.last_seen_at = datetime.now(timezone.utc)
    session.flush()
    return user


def _users(session: Session, flag) -> list[User]:
    if not settings.auth_enabled:
        ensure_user(session, settings.demo_user_id)
    return list(session.scalars(select(User).where(flag.is_(True))))


@traceable(name='review_for_user', process_inputs=only('user_id', 'period_key', 'email'),
           process_outputs=lambda r: {'status': r.status if r else 'nothing_to_review'})
def run_review_for_user(session: Session, user_id: str, period_key: str, *, writer: ReviewWriter | None = None,
                        provider: MarketDataProvider | None = None, email: bool = True) -> PortfolioReview | None:
    review = generate_review(session, user_id, period_key, writer=writer, provider=provider)
    if review is None or review.status != 'ready':
        return review
    import json
    report = json.loads(review.report_json)
    notify(session, user_id, kind='portfolio_review', title=report['headline'],
           body=report['summary'][:1000], dedupe_key=f'review:{period_key}',
           payload={'review_id': review.id}, email=email)
    return review


@traceable(name='job.weekly_reviews', process_inputs=only('today'), process_outputs=lambda n: {'reviews': n})
def weekly_reviews(today: date | None = None, *, writer: ReviewWriter | None = None,
                   provider: MarketDataProvider | None = None) -> int:
    year, week, _ = (today or date.today()).isocalendar()
    done = 0
    with session_scope() as session:
        users = _users(session, User.weekly_review_enabled)
        for user in users:
            try:
                with session.begin_nested():
                    if run_review_for_user(session, user.id, f'weekly-{year}-W{week:02d}', writer=writer,
                                           provider=provider):
                        done += 1
            except Exception:
                log.exception('Weekly review failed for one user')
        deliver_pending(session)
    return done


@traceable(name='job.daily_price_check', process_inputs=only('today'), process_outputs=lambda n: {'alerts': n})
def daily_price_check(today: date | None = None, *, provider: MarketDataProvider | None = None) -> int:
    today = today or date.today()
    created = 0
    with session_scope() as session:
        for user in _users(session, User.price_alerts_enabled):
            try:
                live = live_portfolio(session, user.id, provider)
            except Exception:
                log.exception('Price check failed for one user')
                continue
            for p in live['positions']:
                move = p.get('day_change_pct')
                if move is None or abs(move) < user.price_move_threshold_pct:
                    continue
                direction = 'up' if move > 0 else 'down'
                value = f' Your position is worth about INR {p["market_value"]}.' if p.get('market_value') else ''
                if notify(session, user.id, kind='price_move',
                          title=f'{p["symbol"]} is {direction} {abs(move):.1f}% today',
                          body=f'{p["name"]} moved {move:+.2f}% to INR {p["price"]} (delayed price).{value} '
                               f'Moves like this are often noise; check the news before acting.',
                          dedupe_key=f'move:{p["symbol"]}:{today.isoformat()}',
                          payload={'symbol': p['symbol'], 'day_change_pct': move, 'price': str(p['price'])},
                          email=True):
                    created += 1
        deliver_pending(session)
    return created


@traceable(name='job.deliver_emails', process_inputs=lambda i: {})
def deliver_emails() -> dict:
    with session_scope() as session:
        return deliver_pending(session)


@traceable(name='job.collect_fii_dii', process_inputs=lambda i: {})
def collect_fii_dii() -> dict:
    from app.markets import refresh_fii_dii
    with session_scope() as session:
        return refresh_fii_dii(session, force=True)


def register(scheduler) -> None:
    scheduler.add_job(weekly_reviews, 'cron', day_of_week=settings.review_day, hour=settings.review_hour,
                      minute=0, id='weekly_reviews', replace_existing=True, misfire_grace_time=6 * 3600)
    scheduler.add_job(daily_price_check, 'cron', day_of_week='mon-fri', hour=16, minute=15,
                      id='daily_price_check', replace_existing=True, misfire_grace_time=3600)
    scheduler.add_job(deliver_emails, 'interval', minutes=30, id='deliver_emails', replace_existing=True)
    scheduler.add_job(collect_fii_dii, 'cron', day_of_week='mon-fri', hour=19, minute=30, id='collect_fii_dii',
                      replace_existing=True, misfire_grace_time=6 * 3600)
