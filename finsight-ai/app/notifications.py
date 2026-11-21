"""In-app notifications + idempotent, retryable email delivery.

  notify()          creates a Notification once per (user, dedupe_key) and queues an email delivery
  deliver_pending() sends queued/failed emails (max 5 attempts); safe to run repeatedly
Emails go only to the user's *verified* sign-in email (or ALERT_TO in single-user dev mode).
"""
import html
import json
import logging
import smtplib
from collections.abc import Callable
from datetime import datetime, timezone
from email.message import EmailMessage
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.config import settings
from app.models import Notification, NotificationDelivery, User
from app.observability import only, traceable

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


@traceable(name='send_email', run_type='tool', process_outputs=lambda _: {'sent': True},
           process_inputs=lambda i: {'to_domain': (i.get('to') or '').rpartition('@')[2], 'subject': i.get('subject')})
def send_email(to: str, subject: str, text: str, html_body: str | None = None) -> None:
    if not smtp_configured():
        raise RuntimeError('SMTP settings are incomplete')
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = settings.smtp_from, to, subject
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype='html')
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)


def recipient_for(user: User | None) -> tuple[str | None, str | None]:
    """(address, reason-if-none)."""
    if user is None:
        return None, 'unknown user'
    if not user.email_enabled:
        return None, 'email notifications disabled in settings'
    if user.email and user.email_verified:
        return user.email, None
    if not settings.auth_enabled and settings.alert_to:
        return settings.alert_to, None
    return None, 'no verified email address'


@traceable(name='notify', process_inputs=only('user_id', 'kind', 'dedupe_key', 'email'),
           process_outputs=lambda n: {'created': n is not None})
def notify(session: Session, user_id: str, *, kind: str, title: str, body: str, dedupe_key: str,
           payload: dict | None = None, email: bool = True) -> Notification | None:
    """Create a notification once. Returns None if it already existed (idempotent)."""
    if session.scalar(select(Notification).where(Notification.user_id == user_id,
                                                 Notification.dedupe_key == dedupe_key)):
        return None
    n = Notification(user_id=user_id, kind=kind, title=title[:200], body=body[:1000], dedupe_key=dedupe_key[:120],
                     payload_json=json.dumps(payload or {}))
    try:
        with session.begin_nested():
            session.add(n)
            session.flush()
    except IntegrityError:  # a concurrent job created it first
        return None
    if email:
        session.add(NotificationDelivery(notification_id=n.id, channel='email', status='pending', attempts=0))
        session.flush()
    return n


EmailRenderer = Callable[[Session, Notification], tuple[str, str, str | None]]
Sender = Callable[[str, str, str, str | None], None]


def default_renderer(session: Session, n: Notification) -> tuple[str, str, str | None]:
    payload = json.loads(n.payload_json or '{}')
    if n.kind == 'portfolio_review' and payload.get('review_id'):
        from app.models import PortfolioReview
        from app.advisor import review_to_dict
        review = session.get(PortfolioReview, payload['review_id'])
        if review is not None:
            return render_review_email(review_to_dict(review))
    text = f'{n.body}\n\nOpen FinSight to see details.'
    return n.title, text, None


@traceable(name='deliver_emails', process_inputs=lambda i: {})
def deliver_pending(session: Session, *, sender: Sender = send_email,
                    renderer: EmailRenderer = default_renderer) -> dict[str, int]:
    stats = {'sent': 0, 'failed': 0, 'skipped': 0}
    deliveries = session.scalars(select(NotificationDelivery).where(
        NotificationDelivery.channel == 'email', NotificationDelivery.status.in_(('pending', 'failed')),
        NotificationDelivery.attempts < MAX_ATTEMPTS)).all()
    for d in deliveries:
        n = session.get(Notification, d.notification_id)
        to, reason = recipient_for(session.get(User, n.user_id))
        if to is None or not smtp_configured():
            d.status, d.last_error = 'skipped', (reason or 'SMTP not configured')[:300]
            stats['skipped'] += 1
            continue
        d.attempts += 1
        try:
            subject, text, html_body = renderer(session, n)
            sender(to, subject, text, html_body)
            d.status, d.sent_at, d.last_error = 'sent', datetime.now(timezone.utc), None
            stats['sent'] += 1
        except Exception as exc:
            d.status, d.last_error = 'failed', f'{type(exc).__name__}: {str(exc)[:250]}'
            stats['failed'] += 1
            log.warning('Email delivery %s failed (attempt %d)', d.id, d.attempts)
    session.flush()
    return stats


# ---- review email --------------------------------------------------------------------------------

STANCE_LABEL = {'hold': 'Hold', 'watch_closely': 'Watch closely', 'consider_adding': 'Consider adding',
                'consider_trimming': 'Consider trimming', 'review_thesis': 'Review your thesis'}


def render_review_email(r: dict) -> tuple[str, str, str]:
    rep = r['report'] or {}
    pf = r.get('portfolio') or {}
    total = pf.get('total_market_value')
    subject = f'FinSight weekly review: {rep.get("headline", "Your portfolio")}'[:150]
    lines = [rep.get('headline', ''), '', rep.get('summary', ''), '',
             f'Portfolio value (delayed prices): INR {total}' if total else '', '', 'Holdings:']
    for h in rep.get('holdings', []):
        lines.append(f'- {h["symbol"]}: {STANCE_LABEL.get(h["stance"], h["stance"])} - {h["reasoning"]}')
    lines += ['', 'Things to consider:'] + [f'- [{a["priority"]}] {a["action"]}: {a["rationale"]}'
                                            for a in rep.get('actions', [])]
    lines += ['', r['disclaimer'], f'Market data: {r["source"]}']
    text = '\n'.join(lines)

    e = html.escape
    holdings = ''.join(
        f'<tr><td style="padding:8px;border-bottom:1px solid #eee"><b>{e(h["symbol"])}</b><br>'
        f'<span style="color:#667085">{e(h["name"])}</span></td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee">{e(STANCE_LABEL.get(h["stance"], h["stance"]))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #eee">{e(h["reasoning"])}</td></tr>'
        for h in rep.get('holdings', []))
    actions = ''.join(f'<li><b>{e(a["action"])}</b> — {e(a["rationale"])} <i>(risk: {e(a["risk"])})</i></li>'
                      for a in rep.get('actions', []))
    body = f"""<div style="font-family:system-ui,Segoe UI,sans-serif;max-width:680px;margin:auto;color:#101828">
<h2 style="margin-bottom:4px">{e(rep.get('headline', 'Weekly portfolio review'))}</h2>
<p style="color:#475467">{e(rep.get('summary', ''))}</p>
{f'<p><b>Portfolio value:</b> INR {e(str(total))} <span style="color:#667085">(delayed prices)</span></p>' if total else ''}
<h3>Holdings</h3><table style="border-collapse:collapse;width:100%;font-size:14px">{holdings}</table>
<h3>Things to consider</h3><ul>{actions}</ul>
<p style="color:#667085;font-size:12px">{e(r['disclaimer'])}<br>Market data: {e(r['source'])}</p></div>"""
    return subject, text, body
