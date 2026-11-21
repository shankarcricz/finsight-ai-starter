"""Phase 3 orchestration: detect -> parse -> reconcile -> persist investment documents (idempotently)."""
import hashlib
import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import (BrokerLedgerEntry, HoldingPosition, InvestmentAccount, InvestmentImport,
                        InvestmentTransaction, Security)
from app.parsers.investments import SHEET_PARSERS, DPHoldingParser
from app.reconciliation import Check
from app.schemas import ExtractionWarning, ParsedFundOrders, ParsedHoldings, ParsedLedger, ParsedTradeReport
from app.spreadsheets import Cell
from app.observability import only, summarize_import, traceable

CENT = Decimal('0.01')


class InvestmentImportSummary(BaseModel):
    import_id: str
    document_id: str
    kind: str
    parser: str
    platform: str | None
    account: str | None
    status: str
    reconciliation_status: str
    as_of: date | None
    period_start: date | None
    period_end: date | None
    stated_total_value: Decimal | None
    rows_found: int
    rows_inserted: int
    duplicates_skipped: int
    checks: list[dict]
    warnings: list[dict]
    summary: dict = {}


def summarize(imp: InvestmentImport) -> InvestmentImportSummary:
    return InvestmentImportSummary(
        import_id=imp.id, document_id=imp.document_id, kind=imp.kind, parser=imp.parser,
        platform=imp.account.platform if imp.account else None,
        account=imp.account.account_mask if imp.account else None, status=imp.status,
        reconciliation_status=imp.reconciliation_status, as_of=imp.as_of, period_start=imp.period_start,
        period_end=imp.period_end, stated_total_value=imp.stated_total_value, rows_found=imp.rows_found,
        rows_inserted=imp.rows_inserted, duplicates_skipped=imp.duplicates_skipped,
        checks=json.loads(imp.reconciliation_json or '{}').get('checks', []),
        warnings=json.loads(imp.warnings_json or '[]'), summary=json.loads(imp.summary_json or '{}'))


# ---- reconciliation -------------------------------------------------------------------------------

def _rounding_tolerance(qty: Decimal) -> Decimal:
    """Brokers print average prices rounded to 2 decimals, so qty x avg can differ from the true buy value
    by up to half a paisa per share."""
    return qty * Decimal('0.005') + CENT


def reconcile_holdings(p: ParsedHoldings) -> tuple[str, list[Check]]:
    """Quantities and valuations are checked separately: a stale price must not cast doubt on quantities."""
    checks: list[Check] = []
    qty_breaks = []
    has_components = False
    for r in p.rows:
        parts = [r.free_quantity, r.pledged_quantity, r.demat_quantity, r.remat_quantity, r.locked_quantity]
        if any(x is not None for x in parts):
            has_components = True
            if sum(x or 0 for x in parts) != r.quantity:
                qty_breaks.append(f'{r.isin or r.symbol}: components sum to {sum(x or 0 for x in parts)}, total says {r.quantity}')
    if has_components:
        checks.append(Check(name='quantity_components', status='fail' if qty_breaks else 'pass',
                            detail='; '.join(qty_breaks) or 'Free + pledged + locked balances equal total quantity'))
    cost_rows = [r for r in p.rows if r.average_cost is not None and r.cost_value is not None]
    if cost_rows:
        bad = [f'{r.isin or r.symbol}: {r.quantity} x {r.average_cost} vs buy value {r.cost_value}' for r in cost_rows
               if abs(r.quantity * r.average_cost - r.cost_value) > _rounding_tolerance(r.quantity)]
        checks.append(Check(name='row_cost', status='fail' if bad else 'pass',
                            detail='; '.join(bad) or 'Quantity x average price equals buy value (within rounding)'))
        if p.stated_cost_total is not None:
            total = sum((r.cost_value for r in cost_rows), Decimal('0.00'))
            tol = sum((_rounding_tolerance(r.quantity) for r in cost_rows), Decimal('0'))
            ok = abs(total - p.stated_cost_total) <= tol
            checks.append(Check(name='stated_cost_total', status='pass' if ok else 'fail', expected=p.stated_cost_total,
                                actual=total, detail=f'Row buy values {total} vs invested value {p.stated_cost_total}'
                                + (' (difference is price rounding)' if ok and total != p.stated_cost_total else '')))
    value_breaks = [f'{r.isin or r.symbol}: {r.quantity} x {r.reported_rate} = {(r.quantity * r.reported_rate).quantize(CENT)}, '
                    f'statement shows {r.reported_value}'
                    for r in p.rows if r.reported_rate is not None and r.reported_value is not None
                    and abs(r.quantity * r.reported_rate - r.reported_value) > CENT]
    checks.append(Check(name='row_valuation', status='fail' if value_breaks else 'pass',
                        detail='; '.join(value_breaks) or 'Quantity x rate equals value on every row'))
    if p.stated_total_value is None:
        checks.append(Check(name='stated_total_value', status='skipped', detail='No total printed'))
    else:
        rows_total = sum((r.reported_value or Decimal(0) for r in p.rows), Decimal('0.00'))
        ok = rows_total == p.stated_total_value
        checks.append(Check(
            name='stated_total_value', status='pass' if ok else 'fail', expected=p.stated_total_value,
            actual=rows_total,
            detail='Row values add up to the printed total' if ok else
            f'Row values add up to {rows_total} but the statement total is {p.stated_total_value}. The statement '
            'is internally inconsistent (often a stale per-row rate); quantities are unaffected, valuations are '
            'unreliable.'))
    status = 'mismatch' if any(c.status == 'fail' for c in checks) else 'reconciled' if p.rows else 'unverifiable'
    return status, checks


