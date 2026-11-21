"""Statement reconciliation: prove the extracted rows account for every rupee on the statement.

Checks (each one reported separately so failures stay visible):
  1. balance_equation  opening + sum(credits) - sum(debits) == closing
  2. stated_totals     extracted credit/debit totals == totals printed on the statement
  3. running_balance   each row: previous balance +/- amount == balance printed on that row
                       (a break usually means a missing or misread row)
  4. duplicate_rows    identical rows inside one statement
"""
from collections import Counter
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel
from app.schemas import ParsedStatement

ZERO = Decimal('0.00')


class Check(BaseModel):
    name: str
    status: Literal['pass', 'fail', 'skipped']
    detail: str
    expected: Decimal | None = None
    actual: Decimal | None = None


class ReconciliationResult(BaseModel):
    status: Literal['reconciled', 'mismatch', 'unverifiable']
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    total_credits: Decimal
    total_debits: Decimal
    opening_balance_source: Literal['statement', 'derived_from_first_row', 'missing']
    checks: list[Check]


def reconcile(statement: ParsedStatement) -> ReconciliationResult:
    s, txns = statement.summary, statement.transactions
    credits = sum((t.amount for t in txns if t.transaction_type == 'credit'), ZERO)
    debits = sum((t.amount for t in txns if t.transaction_type == 'debit'), ZERO)
    checks: list[Check] = []

    opening, opening_source = s.opening_balance, 'statement'
    if opening is None and txns and txns[0].balance_after_transaction is not None:
        first = txns[0]
        signed = first.amount if first.transaction_type == 'credit' else -first.amount
        opening, opening_source = first.balance_after_transaction - signed, 'derived_from_first_row'
    elif opening is None:
        opening_source = 'missing'
    closing = s.closing_balance
    if closing is None and txns and txns[-1].balance_after_transaction is not None:
        closing = txns[-1].balance_after_transaction

    # 1. Balance equation. A derived opening balance makes this check circular, so it is skipped.
    if opening is not None and closing is not None and opening_source == 'statement':
        computed = opening + credits - debits
        checks.append(Check(
            name='balance_equation', status='pass' if computed == closing else 'fail',
            expected=closing, actual=computed,
            detail='opening + credits - debits vs closing balance'))
    else:
        checks.append(Check(name='balance_equation', status='skipped',
                            detail='Opening or closing balance not printed on the statement'))

    # 2. Stated totals
    for label, stated, actual in (('credits', s.total_credits, credits), ('debits', s.total_debits, debits)):
        if stated is None:
            checks.append(Check(name=f'stated_total_{label}', status='skipped',
                                detail=f'Statement does not print total {label}'))
        else:
            checks.append(Check(name=f'stated_total_{label}', status='pass' if stated == actual else 'fail',
                                expected=stated, actual=actual, detail=f'Extracted vs printed total {label}'))

    # 3. Running balance continuity
    breaks: list[str] = []
    prev = opening
    with_balance = 0
    for t in txns:
        if t.balance_after_transaction is None:
            prev = None
            continue
        with_balance += 1
        if prev is not None:
            signed = t.amount if t.transaction_type == 'credit' else -t.amount
            if prev + signed != t.balance_after_transaction:
                breaks.append(f'{t.transaction_date} (page {t.source_page}): expected '
                              f'{prev + signed}, statement shows {t.balance_after_transaction}')
        prev = t.balance_after_transaction
    if with_balance == 0:
        checks.append(Check(name='running_balance', status='skipped', detail='No per-row balances'))
    else:
        checks.append(Check(name='running_balance', status='fail' if breaks else 'pass',
                            detail='; '.join(breaks[:5]) + (f' (+{len(breaks) - 5} more)' if len(breaks) > 5 else '')
                            if breaks else f'{with_balance} row balances are continuous'))

    # 4. Duplicate rows inside the statement (not necessarily wrong, but worth a human look)
    keys = Counter((t.transaction_date, t.amount, t.transaction_type, t.description, t.balance_after_transaction)
                   for t in txns)
    dupes = [k for k, n in keys.items() if n > 1]
    checks.append(Check(name='duplicate_rows', status='fail' if dupes else 'pass',
                        detail=f'{len(dupes)} repeated row(s): ' + ', '.join(f'{k[0]} {k[3]}' for k in dupes[:3])
                        if dupes else 'No repeated rows'))

    if not txns:
        status = 'unverifiable'
    elif any(c.status == 'fail' for c in checks):
        status = 'mismatch'
    elif any(c.status == 'pass' for c in checks if c.name in ('balance_equation', 'running_balance')):
        status = 'reconciled'
    else:
        status = 'unverifiable'
    return ReconciliationResult(status=status, opening_balance=opening, closing_balance=closing,
                                total_credits=credits, total_debits=debits,
                                opening_balance_source=opening_source, checks=checks)
