"""Market data: a provider interface, a Yahoo Finance implementation, and deterministic price statistics.

Yahoo's public chart/search endpoints are free and need no key, but they are UNOFFICIAL (no SLA, may
change or rate-limit) and NSE/BSE prices are typically ~15 minutes delayed. Every result carries its
source and timestamp. To use an official feed (Upstox / Angel One SmartAPI / Kite Connect), implement
MarketDataProvider and pass it to set_provider().
"""
import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from app.observability import traceable
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

SOURCE = 'Yahoo Finance (unofficial public endpoint; NSE/BSE prices usually ~15 min delayed)'


@dataclass
class PriceSeries:
    symbol: str
    name: str | None
    currency: str | None
    exchange: str | None
    price: float | None            # latest traded price
    price_time: datetime | None
    dates: list[date] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None


class MarketDataProvider(Protocol):
    def search(self, query: str) -> list[dict[str, Any]]: ...
    def series(self, symbol: str, range_: str = '1y') -> PriceSeries: ...
    def news(self, query: str, count: int = 5) -> list[dict[str, Any]]: ...


class MarketDataError(RuntimeError):
    pass


class _TTLCache:
    def __init__(self):
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            return hit[1] if hit and hit[0] > time.time() else None

    def set(self, key, value, ttl: float):
        with self._lock:
            self._data[key] = (time.time() + ttl, value)


