"""Deterministic investment analytics over imported broker documents.

Provenance labels used in every result:
  reported  - copied from a broker/depository statement as of its date (not live)
  computed  - calculated here from reported records
No market data is used yet, so there is no live valuation and no P&L: holdings statements carry no
purchase cost. Missing data is reported as missing (None), never as zero.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from app.models import (BrokerLedgerEntry, HoldingPosition, InvestmentAccount, InvestmentImport,
                        InvestmentTransaction, Security)

ZERO = Decimal('0.00')
STALE_AFTER_DAYS = 7


def _latest_snapshots(session: Session, user_id: str) -> list[InvestmentImport]:
    snaps = session.scalars(select(InvestmentImport).options(joinedload(InvestmentImport.account)).where(
        InvestmentImport.user_id == user_id, InvestmentImport.kind == 'holdings_snapshot',
        InvestmentImport.status != 'duplicate', InvestmentImport.as_of.is_not(None),
        InvestmentImport.rows_inserted > 0)).all()
    latest: dict[str, InvestmentImport] = {}
    for s in snaps:  # one snapshot per account: the most recent as-of date wins
        if s.account_id not in latest or s.as_of > latest[s.account_id].as_of:
            latest[s.account_id] = s
    return list(latest.values())


def current_holdings(session: Session, user_id: str, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    snaps = _latest_snapshots(session, user_id)
    by_security: dict[str, dict[str, Any]] = {}
    warnings = []
    for snap in snaps:
        age = (today - snap.as_of).days
        if age > STALE_AFTER_DAYS:
            warnings.append(f'{snap.account.platform} {snap.account.account_mask}: holdings are {age} days old '
                            f'(as of {snap.as_of})')
        if snap.reconciliation_status != 'reconciled':
            warnings.append(f'{snap.account.platform} {snap.account.account_mask} snapshot {snap.as_of}: '
                            f'reconciliation {snap.reconciliation_status}; reported values may be unreliable')
        positions = session.scalars(select(HoldingPosition).options(joinedload(HoldingPosition.security))
                                    .where(HoldingPosition.import_id == snap.id)).all()
        for p in positions:
            key = p.security.isin or (f'{p.security.symbol}:{p.currency}' if p.security.symbol else p.security.id)
            entry = by_security.setdefault(key, {'isin': p.security.isin, 'symbol': p.security.symbol,
                                                 'currency': p.currency, 'name': p.security.name,
                                                 'asset_type': p.security.asset_type, 'quantity': Decimal(0),
                                                 'holdings': []})
            entry['quantity'] += p.quantity
            entry['holdings'].append({
                'platform': snap.account.platform, 'account': snap.account.account_mask, 'as_of': snap.as_of,
                'quantity': p.quantity, 'reported_rate': p.reported_rate, 'reported_value': p.reported_value,
                'average_cost': p.average_cost, 'cost_value': p.cost_value,
                'snapshot_reconciliation': snap.reconciliation_status, 'document_id': snap.document_id})
    securities = sorted(by_security.values(), key=lambda s: s['name'])
    for s in securities:
        values = [h['reported_value'] for h in s['holdings']]
        s['reported_value'] = sum(values, ZERO) if all(v is not None for v in values) else None
    totals = [s['reported_value'] for s in securities if s['currency'] == 'INR']  # never add USD to INR
    stated = [snap.stated_total_value for snap in snaps]
    return {
        'securities': securities,
        'positions_count': len(securities),
        'reported_value_total_from_rows': sum(totals, ZERO) if totals and all(v is not None for v in totals) else None,
        'statement_totals': [{'platform': s.account.platform, 'account': s.account.account_mask, 'as_of': s.as_of,
                              'stated_total_value': s.stated_total_value,
                              'reconciliation_status': s.reconciliation_status} for s in snaps],
        'warnings': warnings,
        'provenance': 'quantities and values are REPORTED by holdings statements as of their dates; not live prices',
        'not_available': ['live market value (see live portfolio)'],
    }


# ---- performance: realised P&L, charges, XIRR -------------------------------------------------------

def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Annualised internal rate of return for dated cash flows (negative = money in, positive = money out).
    Bisection on NPV(r) = sum(cf / (1+r)^(days/365)) = 0; returns None if there is no sign change."""
    if len(flows) < 2 or not any(cf < 0 for _, cf in flows) or not any(cf > 0 for _, cf in flows):
        return None
    t0 = min(d for d, _ in flows)

    def npv(r):
        return sum(cf / (1 + r) ** ((d - t0).days / 365) for d, cf in flows)
    lo, hi = -0.9999, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2 * 100, 2)


