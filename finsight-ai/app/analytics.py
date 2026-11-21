"""Deterministic bank analytics over structured SQL records (no LLM involved).

Definitions (all amounts Decimal, per currency):
  income        = credits, excluding own-money movements (internal/investment transfers) and refunds
  gross_spend   = debits, excluding own-money movements
  refunds       = credits categorised as Refunds (they reduce spending rather than count as income)
  spending      = gross_spend - refunds
  net_invested  = debits to investment platforms - credits back from them
  savings       = income - spending           (money kept, whether left in the bank or invested)
  savings_rate  = savings / income
  investment_rate = net_invested / income
Rates are None (not 0) when income is zero — missing data is not the same as zero.
"""
import statistics
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from app.models import BankTransactionRecord as Txn, StatementImport
from app.schemas import NON_SPENDING_CATEGORIES

ZERO = Decimal('0.00')
INVEST = 'Investment transfers'


def _q(v: Decimal) -> Decimal:
    return v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return _q(part / whole * 100) if whole else None


def _rows(session: Session, user_id: str, start: date | None = None, end: date | None = None,
          account_id: str | None = None) -> list[Txn]:
    stmt = select(Txn).where(Txn.user_id == user_id)
    if start:
        stmt = stmt.where(Txn.transaction_date >= start)
    if end:
        stmt = stmt.where(Txn.transaction_date <= end)
    if account_id:
        stmt = stmt.where(Txn.account_id == account_id)
    return list(session.scalars(stmt.order_by(Txn.transaction_date, Txn.source_page, Txn.source_row)))


def _is_income(t: Txn) -> bool:
    return t.transaction_type == 'credit' and t.category not in NON_SPENDING_CATEGORIES and t.category != 'Refunds'


def _is_spend(t: Txn) -> bool:
    return t.transaction_type == 'debit' and t.category not in NON_SPENDING_CATEGORIES


def _needs_review(t: Txn) -> bool:
    return t.extraction_status == 'needs_review' or (t.category_source in ('llm', 'default')
                                                     and t.category_confidence < 0.6)


def _totals(rows: list[Txn]) -> dict[str, Any]:
    income = sum((t.amount for t in rows if _is_income(t)), ZERO)
    gross = sum((t.amount for t in rows if _is_spend(t)), ZERO)
    refunds = sum((t.amount for t in rows if t.transaction_type == 'credit' and t.category == 'Refunds'), ZERO)
    inv_out = sum((t.amount for t in rows if t.transaction_type == 'debit' and t.category == INVEST), ZERO)
    inv_in = sum((t.amount for t in rows if t.transaction_type == 'credit' and t.category == INVEST), ZERO)
    spending = gross - refunds
    return {'income': income, 'gross_spending': gross, 'refunds': refunds, 'spending': spending,
            'invested_out': inv_out, 'invested_back': inv_in, 'net_invested': inv_out - inv_in,
            'savings': income - spending}


def _meta(session: Session, user_id: str, rows: list[Txn]) -> dict[str, Any]:
    imports = session.scalars(select(StatementImport).where(
        StatementImport.user_id == user_id, StatementImport.id.in_({t.import_id for t in rows}))).all()
    currencies = sorted({t.currency for t in rows})
    return {
        'source': 'structured transactions extracted from uploaded statements (SQL)',
        'currency': currencies[0] if len(currencies) == 1 else currencies,
        'transactions_considered': len(rows),
        'data_from': rows[0].transaction_date if rows else None,
        'data_through': rows[-1].transaction_date if rows else None,
        'needs_review_count': sum(_needs_review(t) for t in rows),
        'statements': [{'document_id': i.document_id, 'reconciliation_status': i.reconciliation_status,
                        'period_start': i.period_start, 'period_end': i.period_end} for i in imports],
    }


def _guard_currency(rows: list[Txn]) -> None:
    if len({t.currency for t in rows}) > 1:
        raise ValueError('Transactions span multiple currencies; filter by account before aggregating')


def income_expense_summary(session: Session, user_id: str, start: date | None = None,
                           end: date | None = None) -> dict[str, Any]:
    rows = _rows(session, user_id, start, end)
    _guard_currency(rows)
    totals = _totals(rows)
    return {**totals,
            'savings_rate_pct': _pct(totals['savings'], totals['income']),
            'investment_rate_pct': _pct(totals['net_invested'], totals['income']),
            'all_credits': sum((t.amount for t in rows if t.transaction_type == 'credit'), ZERO),
            'all_debits': sum((t.amount for t in rows if t.transaction_type == 'debit'), ZERO),
            'period': {'start': start, 'end': end}, 'meta': _meta(session, user_id, rows)}


