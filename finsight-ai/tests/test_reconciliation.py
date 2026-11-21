from datetime import date
from decimal import Decimal
from app.parsers import choose_parser
from app.reconciliation import reconcile
from app.schemas import ParsedStatement, ParsedTransaction, StatementSummary


def txn(day: int, kind: str, amount: str, balance: str | None, desc: str = 'X') -> ParsedTransaction:
    return ParsedTransaction(transaction_date=date(2026, 1, day), description=desc, original_description=desc,
                             amount=Decimal(amount), transaction_type=kind,
                             balance_after_transaction=Decimal(balance) if balance else None,
                             source_page=1, source_row=day, extraction_confidence=0.99)


def statement(txns, **summary) -> ParsedStatement:
    return ParsedStatement(parser='test', summary=StatementSummary(**summary), transactions=txns)


def checks(result):
    return {c.name: c.status for c in result.checks}


def test_demo_statement_reconciles(demo_layout_pages):
    result = reconcile(choose_parser(demo_layout_pages).parse(demo_layout_pages))
    assert result.status == 'reconciled'
    assert checks(result) == {'balance_equation': 'pass', 'stated_total_credits': 'pass',
                              'stated_total_debits': 'pass', 'running_balance': 'pass', 'duplicate_rows': 'pass'}
    assert result.total_credits == Decimal('960000.00') and result.total_debits == Decimal('377100.00')


def test_missing_row_is_detected():
    # Row on day 3 (debit 200) is missing from extraction; closing balance still says 700.
    txns = [txn(2, 'debit', '100.00', '900.00'), txn(4, 'debit', '0.01', '699.99')]
    result = reconcile(statement(txns, opening_balance=Decimal('1000.00'), closing_balance=Decimal('699.99')))
    assert result.status == 'mismatch'
    assert checks(result)['balance_equation'] == 'fail'
    assert checks(result)['running_balance'] == 'fail'
    running = next(c for c in result.checks if c.name == 'running_balance')
    assert '2026-01-04' in running.detail


def test_stated_totals_mismatch_is_flagged():
    txns = [txn(2, 'credit', '100.00', '1100.00')]
    result = reconcile(statement(txns, opening_balance=Decimal('1000.00'), closing_balance=Decimal('1100.00'),
                                 total_credits=Decimal('150.00')))
    assert checks(result)['stated_total_credits'] == 'fail' and result.status == 'mismatch'


def test_opening_balance_can_be_derived_but_equation_is_then_skipped():
    txns = [txn(2, 'debit', '100.00', '900.00'), txn(3, 'credit', '50.00', '950.00')]
    result = reconcile(statement(txns))
    assert result.opening_balance == Decimal('1000.00')
    assert result.opening_balance_source == 'derived_from_first_row'
    assert checks(result)['balance_equation'] == 'skipped'
    assert result.status == 'reconciled'  # running balance continuity still proves completeness


def test_no_balances_at_all_is_unverifiable():
    result = reconcile(statement([txn(2, 'debit', '100.00', None)]))
    assert result.status == 'unverifiable'


def test_duplicate_rows_are_flagged():
    txns = [txn(2, 'debit', '100.00', None, 'TEA'), txn(2, 'debit', '100.00', None, 'TEA')]
    result = reconcile(statement(txns))
    assert checks(result)['duplicate_rows'] == 'fail'
