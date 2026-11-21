"""Market-wide data: FII/DII cash-market flows and NIFTY 50 history.

FII/DII source: NSE's public JSON used by nseindia.com/reports/fii-dii (needs session cookies from the
home page; unofficial, latest trading day only). We store each day we see, so history accumulates from the
day collection starts; a CSV import fills in the past. Values are INR crore, combined NSE+BSE+MSEI.
Fetched at most once per 30 minutes; please keep it low-frequency (personal use).
"""
import csv
import io
import logging
import threading
import time
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.market_data import MarketDataError, MarketDataProvider, get_provider, price_stats
from app.models import MarketFlow
from app.observability import only, traceable

log = logging.getLogger(__name__)
NSE_SOURCE = 'NSE India (FII/DII trading activity; unofficial endpoint, INR crore)'
_BROWSER = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/124.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*', 'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.nseindia.com/reports/fii-dii'}
_last_attempt = {'t': 0.0}
_lock = threading.Lock()
MIN_INTERVAL = 30 * 60


def _dec(text) -> Decimal | None:
    if text is None or str(text).strip() in ('', '-'):
        return None
    try:
        return Decimal(str(text).replace(',', '').strip()).quantize(Decimal('0.01'))
    except InvalidOperation as exc:
        raise ValueError(f'Not a number: {text!r}') from exc


@traceable(name='nse_fii_dii', run_type='tool', process_inputs=lambda i: {},
           process_outputs=lambda rows: {'rows': len(rows), 'dates': sorted({r['date'].isoformat() for r in rows})})
def fetch_nse_fii_dii(http=None) -> list[dict]:
    """Return [{'date', 'category' FII|DII, 'buy', 'sell', 'net'}] for the latest trading day."""
    import requests
    s = http or requests.Session()
    s.get('https://www.nseindia.com/', headers=_BROWSER, timeout=15)  # sets the cookies the API requires
    r = s.get('https://www.nseindia.com/api/fiidiiTradeReact', headers=_BROWSER, timeout=15)
    r.raise_for_status()
    out = []
    for row in r.json():
        cat = 'FII' if 'FII' in row.get('category', '').upper() or 'FPI' in row.get('category', '').upper() else \
            'DII' if 'DII' in row.get('category', '').upper() else None
        if cat is None:
            continue
        out.append({'date': datetime.strptime(row['date'], '%d-%b-%Y').date(), 'category': cat,
                    'buy': _dec(row.get('buyValue')), 'sell': _dec(row.get('sellValue')),
                    'net': _dec(row.get('netValue'))})
    return out


def upsert_flows(session: Session, rows: list[dict], source: str) -> int:
    changed = 0
    for r in rows:
        if r.get('net') is None:
            continue
        existing = session.scalar(select(MarketFlow).where(MarketFlow.flow_date == r['date'],
                                                           MarketFlow.category == r['category']))
        if existing is None:
            session.add(MarketFlow(flow_date=r['date'], category=r['category'], buy_value=r.get('buy'),
                                   sell_value=r.get('sell'), net_value=r['net'], source=source))
            changed += 1
        elif existing.source != 'nse' or source == 'nse':  # NSE data wins over CSV imports
            existing.buy_value, existing.sell_value, existing.net_value = r.get('buy'), r.get('sell'), r['net']
            existing.source, changed = source, changed + 1
    session.flush()
    return changed


@traceable(name='refresh_fii_dii', process_inputs=only('force'))
def refresh_fii_dii(session: Session, *, force: bool = False, fetcher=None) -> dict:
    with _lock:
        if not force and time.time() - _last_attempt['t'] < MIN_INTERVAL:
            return {'refreshed': False, 'reason': 'fetched recently'}
        _last_attempt['t'] = time.time()
    try:
        rows = (fetcher or fetch_nse_fii_dii)()  # looked up at call time so tests can swap it
    except Exception as exc:
        log.warning('FII/DII fetch failed: %s', type(exc).__name__)
        return {'refreshed': False, 'reason': f'NSE unavailable ({type(exc).__name__})'}
    return {'refreshed': True, 'rows': upsert_flows(session, rows, 'nse')}


@traceable(name='import_fii_dii_csv', process_inputs=lambda i: {'size_bytes': len(i.get('raw') or b'')},
           process_outputs=lambda r: {'rows_read': r['rows_read'], 'imported': r['imported'], 'errors': len(r['errors'])})