class YahooProvider:
    UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) FinSight/0.1'}

    def __init__(self, session=None):
        import requests
        self._http = session or requests.Session()
        self._cache = _TTLCache()

    @traceable(name='yahoo_finance', run_type='tool', process_outputs=lambda r: {'ok': True},
               process_inputs=lambda i: {'endpoint': i['url'].split('finance.yahoo.com')[-1], 'params': i.get('params')})
    def _get(self, url: str, params: dict) -> dict:
        last_exc = None
        for attempt in range(2):
            try:
                r = self._http.get(url, params=params, headers=self.UA, timeout=12)
                if r.status_code == 404:
                    raise MarketDataError(f'Symbol not found ({params})')
                r.raise_for_status()
                return r.json()
            except MarketDataError:
                raise
            except Exception as exc:  # network / 429 / 5xx: one retry, then give up visibly
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
        raise MarketDataError(f'Market data request failed: {type(last_exc).__name__}')

    def search(self, query: str) -> list[dict[str, Any]]:
        key = ('search', query.upper())
        if (hit := self._cache.get(key)) is not None:
            return hit
        data = self._get('https://query2.finance.yahoo.com/v1/finance/search',
                         {'q': query, 'quotesCount': 8, 'newsCount': 0})
        out = [{'symbol': q.get('symbol'), 'name': q.get('longname') or q.get('shortname'),
                'exchange': q.get('exchange'), 'type': q.get('quoteType')}
               for q in data.get('quotes', []) if q.get('symbol')]
        self._cache.set(key, out, 7 * 86400)
        return out

    def series(self, symbol: str, range_: str = '1y') -> PriceSeries:
        key = ('series', symbol, range_)
        if (hit := self._cache.get(key)) is not None:
            return hit
        data = self._get(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',
                         {'range': range_, 'interval': '1d'})
        results = (data.get('chart') or {}).get('result') or []
        if not results:
            raise MarketDataError(f'No data for {symbol}')
        res = results[0]
        meta = res.get('meta', {})
        stamps = res.get('timestamp') or []
        closes = ((res.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
        pairs = [(datetime.fromtimestamp(t, timezone.utc).date(), float(c)) for t, c in zip(stamps, closes)
                 if c is not None]
        ts = meta.get('regularMarketTime')
        out = PriceSeries(
            symbol=meta.get('symbol', symbol), name=meta.get('longName') or meta.get('shortName'),
            currency=meta.get('currency'), exchange=meta.get('exchangeName'),
            price=meta.get('regularMarketPrice'),
            price_time=datetime.fromtimestamp(ts, timezone.utc) if ts else None,
            dates=[d for d, _ in pairs], closes=[c for _, c in pairs],
            fifty_two_week_high=meta.get('fiftyTwoWeekHigh'), fifty_two_week_low=meta.get('fiftyTwoWeekLow'))
        self._cache.set(key, out, 300)  # 5 minutes: fresh enough for a delayed feed, polite to the endpoint
        return out

    def news(self, query: str, count: int = 5) -> list[dict[str, Any]]:
        key = ('news', query.upper(), count)
        if (hit := self._cache.get(key)) is not None:
            return hit
        data = self._get('https://query2.finance.yahoo.com/v1/finance/search',
                         {'q': query, 'quotesCount': 0, 'newsCount': count})
        out = [{'title': n.get('title'), 'publisher': n.get('publisher'), 'link': n.get('link'),
                'published': datetime.fromtimestamp(n['providerPublishTime'], timezone.utc).isoformat()
                if n.get('providerPublishTime') else None}
               for n in data.get('news', [])[:count] if n.get('title')]
        self._cache.set(key, out, 1800)
        return out


_provider: MarketDataProvider | None = None


def get_provider() -> MarketDataProvider:
    global _provider
    if _provider is None:
        _provider = YahooProvider()
    return _provider


def set_provider(provider: MarketDataProvider | None) -> None:
    global _provider
    _provider = provider


# ---- symbol resolution ------------------------------------------------------------------------

def resolve_symbol(query: str, provider: MarketDataProvider | None = None) -> dict[str, Any] | None:
    """ISIN / NSE symbol / company name -> best equity ticker, preferring NSE, then BSE."""
    provider = provider or get_provider()
    q = query.strip().upper()
    candidates = provider.search(q)
    if not candidates and '.' not in q and ' ' not in q:
        candidates = provider.search(f'{q}.NS')
    equities = [c for c in candidates if c.get('type') in ('EQUITY', 'ETF', 'INDEX', 'MUTUALFUND', None)]
    for c in equities:
        if c['symbol'].endswith('.NS'):
            return c
    for c in equities:
        if c['symbol'].endswith('.BO'):
            # Search sometimes returns only the BSE listing; prefer NSE when the same code trades there.
            nse = c['symbol'][:-3] + '.NS'
            if any(x['symbol'] == nse for x in provider.search(nse)):
                return {**c, 'symbol': nse, 'exchange': 'NSI'}
            return c
    return equities[0] if equities else None


# ---- deterministic statistics -------------------------------------------------------------------

def _ret(closes: list[float], days: int) -> float | None:
    if len(closes) <= days or closes[-days - 1] == 0:
        return None
    return round((closes[-1] / closes[-days - 1] - 1) * 100, 2)


def price_stats(s: PriceSeries) -> dict[str, Any]:
    """Trend/risk facts from daily closes. Floats are fine here: these are descriptive statistics,
    not money owed. Portfolio values use Decimal (see advisor.py)."""
    c = s.closes
    price = s.price if s.price is not None else (c[-1] if c else None)
    out: dict[str, Any] = {
        'symbol': s.symbol, 'name': s.name, 'currency': s.currency, 'exchange': s.exchange,
        'price': price, 'price_time': s.price_time.isoformat() if s.price_time else None,
        'source': SOURCE, 'history_points': len(c),
    }
    if len(c) < 2 or price is None:
        out['note'] = 'Not enough price history for statistics'
        return out
    prev_close = c[-2] if s.price_time and c and s.dates[-1] == s.price_time.date() else c[-1]
    out['previous_close'] = prev_close
    out['day_change_pct'] = round((price / prev_close - 1) * 100, 2) if prev_close else None
    series = c[:-1] + [price] if s.price_time and s.dates[-1] == s.price_time.date() else c + [price]
    out['return_pct'] = {'1w': _ret(series, 5), '1m': _ret(series, 21), '3m': _ret(series, 63),
                         '6m': _ret(series, 126),
                         '1y': round((series[-1] / series[0] - 1) * 100, 2) if series[0] else None}
    sma50 = statistics.fmean(series[-50:]) if len(series) >= 50 else None
    sma200 = statistics.fmean(series[-200:]) if len(series) >= 200 else None
    out['sma50'] = round(sma50, 2) if sma50 else None
    out['sma200'] = round(sma200, 2) if sma200 else None
    if sma50 and sma200:
        out['trend'] = ('uptrend' if price > sma50 > sma200 else 'downtrend' if price < sma50 < sma200 else 'mixed')
    logs = [math.log(b / a) for a, b in zip(series, series[1:]) if a > 0 and b > 0]
    out['volatility_annual_pct'] = round(statistics.stdev(logs) * math.sqrt(252) * 100, 1) if len(logs) > 2 else None
    peak, max_dd = series[0], 0.0
    for p in series:
        peak = max(peak, p)
        max_dd = min(max_dd, p / peak - 1)
    out['max_drawdown_1y_pct'] = round(max_dd * 100, 1)
    hi = s.fifty_two_week_high or max(series)
    lo = s.fifty_two_week_low or min(series)
    out['high_52w'], out['low_52w'] = hi, lo
    out['pct_below_52w_high'] = round((1 - price / hi) * 100, 1) if hi else None
    out['pct_above_52w_low'] = round((price / lo - 1) * 100, 1) if lo else None
    return out


def quote(query: str, provider: MarketDataProvider | None = None) -> dict[str, Any]:
    """Resolve anything (ISIN, symbol, name) and return price + statistics."""
    provider = provider or get_provider()
    match = resolve_symbol(query, provider)
    if match is None:
        raise MarketDataError(f'No listed security found for {query!r}')
    return price_stats(provider.series(match['symbol']))


def to_decimal_price(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(repr(value)).quantize(Decimal('0.01'))