def reconcile_ledger(p: ParsedLedger) -> tuple[str, list[Check]]:
    rows = [r for r in p.rows if r.balance is not None]
    if not rows:
        return 'unverifiable', [Check(name='running_balance', status='skipped', detail='No balances')]
    first = p.rows[0]
    prev = first.balance - first.credit + first.debit if first.balance is not None else None
    breaks = []
    for r in p.rows:
        if r.balance is None:
            prev = None
            continue
        if prev is not None and prev + r.credit - r.debit != r.balance:
            breaks.append(f'{r.entry_date} (row {r.source_row}): expected {prev + r.credit - r.debit}, shows {r.balance}')
        prev = r.balance
    opening = first.balance - first.credit + first.debit if first.balance is not None else None
    checks = [
        Check(name='running_balance', status='fail' if breaks else 'pass',
              detail='; '.join(breaks[:5]) or f'{len(rows)} running balances are continuous'),
        Check(name='opening_closing', status='pass', expected=p.rows[-1].balance, actual=opening,
              detail=f'Opening balance (derived) {opening}, closing balance {p.rows[-1].balance}'),
    ]
    return ('mismatch' if breaks else 'reconciled'), checks


# ---- persistence helpers ----------------------------------------------------------------------------

def _account(session: Session, user_id: str, platform: str, mask: str) -> InvestmentAccount:
    acct = session.scalar(select(InvestmentAccount).where(
        InvestmentAccount.user_id == user_id, InvestmentAccount.platform == platform,
        InvestmentAccount.account_mask == mask))
    if acct is None:
        acct = InvestmentAccount(user_id=user_id, platform=platform, account_mask=mask)
        session.add(acct)
        session.flush()
    return acct


def _security(session: Session, *, isin: str | None, name: str, asset_type: str, symbol: str | None = None,
              currency: str = 'INR') -> Security:
    if isin:
        sec = session.scalar(select(Security).where(Security.isin == isin))
    elif symbol:  # foreign listings have tickers, not Indian ISINs
        sec = session.scalar(select(Security).where(Security.isin.is_(None), Security.symbol == symbol,
                                                    Security.currency == currency))
    else:
        sec = session.scalar(select(Security).where(Security.isin.is_(None), Security.name == name,
                                                    Security.asset_type == asset_type))
    if sec is None:
        sec = Security(isin=isin, name=name[:200], asset_type=asset_type, symbol=symbol, currency=currency)
        session.add(sec)
        session.flush()
    return sec


def _fp(*parts) -> str:
    return hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()


def _record(session, user_id, document_id, parser, kind, status, warnings, **kw) -> InvestmentImport:
    imp = InvestmentImport(user_id=user_id, document_id=document_id, parser=parser, kind=kind, status=status,
                           warnings_json=json.dumps([w.model_dump() for w in warnings]), **kw)
    session.add(imp)
    session.flush()
    return imp


# ---- importers -----------------------------------------------------------------------------------

