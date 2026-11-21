import json
from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
import app.ingest as ingest
from app import portfolio
from app.ingest import extract_pdf_text
from app.investment_import import import_investment_pdf, import_investment_sheet, reconcile_holdings, reconcile_ledger
from app.main import app
from app.models import Document
from app.parsers.investments import DPHoldingParser, GrowwFundOrdersParser, GrowwLedgerParser
from app.spreadsheets import read_rows
from app.tools import make_finance_tools
from tests.conftest import USER
from tests.investment_fixtures import (FAKE_NAME, FAKE_PAN, LEDGER_ROWS, dp_holding_pdf, ledger_xlsx,
                                       mf_orders_xlsx)

D = Decimal
ALPHA = ('1', 'INE001A01036', 'ALPHA POWER LIMITED', '10.000', '', '', '', '', '10.000', '25.50', '255.00')
BETA = ('2', 'INE002B01024', 'BETA BANK LIMITED', '3.000', '', '', '', '2.000', '5.000', '100.00', '500.00')


def layout(pdf: bytes) -> list[str]:
    return extract_pdf_text(pdf).layout


# ---- DP holding statement parser -------------------------------------------------------------

def test_dp_statement_parses_rows_and_header():
    p = DPHoldingParser().parse(layout(dp_holding_pdf([ALPHA, BETA], total='755.00')))
    assert (p.platform, p.account_ref, p.as_of, p.stated_total_value) == ('INDMONEY', 'XXXX1234', date(2026, 11, 19), D('755.00'))
    assert [(r.isin, r.name, r.quantity, r.free_quantity, r.locked_quantity, r.reported_rate, r.reported_value)
            for r in p.rows] == [
        ('INE001A01036', 'ALPHA POWER LIMITED', D('10.000'), D('10.000'), None, D('25.50'), D('255.00')),
        ('INE002B01024', 'BETA BANK LIMITED', D('5.000'), D('3.000'), D('2.000'), D('100.00'), D('500.00'))]
    status, checks = reconcile_holdings(p)
    assert status == 'reconciled' and {c.name: c.status for c in checks} == {
        'quantity_components': 'pass', 'row_valuation': 'pass', 'stated_total_value': 'pass'}


def test_dp_statement_with_inconsistent_total_flags_valuation_not_quantity():
    # Mirrors the real INDmoney quirk: rows sum to 85.76 but the printed total differs.
    row = ('1', 'INE001A01036', 'ALPHA POWER LIMITED', '8.000', '', '', '', '', '8.000', '10.72', '85.76')
    p = DPHoldingParser().parse(layout(dp_holding_pdf([row], total='97.20')))
    status, checks = reconcile_holdings(p)
    by = {c.name: c for c in checks}
    assert status == 'mismatch'
    assert by['quantity_components'].status == 'pass' and by['row_valuation'].status == 'pass'
    assert by['stated_total_value'].status == 'fail' and 'quantities are unaffected' in by['stated_total_value'].detail


def test_dp_statement_wrapped_company_name():
    long = ('1', 'INE001A01036', 'ALPHA POWER AND', '1.000', '', '', '', '', '1.000', '5.00', '5.00')
    pdf = dp_holding_pdf([long], total='5.00', extra_lines=[' ' * 22 + 'INFRASTRUCTURE LIMITED'])
    assert DPHoldingParser().parse(layout(pdf)).rows[0].name == 'ALPHA POWER AND INFRASTRUCTURE LIMITED'


def test_quantity_components_mismatch_is_flagged():
    bad = ('1', 'INE001A01036', 'ALPHA POWER LIMITED', '3.000', '', '', '', '', '5.000', '1.00', '5.00')
    status, checks = reconcile_holdings(DPHoldingParser().parse(layout(dp_holding_pdf([bad], total='5.00'))))
    assert status == 'mismatch' and checks[0].status == 'fail'


# ---- Groww fund ledger ---------------------------------------------------------------------------

def ledger_rows(**kw):
    return read_rows(ledger_xlsx(**kw), 'Groww_Balance_Statement.xlsx')[1]


def test_ledger_parses_chronologically_with_kinds():
    p = GrowwLedgerParser().parse(ledger_rows(), filename='Groww_Balance_Statement.xlsx')
    assert (p.platform, p.account_ref, p.period_start, p.period_end) == ('GROWW', 'XXXX1234', date(2026, 4, 1), date(2026, 11, 20))
    assert [r.kind for r in p.rows] == ['sell_settlement', 'withdrawal', 'deposit', 'buy_settlement', 'withdrawal',
                                        'deposit', 'buy_settlement']
    assert p.rows[-1].debit == D('9084.43') and p.rows[-1].balance == D('915.57')  # exact, not float noise
    assert reconcile_ledger(p)[0] == 'reconciled'