def monthly_cash_flow(session: Session, user_id: str, start: date | None = None,
                      end: date | None = None) -> dict[str, Any]:
    rows = _rows(session, user_id, start, end)
    _guard_currency(rows)
    by_month: dict[str, list[Txn]] = defaultdict(list)
    for t in rows:
        by_month[t.transaction_date.strftime('%Y-%m')].append(t)
    months = []
    for month in sorted(by_month):
        m_rows = by_month[month]
        totals = _totals(m_rows)
        net = sum((t.amount if t.transaction_type == 'credit' else -t.amount for t in m_rows), ZERO)
        months.append({'month': month, 'income': totals['income'], 'spending': totals['spending'],
                       'net_invested': totals['net_invested'], 'savings': totals['savings'],
                       'net_cash_flow': net, 'transactions': len(m_rows)})
    return {'months': months, 'meta': _meta(session, user_id, rows)}


def category_breakdown(session: Session, user_id: str, start: date | None = None,
                       end: date | None = None) -> dict[str, Any]:
    """Spending by category. Own-money transfers are reported separately, never as spending."""
    rows = _rows(session, user_id, start, end)
    _guard_currency(rows)
    spend: dict[str, list[Txn]] = defaultdict(list)
    for t in rows:
        if _is_spend(t):
            spend[t.category].append(t)
    total = sum((t.amount for ts in spend.values() for t in ts), ZERO)
    categories = sorted(
        ({'category': c, 'amount': sum((t.amount for t in ts), ZERO), 'count': len(ts)} for c, ts in spend.items()),
        key=lambda r: r['amount'], reverse=True)
    for c in categories:
        c['share_pct'] = _pct(c['amount'], total)
    totals = _totals(rows)
    return {'categories': categories, 'gross_spending': total, 'refunds': totals['refunds'],
            'excluded_transfers': {'investment_transfers_out': totals['invested_out'],
                                   'internal_transfers_out': sum((t.amount for t in rows if t.transaction_type == 'debit'
                                                                  and t.category == 'Internal transfers'), ZERO)},
            'meta': _meta(session, user_id, rows)}


def spending_trends(session: Session, user_id: str, month: str | None = None) -> dict[str, Any]:
    """Category spending in `month` (YYYY-MM, default: latest month with data) vs the previous month."""
    rows = _rows(session, user_id)
    _guard_currency(rows)
    months = sorted({t.transaction_date.strftime('%Y-%m') for t in rows})
    if not months:
        return {'month': None, 'previous_month': None, 'changes': [], 'meta': _meta(session, user_id, rows)}
    month = month or months[-1]
    y, m = map(int, month.split('-'))
    prev = f'{y - 1}-12' if m == 1 else f'{y}-{m - 1:02d}'
    cur_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    prev_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in rows:
        key = t.transaction_date.strftime('%Y-%m')
        if _is_spend(t) and key in (month, prev):
            (cur_totals if key == month else prev_totals)[t.category] += t.amount
    changes = []
    for cat in sorted(set(cur_totals) | set(prev_totals)):
        cur, before = cur_totals[cat], prev_totals[cat]
        changes.append({'category': cat, 'current': cur, 'previous': before, 'change': cur - before,
                        'change_pct': _pct(cur - before, before)})
    changes.sort(key=lambda c: c['change'], reverse=True)
    in_month = [t for t in rows if t.transaction_date.strftime('%Y-%m') == month]
    return {'month': month, 'previous_month': prev, 'previous_month_has_data': prev in months,
            'current_total': sum(cur_totals.values(), ZERO), 'previous_total': sum(prev_totals.values(), ZERO),
            'changes': changes,
            'note': f'{month} data runs through {in_month[-1].transaction_date}' if in_month else None,
            'meta': _meta(session, user_id, rows)}


def recurring_payments(session: Session, user_id: str, min_occurrences: int = 3,
                       amount_tolerance: Decimal = Decimal('0.10')) -> dict[str, Any]:
    """Debits to the same merchant in >= `min_occurrences` distinct months at a roughly monthly
    cadence (median gap 20-40 days) with amounts within +/-10% of their median."""
    rows = _rows(session, user_id)
    by_merchant: dict[str, list[Txn]] = defaultdict(list)
    for t in rows:
        if t.transaction_type == 'debit':
            by_merchant[t.merchant].append(t)
    found = []
    for merchant, ts in by_merchant.items():
        months = {t.transaction_date.strftime('%Y-%m') for t in ts}
        if len(months) < min_occurrences:
            continue
        median_amount = statistics.median(t.amount for t in ts)
        consistent = [t for t in ts if abs(t.amount - median_amount) <= median_amount * amount_tolerance]
        if len({t.transaction_date.strftime('%Y-%m') for t in consistent}) < min_occurrences:
            continue
        gaps = [(b.transaction_date - a.transaction_date).days for a, b in zip(consistent, consistent[1:])]
        median_gap = statistics.median(gaps) if gaps else 0
        if not 20 <= median_gap <= 40:
            continue
        found.append({
            'merchant': merchant, 'category': statistics.mode(t.category for t in consistent),
            'typical_amount': _q(Decimal(median_amount)), 'occurrences': len(consistent),
            'total': sum((t.amount for t in consistent), ZERO), 'cadence': 'monthly',
            'median_gap_days': median_gap, 'first_date': consistent[0].transaction_date,
            'last_date': consistent[-1].transaction_date,
            'is_spending': consistent[0].category not in NON_SPENDING_CATEGORIES,
        })
    found.sort(key=lambda r: r['typical_amount'], reverse=True)
    monthly_spend = sum((r['typical_amount'] for r in found if r['is_spending']), ZERO)
    return {'recurring': found, 'estimated_monthly_recurring_spend': monthly_spend,
            'meta': _meta(session, user_id, rows)}