def _import_holdings(session, user_id, document_id, p: ParsedHoldings) -> InvestmentImport:
    warnings = list(p.warnings)
    if not p.account_ref:
        warnings.append(ExtractionWarning(code='account_unidentified', message='BO/Client ID not found'))
    acct = _account(session, user_id, p.platform, p.account_ref or 'UNKNOWN')
    recon_status, checks = reconcile_holdings(p)
    if p.as_of is None or not p.rows:
        return _record(session, user_id, document_id, p.parser, 'holdings_snapshot', 'needs_review',
                       warnings, account_id=acct.id, reconciliation_status='unverifiable',
                       reconciliation_json=json.dumps({'checks': [c.model_dump(mode='json') for c in checks]}),
                       as_of=p.as_of, rows_found=len(p.rows))
    same_day = session.scalar(select(InvestmentImport).where(
        InvestmentImport.account_id == acct.id, InvestmentImport.kind == 'holdings_snapshot',
        InvestmentImport.as_of == p.as_of, InvestmentImport.status != 'duplicate'))
    if same_day:
        warnings.append(ExtractionWarning(code='duplicate_snapshot',
                                          message=f'A snapshot for this account as of {p.as_of} already exists'))
    imp = _record(session, user_id, document_id, p.parser, 'holdings_snapshot',
                  'duplicate' if same_day else ('imported' if recon_status == 'reconciled' else 'needs_review'),
                  warnings, account_id=acct.id, reconciliation_status=recon_status,
                  reconciliation_json=json.dumps({'checks': [c.model_dump(mode='json') for c in checks]}),
                  as_of=p.as_of, stated_total_value=p.stated_total_value, rows_found=len(p.rows),
                  duplicates_skipped=len(p.rows) if same_day else 0)
    if same_day:
        return imp
    for r in p.rows:
        sec = _security(session, isin=r.isin, name=r.name, asset_type=_asset_type(r.isin, r.currency),
                        symbol=r.symbol, currency=r.currency)
        session.add(HoldingPosition(
            currency=r.currency,
            user_id=user_id, import_id=imp.id, account_id=acct.id, security_id=sec.id, as_of=p.as_of,
            quantity=r.quantity, free_quantity=r.free_quantity, pledged_quantity=r.pledged_quantity,
            locked_quantity=r.locked_quantity, reported_rate=r.reported_rate, reported_value=r.reported_value,
            average_cost=r.average_cost, cost_value=r.cost_value,
            source_page=r.source_page, source_row=r.source_row))
    imp.rows_inserted = len(p.rows)
    return imp


def _asset_type(isin: str | None, currency: str = 'INR') -> str:
    if currency != 'INR':
        return 'foreign_equity'
    return 'fund_or_etf' if isin and isin.startswith('INF') else 'equity'  # INF* = mutual fund / ETF units


def reconcile_trade_report(p: ParsedTradeReport) -> tuple[str, list[Check]]:
    checks: list[Check] = []
    bad = []
    for lot in p.lots:
        if lot.sell_value is not None and lot.sell_value - lot.buy_value != lot.pnl:
            bad.append(f'row {lot.source_row}: {lot.sell_value} - {lot.buy_value} != {lot.pnl}')
        if abs(lot.quantity * lot.buy_price - lot.buy_value) > _rounding_tolerance(lot.quantity):
            bad.append(f'row {lot.source_row}: quantity x buy price != buy value')
    checks.append(Check(name='lot_arithmetic', status='fail' if bad else 'pass',
                        detail='; '.join(bad[:5]) or 'Every lot: sell/closing value - buy value = P&L'))
    for label, stated, realised in (('realised', p.stated_realised_pnl, True), ('unrealised', p.stated_unrealised_pnl, False)):
        lots = [l for l in p.lots if l.realised == realised]
        if stated is None:
            continue
        total = sum((l.pnl for l in lots), Decimal('0.00'))
        checks.append(Check(name=f'{label}_pnl_total', status='pass' if total == stated else 'fail', expected=stated,
                            actual=total, detail=f'{label.title()} lots sum to {total}; summary says {stated}'))
    if 'Total' in p.charges:
        parts = sum((v for k, v in p.charges.items() if k != 'Total'), Decimal('0.00'))
        checks.append(Check(name='charges_total', status='pass' if parts == p.charges['Total'] else 'fail',
                            expected=p.charges['Total'], actual=parts, detail=f'Charge lines sum to {parts}'))
    status = 'mismatch' if any(c.status == 'fail' for c in checks) else 'reconciled' if p.lots else 'unverifiable'
    return status, checks