def performance(session: Session, user_id: str, live_prices: dict[str, Decimal] | None = None,
                today: date | None = None) -> dict[str, Any]:
    """Per broker account with a trade report: realised P&L & charges (as reported), invested capital in open
    lots, and XIRR from dated buys/sells plus the current value of open lots.

    XIRR is only computed when the open quantity implied by the lots equals the account's latest holdings,
    i.e. we know every purchase behind what is held. live_prices maps ISIN -> current price."""
    today = today or date.today()
    reports = session.scalars(select(InvestmentImport).options(joinedload(InvestmentImport.account)).where(
        InvestmentImport.user_id == user_id, InvestmentImport.kind == 'trade_report')).all()
    latest: dict[str, InvestmentImport] = {}
    for r in reports:
        if r.account_id not in latest or (r.period_end or date.min) > (latest[r.account_id].period_end or date.min):
            latest[r.account_id] = r
    holdings_by_account: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for snap in _latest_snapshots(session, user_id):
        for p in session.scalars(select(HoldingPosition).options(joinedload(HoldingPosition.security))
                                 .where(HoldingPosition.import_id == snap.id)):
            if p.security.isin:
                holdings_by_account[snap.account_id][p.security.isin] = p.quantity
    accounts = []
    for account_id, report in latest.items():
        import json as _json
        summary = _json.loads(report.summary_json or '{}')
        txns = session.scalars(select(InvestmentTransaction).options(joinedload(InvestmentTransaction.security))
                               .join(Security, Security.id == InvestmentTransaction.security_id)
                               .where(InvestmentTransaction.account_id == account_id,
                                      Security.asset_type != 'mutual_fund')).all()
        open_qty: dict[str, Decimal] = defaultdict(Decimal)
        flows: list[tuple[date, float]] = []
        open_cost: dict[str, Decimal] = defaultdict(lambda: Decimal('0.00'))
        for t in txns:
            isin = t.security.isin
            if t.transaction_type == 'buy':
                open_qty[isin] += t.quantity
                flows.append((t.trade_date, -float(t.amount)))
            else:
                open_qty[isin] -= t.quantity
                flows.append((t.trade_date, float(t.amount)))
        open_positions = {i: q for i, q in open_qty.items() if q != 0}
        held = holdings_by_account.get(account_id, {})
        complete = bool(open_positions) and open_positions == {i: q for i, q in held.items() if q}
        terminal, missing = Decimal('0.00'), []
        for isin, qty in open_positions.items():
            price = (live_prices or {}).get(isin)
            if price is None:
                missing.append(isin)
            else:
                terminal += qty * price
        reason = None
        if not complete:
            reason = ('Open lots in the P&L report do not match the latest holdings statement for this account; '
                      'import both for the same date to compute XIRR')
        elif missing:
            reason = f'No live price for {", ".join(missing)}'
        rate = xirr(flows + [(today, float(terminal))]) if complete and not missing else None
        first = min((d for d, _ in flows), default=None)
        accounts.append({
            'platform': report.account.platform, 'account': report.account.account_mask,
            'report_period': {'start': report.period_start, 'end': report.period_end},
            'realised_pnl': summary.get('realised_pnl'), 'charges_total': summary.get('charges_total'),
            'charges': summary.get('charges', {}),
            'net_cash_invested': sum((t.amount for t in txns if t.transaction_type == 'buy'), Decimal('0.00'))
            - sum((t.amount for t in txns if t.transaction_type == 'sell'), Decimal('0.00')),
            'current_value_open_lots': terminal.quantize(Decimal('0.01')) if not missing else None,
            'xirr_pct': rate, 'xirr_since': first, 'xirr_unavailable_reason': None if rate is not None else reason,
            'note': 'XIRR covers the lots in this report (realised in the period + currently open), valued at '
                    'current delayed prices. Realised P&L and charges are as reported by the broker.'})
    return {'accounts': accounts, 'provenance': 'realised P&L/charges reported by broker; XIRR computed'}


