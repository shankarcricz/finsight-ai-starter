from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from app import advisor, market_data, markets
from app.db import configure_engine, session_scope
from app.investment_import import import_investment_sheet
from app.main import app
from app.models import Document, HoldingPosition, MarketFlow
from app.parsers.investments import IndmoneyUSHoldingsParser
from app.spreadsheets import read_rows
from tests.conftest import USER
from tests.investment_fixtures import indmoney_us_xlsx
from tests.test_advisor_alerts import FakeProvider, series
from tests.test_migrations import upgrade

D = Decimal


def us_rows():
    return read_rows(indmoney_us_xlsx(), 'report.xlsx')[1]


# ---- INDmoney US holdings ------------------------------------------------------------------------

def test_workbook_with_wrong_declared_dimension_is_read_fully():
    """Real INDmoney exports declare <dimension ref="A1:A1"/> although the sheet has many rows."""
    import io, re, zipfile
    src = zipfile.ZipFile(io.BytesIO(indmoney_us_xlsx()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith('xl/worksheets/sheet'):
                data = re.sub(rb'<dimension ref="[^"]+"/>', b'<dimension ref="A1:A1"/>', data)
            z.writestr(item, data)
    _, rows = read_rows(out.getvalue(), 'broken.xlsx')
    assert len(rows) > 6 and IndmoneyUSHoldingsParser().detect(rows) > 0


def test_us_holdings_parse_fractional_usd():
    p = IndmoneyUSHoldingsParser().parse(us_rows())
    assert (p.platform, p.account_ref, p.as_of) == ('INDMONEY', 'XXXX9876', date(2026, 11, 21))
    acme, globx = p.rows
    assert (acme.symbol, acme.isin, acme.currency) == ('ACME', None, 'USD')
    assert acme.quantity == D('0.93130636') and acme.average_cost == D('223.073748')
    assert acme.reported_value == D('229.86') or acme.reported_value == D('229.87')
    assert globx.quantity == D('0.32164891')  # 9 decimals in the file -> rounded to 8


def test_us_holdings_import_reconciles_and_stores_exactly(session):
    d = Document(user_id=USER, category='investment', filename='us.xlsx', sha256='us', pages=1, indexed_in_rag=False)
    session.add(d)
    session.flush()
    s = import_investment_sheet(session, user_id=USER, document_id=d.id, rows=us_rows())
    assert (s.kind, s.platform, s.status, s.reconciliation_status) == ('holdings_snapshot', 'INDMONEY', 'imported', 'reconciled')
    session.commit()
    pos = session.scalars(select(HoldingPosition).order_by(HoldingPosition.source_row)).all()
    assert pos[0].quantity == D('0.93130636') and pos[0].currency == 'USD'  # survives the round trip exactly
    assert pos[0].security.symbol == 'ACME' and pos[0].security.isin is None


def test_live_portfolio_converts_usd_at_live_rate(session):
    class P(FakeProvider):
        def series(self, symbol, range_='1y'):
            if symbol == 'USDINR=X':
                s = series(symbol, 90, 0.01)
                s.price = 90.0
                return s
            s = series(symbol, 100, 0.5)  # last price 224.50 USD
            return s
    market_data.set_provider(P())
    try:
        d = Document(user_id=USER, category='investment', filename='us.xlsx', sha256='us', pages=1, indexed_in_rag=False)
        session.add(d)
        session.flush()
        import_investment_sheet(session, user_id=USER, document_id=d.id, rows=us_rows())
        lp = advisor.live_portfolio(session, USER)
        acme = next(p for p in lp['positions'] if p['symbol'] == 'ACME')
        assert acme['currency'] == 'USD' and acme['fx_rate'] == D('90')
        assert acme['market_value_native'] == D('209.08')            # 0.93130636 x 224.50
        assert acme['market_value'] == D('18817.20')                  # x 90
        assert acme['cost_basis_native'] == D('207.75') and acme['cost_basis'] == D('18697.50')
        assert lp['fx_rates'] == {'USD': D('90.0000')}
        assert lp['total_market_value'] == sum(p['market_value'] for p in lp['positions'])
    finally:
        market_data.set_provider(None)


def test_migration_0007_rescales_existing_rows(tmp_path):
    engine = configure_engine('sqlite:///' + (tmp_path / 'old.sqlite3').as_posix())
    upgrade(engine, '0006')
    with engine.begin() as c:
        c.execute(text("INSERT INTO documents(id,user_id,category,filename,sha256,pages) VALUES('d','u','investment','f','h',1)"))
        c.execute(text("INSERT INTO investment_accounts VALUES('a','u','GROWW','XXXX0001','2026-01-01')"))
        c.execute(text("INSERT INTO securities(id,isin,name,asset_type,created_at) VALUES('s','INE001A01036','A','equity','2026-01-01')"))
        c.execute(text("INSERT INTO investment_imports(id,user_id,document_id,account_id,parser,kind,status,reconciliation_status,"
                       "reconciliation_json,warnings_json,summary_json,rows_found,rows_inserted,duplicates_skipped,created_at) "
                       "VALUES('i','u','d','a','p','holdings_snapshot','imported','reconciled','{}','[]','{}',1,1,0,'2026-01-01')"))
        # Old scales: quantity 3010.0000 -> 30100000 (x10^4); rate 19.3300 -> 193300 (x10^4)
        c.execute(text("INSERT INTO holding_positions(id,user_id,import_id,account_id,security_id,as_of,quantity,reported_rate,average_cost) "
                       "VALUES('p','u','i','a','s','2026-11-20',30100000,193300,248800)"))
    upgrade(engine, 'head')
    with session_scope() as s:
        p = s.get(HoldingPosition, 'p')
        assert (p.quantity, p.reported_rate, p.average_cost, p.currency) == (D('3010'), D('19.33'), D('24.88'), 'INR')
    engine.dispose()


# ---- markets ------------------------------------------------------------------------------------

NSE_DAY = [{'date': date(2026, 11, 20), 'category': 'FII', 'buy': D('23596.40'), 'sell': D('13287.98'), 'net': D('10308.42')},
           {'date': date(2026, 11, 20), 'category': 'DII', 'buy': D('18492.63'), 'sell': D('23352.43'), 'net': D('-4859.80')}]


def test_nse_payload_is_parsed():
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return [{'buyValue': '18492.63', 'category': 'DII', 'date': '20-Nov-2026', 'netValue': '-4859.8', 'sellValue': '23352.43'},
                    {'buyValue': '23596.4', 'category': 'FII/FPI', 'date': '20-Nov-2026', 'netValue': '10308.42', 'sellValue': '13287.98'}]

    class Http:
        calls = []
        def get(self, url, **kw):
            self.calls.append(url)
            return Resp()
    http = Http()
    rows = markets.fetch_nse_fii_dii(http)
    assert http.calls[0] == 'https://www.nseindia.com/'  # cookie priming happens first
    assert {(r['category'], r['net']) for r in rows} == {('DII', D('-4859.80')), ('FII', D('10308.42'))}


def test_refresh_upserts_and_is_rate_limited(session, monkeypatch):
    monkeypatch.setitem(markets._last_attempt, 't', 0.0)
    assert markets.refresh_fii_dii(session, fetcher=lambda: NSE_DAY) == {'refreshed': True, 'rows': 2}
    assert markets.refresh_fii_dii(session, fetcher=lambda: pytest.fail('should be throttled'))['refreshed'] is False
    assert markets.refresh_fii_dii(session, force=True, fetcher=lambda: NSE_DAY)['rows'] == 2  # same day updated, not duplicated
    assert session.scalar(select(text('count(*)')).select_from(MarketFlow)) == 2


def test_refresh_failure_is_reported_not_raised(session, monkeypatch):
    monkeypatch.setitem(markets._last_attempt, 't', 0.0)
    def boom():
        raise ConnectionError('blocked')
    assert markets.refresh_fii_dii(session, fetcher=boom) == {'refreshed': False, 'reason': 'NSE unavailable (ConnectionError)'}


def test_csv_import_wide_and_long_and_monthly_totals(session):
    wide = b'Date,FII Net,DII Net,FII Buy,FII Sell\n01-10-2026,"-1,200.50",900,10000,11200.5\n02-10-2026,300,-100,,\n'
    assert markets.import_flows_csv(session, wide)['imported'] == 4
    long = b'date,category,buy,sell,net\n2026-11-03,FII/FPI,1,2,-1000\n2026-11-03,DII,3,1,2000\n'
    assert markets.import_flows_csv(session, long)['imported'] == 2
    f = markets.flows(session, 90)
    assert f['days_stored'] == 3 and f['collected_since'] == date(2026, 10, 1)
    oct_, nov = f['monthly']
    assert (oct_['fii_net'], oct_['dii_net'], oct_['days']) == (D('-900.50'), D('800.00'), 2)
    assert (nov['fii_net'], nov['dii_net']) == (D('-1000.00'), D('2000.00'))
    assert f['daily'][0]['fii_buy'] == D('10000.00')
    # NSE data overrides an earlier CSV value for the same day.
    markets.upsert_flows(session, [{'date': date(2026, 11, 3), 'category': 'FII', 'net': D('-999.00')}], 'nse')
    assert markets.flows(session)['latest']['fii_net'] == D('-999.00')


@pytest.mark.parametrize('csv_bytes,msg', [(b'', 'header'), (b'foo,bar\n1,2\n', 'date column'), (b'Date,Other\n01-10-2026,5\n', 'No FII/DII rows')])
def test_csv_import_rejects_bad_files(session, csv_bytes, msg):
    with pytest.raises(ValueError, match=msg):
        markets.import_flows_csv(session, csv_bytes)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(markets, 'fetch_nse_fii_dii', lambda: NSE_DAY)
    monkeypatch.setitem(markets._last_attempt, 't', 0.0)
    market_data.set_provider(FakeProvider())
    with TestClient(app) as c:
        yield c
    market_data.set_provider(None)


def test_markets_endpoints(client):
    r = client.get('/api/markets/fii-dii?days=30')
    assert r.status_code == 200
    body = r.json()
    assert body['latest']['fii_net'] == '10308.42' and body['fetch']['refreshed'] is True and body['unit'] == 'INR crore'
    r = client.post('/api/markets/fii-dii/import', files={'file': ('f.csv', b'Date,FII Net,DII Net\n19-11-2026,5,6\n', 'text/csv')})
    assert r.json()['imported'] == 2
    assert client.get('/api/markets/fii-dii?refresh=false').json()['days_stored'] == 2
    n = client.get('/api/markets/index?range=6mo').json()
    assert n['symbol'] == '^NSEI' and len(n['points']) > 100 and 'return_pct' in n['stats']
    assert client.get('/api/markets/index?symbol=AAPL').status_code == 422