def _import_trade_report(session, user_id, document_id, p: ParsedTradeReport) -> InvestmentImport:
    """Each lot becomes a dated buy (and a sell, if realised) - the cash flows needed for XIRR."""
    acct = _account(session, user_id, p.platform, p.account_ref or 'UNKNOWN')
    recon_status, checks = reconcile_trade_report(p)
    summary = {'realised_pnl': p.stated_realised_pnl, 'unrealised_pnl': p.stated_unrealised_pnl,
               'charges': p.charges, 'charges_total': p.charges.get('Total'),
               'period_start': p.period_start, 'period_end': p.period_end}
    imp = _record(session, user_id, document_id, p.parser, 'trade_report',
                  'imported' if recon_status == 'reconciled' else 'needs_review', list(p.warnings),
                  account_id=acct.id, reconciliation_status=recon_status,
                  reconciliation_json=json.dumps({'checks': [c.model_dump(mode='json') for c in checks]}),
                  summary_json=json.dumps(summary, default=str), period_start=p.period_start,
                  period_end=p.period_end, rows_found=len(p.lots))
    known = set(session.scalars(select(InvestmentTransaction.fingerprint)
                                .where(InvestmentTransaction.account_id == acct.id)))
    seen: Counter = Counter()
    for lot in p.lots:
        sec = _security(session, isin=lot.isin, name=lot.name, asset_type=_asset_type(lot.isin))
        legs = [('buy', lot.buy_date, lot.buy_price, lot.buy_value)]
        if lot.realised:
            legs.append(('sell', lot.sell_date, lot.sell_price, lot.sell_value))
        for side, when, price, value in legs:
            key = (lot.isin, when, side, lot.quantity, price)
            seen[key] += 1
            fp = _fp(acct.id, 'lot', *key, seen[key])
            if fp in known:
                imp.duplicates_skipped += 1
                continue
            known.add(fp)
            session.add(InvestmentTransaction(
                user_id=user_id, account_id=acct.id, document_id=document_id, import_id=imp.id, security_id=sec.id,
                trade_date=when, transaction_type=side, quantity=lot.quantity, price=price, amount=value,
                source_row=lot.source_row, fingerprint=fp))
            imp.rows_inserted += 1
    return imp


def _import_ledger(session, user_id, document_id, p: ParsedLedger) -> InvestmentImport:
    warnings = list(p.warnings)
    acct = _account(session, user_id, p.platform, p.account_ref or 'UNKNOWN')
    recon_status, checks = reconcile_ledger(p)
    review = recon_status != 'reconciled' or any(r.kind == 'other' for r in p.rows)
    imp = _record(session, user_id, document_id, p.parser, 'funds_ledger', 'needs_review' if review else 'imported',
                  warnings, account_id=acct.id, reconciliation_status=recon_status,
                  reconciliation_json=json.dumps({'checks': [c.model_dump(mode='json') for c in checks]}),
                  period_start=p.period_start, period_end=p.period_end, rows_found=len(p.rows))
    known = set(session.scalars(select(BrokerLedgerEntry.fingerprint).where(BrokerLedgerEntry.account_id == acct.id)))
    seen: Counter = Counter()
    for r in p.rows:
        if r.reference:
            fp = _fp(acct.id, 'ref', r.reference)
        else:
            key = (r.entry_date, r.debit, r.credit, r.balance)
            seen[key] += 1
            fp = _fp(acct.id, *key, seen[key])
        if fp in known:
            imp.duplicates_skipped += 1
            continue
        known.add(fp)
        session.add(BrokerLedgerEntry(
            user_id=user_id, account_id=acct.id, document_id=document_id, import_id=imp.id, entry_date=r.entry_date,
            settlement_date=r.settlement_date, segment_type=r.segment_type, transaction_type=r.transaction_type,
            kind=r.kind, reference=r.reference, debit=r.debit, credit=r.credit, balance=r.balance,
            source_row=r.source_row, fingerprint=fp))
        imp.rows_inserted += 1
    return imp


