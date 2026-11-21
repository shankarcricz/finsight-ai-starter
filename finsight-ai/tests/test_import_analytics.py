"""End-to-end Pipeline B on the synthetic statement: import -> SQL -> deterministic analytics."""
from datetime import date
from decimal import Decimal
from sqlalchemy import func, select
from app import analytics
from app.models import BankTransactionRecord, Document, FinancialAccount
from app.statement_import import import_statement
from tests.conftest import USER
from tests.pdf_factory import HEADER, make_pdf, row
from app.ingest import extract_pdf_text

D = Decimal


def add_document(session, user=USER, sha='a' * 64) -> str:
    doc = Document(user_id=user, category='bank', filename='statement.pdf', sha256=sha, pages=2)
    session.add(doc)
    session.flush()
    return doc.id


def import_demo(session, layout, user=USER, sha='a' * 64):
    return import_statement(session, user_id=user, document_id=add_document(session, user, sha), layout_pages=layout)


def test_import_persists_reconciled_transactions(session, demo_layout_pages):
    result = import_demo(session, demo_layout_pages)
    assert result.reconciliation_status == 'reconciled'
    assert result.status == 'imported'
    assert (result.transactions_found, result.transactions_inserted, result.duplicates_skipped) == (47, 47, 0)
    assert (result.institution, result.account) == ('DEMO BANK', 'XXXX0042')

    rows = session.scalars(select(BankTransactionRecord)).all()
    assert len(rows) == 47
    rent = next(r for r in rows if r.description == 'HOUSE RENT - UPI')
    assert (rent.amount, rent.category, rent.category_source, rent.merchant) == (D('18000.00'), 'Rent', 'rule', 'HOUSE RENT')
    assert isinstance(rent.amount, Decimal)
    assert rent.document_id == result.document_id and rent.source_page == 1


def test_import_is_idempotent_per_document(session, demo_layout_pages):
    first = import_demo(session, demo_layout_pages)
    again = import_statement(session, user_id=USER, document_id=first.document_id, layout_pages=demo_layout_pages)
    assert again.import_id == first.import_id
    assert session.scalar(select(func.count()).select_from(BankTransactionRecord)) == 47


def statement_pdf(rows):
    return extract_pdf_text(make_pdf([['DEMO BANK', 'Account Number: DEMO-XXXX-0042', HEADER, *rows]])).layout


def test_overlapping_statements_do_not_double_count(session):
    jan = [row('2026-01-03', 'SALARY CREDIT', '-', '85,000.00', '85,000.00'),
           row('2026-01-05', 'HOUSE RENT', '18,000.00', '-', '67,000.00')]
    feb = [row('2026-02-03', 'SALARY CREDIT', '-', '85,000.00', '152,000.00')]
    first = import_statement(session, user_id=USER, document_id=add_document(session, sha='1' * 64),
                             layout_pages=statement_pdf(jan))
    second = import_statement(session, user_id=USER, document_id=add_document(session, sha='2' * 64),
                              layout_pages=statement_pdf(jan + feb))  # Jan rows appear again
    assert first.transactions_inserted == 2
    assert (second.transactions_inserted, second.duplicates_skipped) == (1, 2)
    assert any(w['code'] == 'duplicates_skipped' for w in second.warnings)
    assert session.scalar(select(func.count()).select_from(FinancialAccount)) == 1
    assert analytics.income_expense_summary(session, USER)['income'] == D('170000.00')


def test_identical_rows_within_one_statement_are_both_kept(session):
    rows = [row('2026-01-03', 'TEA STALL', '20.00', '-', ''), row('2026-01-03', 'TEA STALL', '20.00', '-', '')]
    result = import_statement(session, user_id=USER, document_id=add_document(session),
                              layout_pages=statement_pdf(rows))
    assert result.transactions_inserted == 2
    assert result.status == 'needs_review'  # flagged for a human, but nothing silently dropped


def test_unsupported_layout_is_recorded_and_visible(session):
    layout = extract_pdf_text(make_pdf([['Some letter from the bank', 'Dear customer']])).layout
    result = import_statement(session, user_id=USER, document_id=add_document(session), layout_pages=layout)
    assert result.status == 'unsupported_layout'
    assert result.warnings[-1]['code'] == 'unsupported_layout'


# ---- analytics (numbers hand-checked against the synthetic statement) -----------------------

def test_income_expense_summary(session, demo_layout_pages):
    import_demo(session, demo_layout_pages)
    s = analytics.income_expense_summary(session, USER)
    assert s['income'] == D('960000.00')            # 11 salaries + 1 bonus
    assert s['all_debits'] == D('377100.00')
    assert s['net_invested'] == D('110000.00')      # 11 SIPs, excluded from spending
    assert s['spending'] == D('267100.00')
    assert s['savings'] == D('692900.00')
    assert s['savings_rate_pct'] == D('72.18')
    assert s['investment_rate_pct'] == D('11.46')
    assert s['meta']['statements'][0]['reconciliation_status'] == 'reconciled'
    assert s['meta']['data_from'] == date(2026, 1, 3) and s['meta']['data_through'] == date(2026, 11, 19)