def test_ledger_balance_break_is_detected():
    rows = [list(r) for r in LEDGER_ROWS]
    rows[2][11] = 1.0  # wrong running balance on one row
    status, checks = reconcile_ledger(GrowwLedgerParser().parse(ledger_rows(rows=rows)))
    assert status == 'mismatch' and checks[0].status == 'fail'


def test_ledger_csv_works_too():
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in read_rows(ledger_xlsx(), 'x.xlsx')[1]:
        w.writerow(['' if c is None else c for c in r])
    _, rows = read_rows(buf.getvalue().encode(), 'ledger.csv')
    assert len(GrowwLedgerParser().parse(rows).rows) == 7


# ---- Mutual fund orders ------------------------------------------------------------------------

def test_mf_orders_empty_file_is_zero_rows_not_an_error():
    rows = read_rows(mf_orders_xlsx([]), 'Mutual_Funds_Order_History.xlsx')[1]
    p = GrowwFundOrdersParser().parse(rows, 'GROWW')
    assert p.rows == [] and p.warnings == [] and p.period_start == date(2026, 1, 1)


def test_mf_orders_rows():
    rows = read_rows(mf_orders_xlsx([
        ['Gamma Index Fund Direct Growth', 'PURCHASE', 12.345, 81.0125, 1000.0, '15 Mar 2026'],
        ['Gamma Index Fund Direct Growth', 'REDEEM', 5.0, 90.0, 450.0, '20 Sep 2026'],
        ['Gamma Index Fund Direct Growth', 'MYSTERY', 1.0, 1.0, 1.0, '20 Sep 2026'],
    ]), 'mf.xlsx')[1]
    p = GrowwFundOrdersParser().parse(rows, 'GROWW')
    assert [(r.transaction_type, r.units, r.nav, r.amount, r.order_date) for r in p.rows] == [
        ('buy', D('12.345'), D('81.0125'), D('1000.00'), date(2026, 3, 15)),
        ('sell', D('5.0'), D('90.0'), D('450.00'), date(2026, 9, 20))]
    assert [w.code for w in p.warnings] == ['unknown_order_type']


# ---- persistence + analytics -------------------------------------------------------------------

def doc(session, sha):
    d = Document(user_id=USER, category='investment', filename='f', sha256=sha, pages=1, indexed_in_rag=False)
    session.add(d)
    session.flush()
    return d.id


def import_pdf(session, pdf, sha, hint=None):
    return import_investment_pdf(session, user_id=USER, document_id=doc(session, sha), layout_pages=layout(pdf),
                                 platform_hint=hint)


def test_latest_snapshot_per_account_and_cross_platform_combination(session):
    import_pdf(session, dp_holding_pdf([ALPHA], total='255.00', as_of='31/03/2026'), 'a')
    newer_alpha = ('1', 'INE001A01036', 'ALPHA POWER LIMITED', '12.000', '', '', '', '', '12.000', '25.50', '306.00')
    import_pdf(session, dp_holding_pdf([newer_alpha, BETA], total='806.00', as_of='19/11/2026'), 'b')
    # Same ISIN at another broker account
    import_pdf(session, dp_holding_pdf([ALPHA], total='255.00', as_of='18/11/2026', bo_id='1300000000009999'),
               'c', hint='ZERODHA')
    h = portfolio.current_holdings(session, USER, today=date(2026, 11, 20))
    alpha = next(s for s in h['securities'] if s['isin'] == 'INE001A01036')
    assert alpha['quantity'] == D('22.000')   # 12 (latest INDmoney) + 10 (Zerodha); the March snapshot is ignored
    assert {x['platform'] for x in alpha['holdings']} == {'INDMONEY', 'ZERODHA'}
    assert alpha['reported_value'] == D('561.00')
    assert h['warnings'] == []
    hist = portfolio.holding_history(session, USER, 'INE001A01036')['history']
    assert [x['quantity'] for x in hist] == [D('10.000'), D('10.000'), D('12.000')]


def test_same_day_snapshot_is_marked_duplicate(session):
    first = import_pdf(session, dp_holding_pdf([ALPHA], total='255.00'), 'a')
    again = import_pdf(session, dp_holding_pdf([ALPHA], total='255.00', extra_lines=['  ']), 'b')
    assert first.status == 'imported' and again.status == 'duplicate' and again.rows_inserted == 0
    assert portfolio.current_holdings(session, USER, today=date(2026, 11, 19))['securities'][0]['quantity'] == D('10.000')


def test_stale_and_unreconciled_snapshots_warn(session):
    row = ('1', 'INE001A01036', 'ALPHA POWER LIMITED', '8.000', '', '', '', '', '8.000', '10.72', '85.76')
    import_pdf(session, dp_holding_pdf([row], total='97.20', as_of='31/03/2026'), 'a')
    w = portfolio.current_holdings(session, USER, today=date(2026, 11, 20))['warnings']
    assert any('days old' in x for x in w) and any('mismatch' in x for x in w)