def import_flows_csv(session: Session, raw: bytes) -> dict:
    """Backfill from CSV. Accepted layouts (headers case-insensitive, INR crore):
       long: date, category (FII/FPI or DII), [buy], [sell], net
       wide: date, fii net, dii net  [+ fii buy, fii sell, dii buy, dii sell]"""
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('CSV must be UTF-8') from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError('CSV has no header row')
    cols = {' '.join(f.lower().replace('_', ' ').split()): f for f in reader.fieldnames}
    date_col = next((v for k, v in cols.items() if 'date' in k), None)
    if not date_col:
        raise ValueError('CSV needs a date column')

    def find(*words):
        return next((v for k, v in cols.items() if all(w in k for w in words)), None)
    rows, errors = [], []
    for n, rec in enumerate(reader, start=2):
        if n > 20_002:
            raise ValueError('CSV too large (max 20,000 rows)')
        try:
            d = _parse_any_date(rec[date_col])
            if cat_col := find('category'):
                cat = 'FII' if any(x in rec[cat_col].upper() for x in ('FII', 'FPI')) else 'DII'
                rows.append({'date': d, 'category': cat, 'buy': _dec(rec.get(find('buy') or '', None)),
                             'sell': _dec(rec.get(find('sell') or '', None)), 'net': _dec(rec[find('net')])})
            else:
                for cat, keys in (('FII', ('fii', 'fpi')), ('DII', ('dii',))):
                    net_col = next((find(k, 'net') for k in keys if find(k, 'net')), None)
                    if net_col:
                        buy_col = next((find(k, 'buy') for k in keys if find(k, 'buy')), None)
                        sell_col = next((find(k, 'sell') for k in keys if find(k, 'sell')), None)
                        rows.append({'date': d, 'category': cat, 'net': _dec(rec[net_col]),
                                     'buy': _dec(rec[buy_col]) if buy_col else None,
                                     'sell': _dec(rec[sell_col]) if sell_col else None})
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f'row {n}: {exc}')
    if not rows:
        raise ValueError('No FII/DII rows found. ' + '; '.join(errors[:3]))
    return {'imported': upsert_flows(session, rows, 'csv_import'), 'rows_read': len(rows), 'errors': errors[:10]}


def _parse_any_date(text: str) -> date:
    from app.parsers.normalize import parse_date
    return parse_date(text.strip())


def flows(session: Session, days: int = 90) -> dict[str, Any]:
    all_rows = session.scalars(select(MarketFlow).order_by(MarketFlow.flow_date)).all()
    by_day: dict[date, dict] = defaultdict(dict)
    for r in all_rows:
        by_day[r.flow_date][r.category] = r
    dates = sorted(by_day)
    def day_dict(d):
        f, i = by_day[d].get('FII'), by_day[d].get('DII')
        return {'date': d, 'fii_net': f.net_value if f else None, 'dii_net': i.net_value if i else None,
                'fii_buy': f.buy_value if f else None, 'fii_sell': f.sell_value if f else None,
                'dii_buy': i.buy_value if i else None, 'dii_sell': i.sell_value if i else None}
    daily = [day_dict(d) for d in dates[-days:]]
    months: dict[str, dict] = {}
    for d in dates:
        m = months.setdefault(d.strftime('%Y-%m'), {'month': d.strftime('%Y-%m'), 'fii_net': Decimal('0.00'),
                                                      'dii_net': Decimal('0.00'), 'days': 0})
        m['days'] += 1
        for cat, key in (('FII', 'fii_net'), ('DII', 'dii_net')):
            if cat in by_day[d]:
                m[key] += by_day[d][cat].net_value
    return {'daily': daily, 'monthly': list(months.values()), 'latest': day_dict(dates[-1]) if dates else None,
            'collected_since': dates[0] if dates else None, 'days_stored': len(dates), 'unit': 'INR crore',
            'source': NSE_SOURCE,
            'note': 'History builds up from the first day collected (NSE publishes only the latest day). Import a '
                    'CSV to backfill. Monthly totals only include stored days.'}


RANGES = {'1mo': '1mo', '6mo': '6mo', '1y': '1y', '5y': '5y'}


def index_history(symbol: str = '^NSEI', range_: str = '1y', provider: MarketDataProvider | None = None) -> dict:
    provider = provider or get_provider()
    s = provider.series(symbol, RANGES.get(range_, '1y'))
    return {'symbol': symbol, 'name': s.name or 'NIFTY 50', 'range': range_,
            'points': [{'date': d, 'close': round(c, 2)} for d, c in zip(s.dates, s.closes)],
            'stats': price_stats(provider.series(symbol, '1y'))}