def test_summary_date_filter(session, demo_layout_pages):
    import_demo(session, demo_layout_pages)
    jan = analytics.income_expense_summary(session, USER, date(2026, 1, 1), date(2026, 1, 31))
    assert (jan['income'], jan['spending'], jan['net_invested']) == (D('85000.00'), D('25300.00'), D('10000.00'))


def test_rates_are_none_not_zero_without_income(session):
    s = analytics.income_expense_summary(session, USER)
    assert s['income'] == D('0.00') and s['savings_rate_pct'] is None


def test_category_breakdown(session, demo_layout_pages):
    import_demo(session, demo_layout_pages)
    b = analytics.category_breakdown(session, USER)
    amounts = {c['category']: c['amount'] for c in b['categories']}
    assert amounts == {'Rent': D('198000.00'), 'Groceries': D('23900.00'), 'Travel': D('22300.00'),
                       'Shopping': D('10800.00'), 'Other': D('6500.00'), 'Healthcare': D('2500.00'),
                       'Utilities': D('1850.00'), 'Food': D('1250.00')}
    assert b['gross_spending'] == D('267100.00')
    assert 'Investment transfers' not in amounts
    assert b['excluded_transfers']['investment_transfers_out'] == D('110000.00')
    assert b['categories'][0]['share_pct'] == D('74.13')


def test_monthly_cash_flow(session, demo_layout_pages):
    import_demo(session, demo_layout_pages)
    months = analytics.monthly_cash_flow(session, USER)['months']
    assert [m['month'] for m in months] == [f'2026-{m:02d}' for m in range(1, 12)]
    jan = months[0]
    assert (jan['income'], jan['spending'], jan['net_invested'], jan['net_cash_flow']) == (
        D('85000.00'), D('25300.00'), D('10000.00'), D('49700.00'))
    assert sum((m['net_cash_flow'] for m in months), D('0')) == D('582900.00')  # closing - opening


def test_recurring_payments(session, demo_layout_pages):
    import_demo(session, demo_layout_pages)
    r = analytics.recurring_payments(session, USER)
    found = {x['merchant']: x for x in r['recurring']}
    assert set(found) == {'HOUSE RENT', 'SIP NIFTY INDEX FUND'}  # irregular groceries are not "recurring"
    assert found['HOUSE RENT']['typical_amount'] == D('18000.00') and found['HOUSE RENT']['occurrences'] == 11
    assert found['SIP NIFTY INDEX FUND']['is_spending'] is False
    assert r['estimated_monthly_recurring_spend'] == D('18000.00')


def test_spending_trends(session, demo_layout_pages):
    import_demo(session, demo_layout_pages)
    t = analytics.spending_trends(session, USER)
    assert (t['month'], t['previous_month']) == ('2026-11', '2026-10')
    changes = {c['category']: c['change'] for c in t['changes']}
    assert changes == {'Groceries': D('4900.00'), 'Rent': D('0.00'), 'Travel': D('-9800.00')}
    assert t['note'] == '2026-11 data runs through 2026-11-19'


def test_largest_and_investment_transfers(session, demo_layout_pages):
    import_demo(session, demo_layout_pages)
    top = analytics.largest_transactions(session, USER, 'debit', 3)['transactions']
    assert [x['amount'] for x in top] == [D('18000.00')] * 3
    inv = analytics.investment_transfers(session, USER)
    assert inv['total_transferred_out'] == D('110000.00') and inv['share_of_income_pct'] == D('11.46')
    assert analytics.investment_transfers(session, USER, platform='groww')['total_transferred_out'] == D('0.00')


def test_list_transactions_filters(session, demo_layout_pages):
    import_demo(session, demo_layout_pages)
    res = analytics.list_transactions(session, USER, category='Groceries', limit=2)
    assert res['total_matching'] == 5 and len(res['transactions']) == 2
    assert res['sum_debits'] == D('23900.00')
    assert res['transactions'][0]['date'] == date(2026, 11, 19)  # newest first
    assert analytics.list_transactions(session, USER, search="'; DROP TABLE x;--")['total_matching'] == 0


def test_user_isolation(session, demo_layout_pages):
    import_demo(session, demo_layout_pages, user=USER)
    other = analytics.income_expense_summary(session, 'someone-else')
    assert other['income'] == D('0.00') and other['meta']['transactions_considered'] == 0
    assert analytics.list_transactions(session, 'someone-else')['total_matching'] == 0