def largest_transactions(session: Session, user_id: str, transaction_type: Literal['debit', 'credit'] = 'debit',
                         limit: int = 10, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    rows = [t for t in _rows(session, user_id, start, end) if t.transaction_type == transaction_type]
    top = sorted(rows, key=lambda t: t.amount, reverse=True)[:max(1, min(limit, 50))]
    return {'transactions': [serialize_txn(t) for t in top], 'meta': _meta(session, user_id, rows)}


def investment_transfers(session: Session, user_id: str, platform: str | None = None,
                         start: date | None = None, end: date | None = None) -> dict[str, Any]:
    """Money moved to/from investment platforms (SIPs, brokers), grouped by merchant."""
    rows = [t for t in _rows(session, user_id, start, end) if t.category == INVEST]
    if platform:
        rows = [t for t in rows if platform.upper() in t.merchant or platform.upper() in t.description.upper()]
    groups: dict[str, dict[str, Any]] = {}
    for t in rows:
        g = groups.setdefault(t.merchant, {'merchant': t.merchant, 'out': ZERO, 'back': ZERO, 'count': 0})
        g['out' if t.transaction_type == 'debit' else 'back'] += t.amount
        g['count'] += 1
    for g in groups.values():
        g['net'] = g['out'] - g['back']
    total_out = sum((g['out'] for g in groups.values()), ZERO)
    income = _totals(_rows(session, user_id, start, end))['income']
    return {'platforms': sorted(groups.values(), key=lambda g: g['net'], reverse=True),
            'total_transferred_out': total_out,
            'total_net': sum((g['net'] for g in groups.values()), ZERO),
            'share_of_income_pct': _pct(total_out, income),
            'note': 'Bank-side transfers only; this is not portfolio value or returns.',
            'meta': _meta(session, user_id, rows)}


def list_transactions(session: Session, user_id: str, *, start: date | None = None, end: date | None = None,
                      category: str | None = None, transaction_type: str | None = None,
                      search: str | None = None, min_amount: Decimal | None = None,
                      max_amount: Decimal | None = None, needs_review: bool | None = None,
                      limit: int = 50, offset: int = 0) -> dict[str, Any]:
    stmt = select(Txn).where(Txn.user_id == user_id)
    if start:
        stmt = stmt.where(Txn.transaction_date >= start)
    if end:
        stmt = stmt.where(Txn.transaction_date <= end)
    if category:
        stmt = stmt.where(Txn.category == category)
    if transaction_type:
        stmt = stmt.where(Txn.transaction_type == transaction_type)
    if search:
        like = f'%{search.strip()[:60]}%'  # bound parameter, never interpolated SQL
        stmt = stmt.where(or_(Txn.description.ilike(like), Txn.merchant.ilike(like)))
    rows = list(session.scalars(stmt.order_by(Txn.transaction_date.desc(), Txn.source_page.desc(),
                                              Txn.source_row.desc())))
    if min_amount is not None:
        rows = [t for t in rows if t.amount >= min_amount]
    if max_amount is not None:
        rows = [t for t in rows if t.amount <= max_amount]
    if needs_review is not None:
        rows = [t for t in rows if _needs_review(t) == needs_review]
    limit = max(1, min(limit, 200))
    page = rows[offset:offset + limit]
    return {'transactions': [serialize_txn(t) for t in page], 'total_matching': len(rows),
            'sum_debits': sum((t.amount for t in rows if t.transaction_type == 'debit'), ZERO),
            'sum_credits': sum((t.amount for t in rows if t.transaction_type == 'credit'), ZERO)}


def serialize_txn(t: Txn) -> dict[str, Any]:
    return {'id': t.id, 'date': t.transaction_date, 'description': t.description, 'merchant': t.merchant,
            'amount': t.amount, 'type': t.transaction_type, 'currency': t.currency,
            'balance_after': t.balance_after_transaction, 'category': t.category,
            'category_source': t.category_source, 'category_confidence': t.category_confidence,
            'needs_review': _needs_review(t), 'source': {'document_id': t.document_id, 'page': t.source_page}}


def to_jsonable(value: Any) -> Any:
    """Decimals become strings (never floats) so exact amounts survive JSON."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value