def holding_history(session: Session, user_id: str, isin: str | None = None) -> dict[str, Any]:
    """Quantity per snapshot date - shows how a position changed between statements."""
    stmt = (select(HoldingPosition).options(joinedload(HoldingPosition.security))
            .join(InvestmentImport, InvestmentImport.id == HoldingPosition.import_id)
            .where(HoldingPosition.user_id == user_id, InvestmentImport.status != 'duplicate')
            .order_by(HoldingPosition.as_of))
    rows = session.scalars(stmt).all()
    out = [{'as_of': p.as_of, 'isin': p.security.isin, 'name': p.security.name, 'quantity': p.quantity,
            'reported_value': p.reported_value} for p in rows if not isin or p.security.isin == isin.upper()]
    return {'history': out}


def broker_cash_flows(session: Session, user_id: str, platform: str | None = None, start: date | None = None,
                      end: date | None = None) -> dict[str, Any]:
    """Money moved in/out of broker fund ledgers. deposits - withdrawals = net money added from the bank."""
    stmt = (select(BrokerLedgerEntry, InvestmentAccount).join(InvestmentAccount)
            .where(BrokerLedgerEntry.user_id == user_id).order_by(BrokerLedgerEntry.entry_date, BrokerLedgerEntry.source_row.desc()))
    if platform:
        stmt = stmt.where(InvestmentAccount.platform == platform.upper())
    if start:
        stmt = stmt.where(BrokerLedgerEntry.entry_date >= start)
    if end:
        stmt = stmt.where(BrokerLedgerEntry.entry_date <= end)
    accounts: dict[str, dict[str, Any]] = {}
    for e, acct in session.execute(stmt).all():
        a = accounts.setdefault(acct.id, {
            'platform': acct.platform, 'account': acct.account_mask,
            **{k: ZERO for k in ('deposits', 'withdrawals', 'buy_settlements', 'sell_settlements', 'charges', 'other')},
            'entries': 0, 'first_date': e.entry_date, 'last_date': e.entry_date, 'closing_balance': None})
        field = {'deposit': 'deposits', 'withdrawal': 'withdrawals', 'buy_settlement': 'buy_settlements',
                 'sell_settlement': 'sell_settlements', 'charges': 'charges'}.get(e.kind, 'other')
        a[field] += e.debit + e.credit
        a['entries'] += 1
        a['last_date'] = e.entry_date
        if e.balance is not None:
            a['closing_balance'] = e.balance
    for a in accounts.values():
        a['net_deposited'] = a['deposits'] - a['withdrawals']
        a['net_traded'] = a['sell_settlements'] - a['buy_settlements']  # + means more sold than bought
    imports = session.scalars(select(InvestmentImport).options(joinedload(InvestmentImport.account)).where(
        InvestmentImport.user_id == user_id, InvestmentImport.kind == 'funds_ledger')).all()
    return {'accounts': list(accounts.values()),
            'ledgers': [{'platform': i.account.platform if i.account else None, 'period_start': i.period_start,
                         'period_end': i.period_end, 'reconciliation_status': i.reconciliation_status}
                        for i in imports],
            'definitions': {'net_deposited': 'deposits - withdrawals (money added from bank, net)',
                            'net_traded': 'sell settlements - buy settlements'},
            'provenance': 'computed from broker fund-ledger entries'}


def fund_orders(session: Session, user_id: str, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    stmt = (select(InvestmentTransaction).options(joinedload(InvestmentTransaction.security))
            .join(Security, Security.id == InvestmentTransaction.security_id)
            .where(InvestmentTransaction.user_id == user_id, Security.asset_type == 'mutual_fund')
            .order_by(InvestmentTransaction.trade_date))
    if start:
        stmt = stmt.where(InvestmentTransaction.trade_date >= start)
    if end:
        stmt = stmt.where(InvestmentTransaction.trade_date <= end)
    schemes: dict[str, dict[str, Any]] = defaultdict(lambda: {'invested': ZERO, 'redeemed': ZERO,
                                                              'units_bought': Decimal(0), 'units_sold': Decimal(0),
                                                              'orders': 0})
    for t in session.scalars(stmt):
        s = schemes[t.security.name]
        s['orders'] += 1
        if t.transaction_type == 'buy':
            s['invested'] += t.amount
            s['units_bought'] += t.quantity or 0
        else:
            s['redeemed'] += t.amount
            s['units_sold'] += t.quantity or 0
    out = [{'scheme': k, **v, 'net_units': v['units_bought'] - v['units_sold']} for k, v in schemes.items()]
    return {'schemes': out, 'total_invested': sum((s['invested'] for s in out), ZERO),
            'total_redeemed': sum((s['redeemed'] for s in out), ZERO),
            'provenance': 'computed from mutual fund order history'}