def _import_fund_orders(session, user_id, document_id, p: ParsedFundOrders) -> InvestmentImport:
    warnings = list(p.warnings)
    if p.platform == 'UNKNOWN':
        warnings.append(ExtractionWarning(code='platform_unknown',
                                          message='Platform not stated in the file; choose it in the upload form'))
    acct = _account(session, user_id, p.platform, 'MF')
    checks = [Check(name='rows_validated', status='pass' if not p.warnings else 'fail',
                    detail=f'{len(p.rows)} orders validated' + (f', {len(p.warnings)} warnings' if p.warnings else ''))]
    status = 'needs_review' if p.warnings else 'imported'
    imp = _record(session, user_id, document_id, p.parser, 'mf_orders', status, warnings, account_id=acct.id,
                  reconciliation_status='unverifiable',  # order histories have no balance to reconcile against
                  reconciliation_json=json.dumps({'checks': [c.model_dump(mode='json') for c in checks]}),
                  period_start=p.period_start, period_end=p.period_end, rows_found=len(p.rows))
    known = set(session.scalars(select(InvestmentTransaction.fingerprint)
                                .where(InvestmentTransaction.account_id == acct.id)))
    seen: Counter = Counter()
    for r in p.rows:
        key = (r.scheme_name.upper(), r.order_date, r.transaction_type, r.units, r.amount)
        seen[key] += 1
        fp = _fp(acct.id, *key, seen[key])
        if fp in known:
            imp.duplicates_skipped += 1
            continue
        known.add(fp)
        sec = _security(session, isin=None, name=r.scheme_name, asset_type='mutual_fund')
        session.add(InvestmentTransaction(
            user_id=user_id, account_id=acct.id, document_id=document_id, import_id=imp.id, security_id=sec.id,
            trade_date=r.order_date, transaction_type=r.transaction_type, quantity=r.units, price=r.nav,
            amount=r.amount, source_row=r.source_row, fingerprint=fp))
        imp.rows_inserted += 1
    return imp


def _unsupported(session, user_id, document_id, message: str) -> InvestmentImport:
    return _record(session, user_id, document_id, 'none', 'unsupported', 'unsupported_layout',
                   [ExtractionWarning(code='unsupported_layout', message=message)],
                   reconciliation_status='unverifiable', reconciliation_json='{}')


def _existing(session: Session, document_id: str) -> InvestmentImport | None:
    """Imports are idempotent per document, except 'unsupported' ones: a newer parser may now understand
    the file, so re-uploading retries them."""
    existing = session.scalar(select(InvestmentImport).where(InvestmentImport.document_id == document_id))
    if existing is not None and existing.status == 'unsupported_layout':
        session.delete(existing)
        session.flush()
        return None
    return existing


@traceable(name='import_investment_pdf', run_type='parser', process_outputs=summarize_import,
           process_inputs=lambda i: {'document_id': i.get('document_id'), 'pages': len(i.get('layout_pages') or []),
                                     'platform_hint': i.get('platform_hint')})
def import_investment_pdf(session: Session, *, user_id: str, document_id: str, layout_pages: list[str],
                          platform_hint: str | None = None) -> InvestmentImportSummary:
    if existing := _existing(session, document_id):
        return summarize(existing)
    parser = DPHoldingParser()
    if not parser.detect(layout_pages):
        imp = _unsupported(session, user_id, document_id,
                           'Not a recognised holdings statement. It is still searchable in chat if indexed.')
    else:
        imp = _import_holdings(session, user_id, document_id, parser.parse(layout_pages, platform_hint))
    session.flush()
    return summarize(imp)


@traceable(name='import_investment_sheet', run_type='parser', process_outputs=summarize_import,
           process_inputs=lambda i: {'document_id': i.get('document_id'), 'rows': len(i.get('rows') or []),
                                     'platform_hint': i.get('platform_hint')})
def import_investment_sheet(session: Session, *, user_id: str, document_id: str, rows: list[list[Cell]],
                            filename: str = '', platform_hint: str | None = None) -> InvestmentImportSummary:
    if existing := _existing(session, document_id):
        return summarize(existing)
    score, parser = max(((p.detect(rows), p) for p in SHEET_PARSERS), key=lambda s: s[0])
    if not score:
        imp = _unsupported(session, user_id, document_id, 'Spreadsheet layout not recognised yet.')
    else:
        parsed = parser.parse(rows, platform_hint, filename)
        importer = {ParsedLedger: _import_ledger, ParsedFundOrders: _import_fund_orders,
                    ParsedHoldings: _import_holdings, ParsedTradeReport: _import_trade_report}[type(parsed)]
        imp = importer(session, user_id, document_id, parsed)
    session.flush()
    return summarize(imp)