def test_ledger_import_cash_flows_and_overlap_dedupe(session):
    first = import_investment_sheet(session, user_id=USER, document_id=doc(session, 'l1'), rows=ledger_rows(),
                                    filename='Groww_x.xlsx')
    assert (first.status, first.reconciliation_status, first.rows_inserted) == ('imported', 'reconciled', 7)
    overlap = import_investment_sheet(session, user_id=USER, document_id=doc(session, 'l2'),
                                      rows=ledger_rows(rows=LEDGER_ROWS[:3]), filename='Groww_y.xlsx')
    assert (overlap.rows_inserted, overlap.duplicates_skipped) == (0, 3)
    [a] = portfolio.broker_cash_flows(session, USER, 'GROWW')['accounts']
    assert (a['deposits'], a['withdrawals'], a['buy_settlements'], a['sell_settlements'], a['closing_balance']) == (
        D('15000.00'), D('20049.75'), D('44034.68'), D('50000.00'), D('915.57'))
    assert a['net_deposited'] == D('-5049.75')


def test_unrecognised_spreadsheet_is_recorded(session):
    s = import_investment_sheet(session, user_id=USER, document_id=doc(session, 'u'), rows=[['a', 'b'], [1, 2]])
    assert s.status == 'unsupported_layout'


def test_personal_identifiers_are_never_stored(session, db):
    import_investment_sheet(session, user_id=USER, document_id=doc(session, 'l1'), rows=ledger_rows())
    import_investment_sheet(session, user_id=USER, document_id=doc(session, 'm1'),
                            rows=read_rows(mf_orders_xlsx([]), 'm.xlsx')[1], platform_hint='GROWW')
    import_pdf(session, dp_holding_pdf([ALPHA], total='255.00'), 'p1')
    session.commit()
    with db.connect() as c:
        for table in inspect(db).get_table_names():
            for row in c.execute(text(f'SELECT * FROM "{table}"')):
                blob = json.dumps([str(v) for v in row])
                assert FAKE_PAN not in blob and FAKE_NAME not in blob and '9000001234' not in blob, table


# ---- tools + API -------------------------------------------------------------------------------

def test_investment_tools(session):
    import_pdf(session, dp_holding_pdf([ALPHA, BETA], total='755.00'), 'a')
    import_investment_sheet(session, user_id=USER, document_id=doc(session, 'l'), rows=ledger_rows())
    session.commit()
    tools = {t.name: t for t in make_finance_tools(USER)}
    h = json.loads(tools['get_investment_holdings'].invoke({}))
    assert h['positions_count'] == 2 and 'not live prices' in h['provenance']
    f = json.loads(tools['get_broker_cash_flows'].invoke({'platform': 'GROWW'}))
    assert f['accounts'][0]['deposits'] == '15000.00'
    other = json.loads({t.name: t for t in make_finance_tools('intruder')}['get_investment_holdings'].invoke({}))
    assert other['positions_count'] == 0


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ingest, 'index_in_pinecone', lambda chunks, ids: None)
    with TestClient(app) as c:
        yield c


def post(client, data, name, category='investment', **form):
    return client.post('/api/upload', data={'category': category, **form},
                       files={'file': (name, data, 'application/octet-stream')})


def test_api_spreadsheet_and_pdf_uploads(client):
    ledger = ledger_xlsx()  # same bytes for both uploads (openpyxl stamps the save time into each file)
    r = post(client, ledger, 'Groww_Balance_Statement.xlsx')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['indexed_in_rag'] is False and body['structured']['kind'] == 'funds_ledger'
    assert post(client, ledger, 'again.xlsx').json()['duplicate'] is True
    r = post(client, dp_holding_pdf([ALPHA], total='255.00'), 'holdings.pdf', local_only='true')
    assert r.json()['structured']['kind'] == 'holdings_snapshot'
    r = post(client, mf_orders_xlsx([['Gamma Fund', 'SIP', 1.0, 100.0, 100.0, '01 Feb 2026']]), 'mf.xlsx',
             platform='GROWW')
    assert r.json()['structured']['platform'] == 'GROWW'
    assert client.get('/api/investments/holdings').json()['positions_count'] == 1
    assert client.get('/api/investments/cash-flows').json()['accounts'][0]['closing_balance'] == '915.57'
    assert client.get('/api/investments/fund-orders').json()['total_invested'] == '100.00'
    assert len(client.get('/api/investments/imports').json()) == 3


@pytest.mark.parametrize('data,name,category,status', [
    (b'PK\x03\x04broken', 'x.xlsx', 'investment', 400),
    (b'not a zip', 'x.xlsx', 'investment', 400),
    (b'a,b\n1,2', 'x.xls', 'investment', 400),
])
def test_api_rejects_bad_spreadsheets(client, data, name, category, status):
    assert post(client, data, name, category).status_code == status


def test_api_bank_spreadsheet_not_supported(client):
    assert post(client, ledger_xlsx(), 'x.xlsx', category='bank').status_code == 400
