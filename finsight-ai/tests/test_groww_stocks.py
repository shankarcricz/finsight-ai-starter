from datetime import date
from decimal import Decimal
import pytest
from app import advisor, market_data, portfolio
from app.investment_import import import_investment_sheet, reconcile_holdings, reconcile_trade_report
from app.models import Document, InvestmentImport
from app.parsers.investments import GrowwPnLParser, GrowwStockHoldingsParser
from app.portfolio import xirr
from app.spreadsheets import read_rows
from tests.conftest import USER
from tests.investment_fixtures import GOLDX, groww_holdings_xlsx, groww_pnl_xlsx
from tests.test_advisor_alerts import FakeProvider

D = Decimal


def rows(data: bytes):
    return read_rows(data, 'Stocks_Report.xlsx')[1]


def doc(session, sha):
    d = Document(user_id=USER, category='investment', filename='x.xlsx', sha256=sha, pages=1, indexed_in_rag=False)
    session.add(d)
    session.flush()
    return d.id


def test_holdings_statement_parses_cost_basis():
    p = GrowwStockHoldingsParser().parse(rows(groww_holdings_xlsx()))
    assert (p.platform, p.account_ref, p.as_of) == ('GROWW', 'XXXX1234', date(2026, 11, 20))  # float client code OK
    assert (p.stated_total_value, p.stated_cost_total) == (D('3000.00'), D('3003.60'))
    a = p.rows[0]
    assert (a.isin, a.quantity, a.average_cost, a.cost_value, a.reported_rate, a.reported_value) == (
        'INE001A01036', D('10.0'), D('100.3300'), D('1003.30'), D('120.0000'), D('1200.00'))
    status, checks = reconcile_holdings(p)
    by = {c.name: c for c in checks}
    assert status == 'reconciled' and 'quantity_components' not in by
    assert by['stated_cost_total'].status == 'pass' and 'rounding' in by['stated_cost_total'].detail


def test_pnl_report_lots_charges_and_reconciliation():
    p = GrowwPnLParser().parse(rows(groww_pnl_xlsx()))
    assert (p.period_start, p.period_end) == (date(2026, 4, 1), date(2026, 11, 20))
    assert p.charges == {'Brokerage': D('20.00'), 'STT': D('5.00'), 'Total GST': D('3.60'), 'Total': D('28.60')}
    assert [(l.isin, l.realised, l.pnl) for l in p.lots] == [
        ('INE002B01024', True, D('50.00')), ('INE001A01036', False, D('196.70')), (GOLDX, False, D('-200.00'))]
    status, checks = reconcile_trade_report(p)
    assert status == 'reconciled' and all(c.status == 'pass' for c in checks)


def test_pnl_summary_mismatch_is_flagged():
    status, checks = reconcile_trade_report(GrowwPnLParser().parse(rows(groww_pnl_xlsx(realised_total=99.0))))
    assert status == 'mismatch' and {c.name: c.status for c in checks}['realised_pnl_total'] == 'fail'


def test_xirr_known_values():
    assert xirr([(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]) == pytest.approx(10.0, abs=0.01)
    assert xirr([(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 900.0)]) == pytest.approx(-10.0, abs=0.01)
    assert xirr([(date(2025, 1, 1), -1000.0)]) is None


def test_import_both_reports_then_performance_and_xirr(session):
    h = import_investment_sheet(session, user_id=USER, document_id=doc(session, 'h'), rows=rows(groww_holdings_xlsx()))
    t = import_investment_sheet(session, user_id=USER, document_id=doc(session, 't'), rows=rows(groww_pnl_xlsx()))
    assert (h.kind, h.status) == ('holdings_snapshot', 'imported')
    assert (t.kind, t.status, t.rows_inserted) == ('trade_report', 'imported', 4)  # 3 buys + 1 sell
    assert t.summary['charges_total'] == '28.60'
    # Re-importing an overlapping report adds nothing.
    again = import_investment_sheet(session, user_id=USER, document_id=doc(session, 't2'),
                                    rows=rows(groww_pnl_xlsx()), filename='other.xlsx')
    assert (again.rows_inserted, again.duplicates_skipped) == (0, 4)

    perf = portfolio.performance(session, USER, {'INE001A01036': D('120'), GOLDX: D('18')}, today=date(2026, 11, 20))
    [a] = perf['accounts']
    assert (a['realised_pnl'], a['charges_total']) == ('50.00', '28.60')
    assert a['current_value_open_lots'] == D('3000.00') and a['net_cash_invested'] == D('2953.30')
    flows = [(date(2026, 2, 1), -500.0), (date(2026, 6, 1), 550.0), (date(2026, 3, 15), -1003.3),
             (date(2026, 1, 10), -2000.0), (date(2026, 11, 20), 3000.0)]
    # 3,503.30 in; 550 + 3,000 back -> small annualised gain.
    assert a['xirr_pct'] == xirr(flows) == pytest.approx(1.84, abs=0.01)
    # The mutual fund view must not pick up stock lots.
    assert portfolio.fund_orders(session, USER)['schemes'] == []


def test_xirr_withheld_when_lots_do_not_cover_holdings(session):
    import_investment_sheet(session, user_id=USER, document_id=doc(session, 't'), rows=rows(groww_pnl_xlsx()))
    [a] = portfolio.performance(session, USER, {'INE001A01036': D('120'), GOLDX: D('18')})['accounts']
    assert a['xirr_pct'] is None and 'holdings statement' in a['xirr_unavailable_reason']


def test_live_portfolio_uses_statement_cost_basis(session):
    class P(FakeProvider):
        def search(self, q):
            return [{'symbol': {'INE001A01036': 'ALPHA.NS', GOLDX: 'GOLDX.NS'}.get(q, 'NONE.NS'), 'type': 'EQUITY'}]
    market_data.set_provider(P())
    try:
        import_investment_sheet(session, user_id=USER, document_id=doc(session, 'h'), rows=rows(groww_holdings_xlsx()))
        lp = advisor.live_portfolio(session, USER)
        alpha = next(p for p in lp['positions'] if p['symbol'] == 'ALPHA.NS')
        assert alpha['avg_cost'] == D('100.3300') and alpha['cost_source'] == 'statement'
        assert alpha['cost_basis'] == D('1003.30') and alpha['unrealized_pnl'] == alpha['market_value'] - D('1003.30')
        assert lp['cost_basis_coverage'] == '2 of 2 valued positions have a cost basis'
    finally:
        market_data.set_provider(None)


def test_previously_unsupported_file_is_retried(session):
    d = doc(session, 'u')
    session.add(InvestmentImport(user_id=USER, document_id=d, parser='none', kind='unsupported',
                                 status='unsupported_layout', reconciliation_status='unverifiable'))
    session.flush()
    s = import_investment_sheet(session, user_id=USER, document_id=d, rows=rows(groww_holdings_xlsx()))
    assert s.kind == 'holdings_snapshot' and s.status == 'imported'


def test_resolve_prefers_nse_when_search_only_returns_bse():
    class P(FakeProvider):
        def search(self, q):
            return [{'symbol': 'BHARTIARTL.NS', 'type': 'EQUITY'}] if q == 'BHARTIARTL.NS' else \
                [{'symbol': 'BHARTIARTL.BO', 'type': 'EQUITY'}]
    assert market_data.resolve_symbol('INE397D01024', P())['symbol'] == 'BHARTIARTL.NS'
