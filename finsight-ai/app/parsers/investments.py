"""Investment document parsers.

Verified against real exports (structure only; fixtures in tests/ are fictional):
  * DPHoldingParser      - CDSL-style "Depository Holding Statement" PDF (INDmoney / INDstocks)
  * GrowwLedgerParser    - Groww "Statement of Accounts of Funds" (Client Fund Ledger) .xlsx
  * GrowwFundOrdersParser- Groww mutual fund order history .xlsx (header verified; the sample had
                           no rows, so the row format is assumed and every row is validated)
"""
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from app.parsers.normalize import parse_date
from app.schemas import (ExtractionWarning, FundOrderRow, HoldingRow, LedgerRow, LotRow, ParsedFundOrders,
                         ParsedHoldings, ParsedLedger, ParsedTradeReport)
from app.spreadsheets import Cell, cell_decimal, cell_text, norm

_CELL = re.compile(r'\S+(?: \S+)*')
_ISIN = re.compile(r'^IN[A-Z0-9]{9}\d$')
_NUM = re.compile(r'^-?[\d,]*\.?\d+$')


def detect_platform(text: str, hint: str | None = None) -> str:
    if hint and hint.upper() in ('INDMONEY', 'GROWW', 'ZERODHA'):
        return hint.upper()
    t = text.upper()
    if 'INDSTOCKS' in t or 'INDMONEY' in t:
        return 'INDMONEY'
    if 'ZERODHA' in t:
        return 'ZERODHA'
    if 'GROWW' in t or 'NEXTBILLION' in t:
        return 'GROWW'
    return 'UNKNOWN'


def mask(identifier: str | None) -> str | None:
    if not identifier:
        return None
    alnum = re.sub(r'[^A-Za-z0-9]', '', identifier)
    return 'XXXX' + alnum[-4:] if len(alnum) >= 4 else None


def _num(text: str) -> Decimal:
    if not _NUM.match(text):
        raise ValueError(f'Not a number: {text!r}')
    return Decimal(text.replace(',', ''))


# ---- PDF: depository holding statement ---------------------------------------------------------

_DP_COLUMNS = (('free', r'Free\s+Bal'), ('pledged', r'Pldg'), ('demat', r'Demat'), ('remat', r'Remat'),
               ('locked', r'Lock\s*-?In'), ('total', r'Tot(?:al)?\s+Qty'), ('rate', r'Rate'), ('value', r'Value'))


class DPHoldingParser:
    name = 'dp_holding_statement'

    def detect(self, pages: list[str]) -> float:
        text = '\n'.join(pages)
        if re.search(r'HOLDING\s+STATEMENT', text, re.I) and any(self._header(l) for l in text.splitlines()):
            return 0.9
        return 0.0

    @staticmethod
    def _header(line: str) -> dict[str, tuple[int, int]] | None:
        if 'ISIN' not in line:
            return None
        spans = {}
        for key, pattern in _DP_COLUMNS:
            if m := re.search(pattern, line, re.I):
                spans[key] = m.span()
        return spans if {'total', 'value'} <= spans.keys() else None

    def parse(self, pages: list[str], platform_hint: str | None = None) -> ParsedHoldings:
        text = '\n'.join(pages)
        warnings: list[ExtractionWarning] = []
        rows: list[HoldingRow] = []
        stated_total = None
        columns = None
        name_start = 0
        for page_no, page in enumerate(pages, start=1):
            last: HoldingRow | None = None
            for row_no, line in enumerate(page.splitlines(), start=1):
                if (h := self._header(line)) is not None:
                    columns, name_start, last = h, line.find('Company'), None
                    continue
                if columns is None or not line.strip():
                    continue
                if m := re.search(r'\bTotal\s+([\d,]+\.\d+)\s*$', line):
                    stated_total = _num(m.group(1))
                    last = None
                    continue
                cells = [(c.start(), c.end(), c.group()) for c in _CELL.finditer(line)]
                if len(cells) >= 3 and cells[0][2].isdigit() and _ISIN.match(cells[1][2]):
                    try:
                        rows.append(self._row(cells, columns, page_no, row_no))
                        last = rows[-1]
                    except ValueError as exc:
                        warnings.append(ExtractionWarning(code='unparsed_row', page=page_no,
                                                          message=f'{exc} (row {cells[0][2]}, {cells[1][2]})'))
                        last = None
                elif last and cells and cells[0][0] >= name_start - 2 and not any(_NUM.match(c[2]) for c in cells):
                    last.name = f'{last.name} {line.strip()}'[:200]  # wrapped company name
                else:
                    last = None
        if columns is None:
            warnings.append(ExtractionWarning(code='no_table_header', message='No ISIN holdings table header found'))
        as_of = None
        if m := re.search(r'(?:STATEMENT|Holding)\s+as\s+on\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})', text, re.I):
            as_of = parse_date(m.group(1))
        else:
            warnings.append(ExtractionWarning(code='no_as_of_date', message='Statement "as on" date not found'))
        account = re.search(r'\b(?:BO\s*ID|Client\s*ID|DP\s*ID)\s*:?\s*(\d{8,16})', text, re.I)
        return ParsedHoldings(parser=self.name, platform=detect_platform(text, platform_hint),
                              account_ref=mask(account.group(1)) if account else None, as_of=as_of,
                              stated_total_value=stated_total, rows=rows, warnings=warnings)

    @staticmethod
    def _row(cells, columns, page_no, row_no) -> HoldingRow:
        isin, name = cells[1][2], cells[2][2]
        values: dict[str, Decimal] = {}
        for start, end, text in cells[3:]:
            col = min(columns, key=lambda k: min(abs(end - columns[k][1]), abs(start - columns[k][0])))
            if col in values:
                raise ValueError('Two numbers fall in the same column')
            values[col] = _num(text)
        if 'total' not in values:
            raise ValueError('Total quantity missing')
        return HoldingRow(isin=isin, name=name, quantity=values['total'], free_quantity=values.get('free'),
                          pledged_quantity=values.get('pledged'), demat_quantity=values.get('demat'),
                          remat_quantity=values.get('remat'), locked_quantity=values.get('locked'),
                          reported_rate=values.get('rate'), reported_value=values.get('value'),
                          source_page=page_no, source_row=row_no)


# ---- Spreadsheet helpers ------------------------------------------------------------------------

def _find_header(rows: list[list[Cell]], required: set[str]) -> tuple[int, dict[str, int]] | None:
    for i, row in enumerate(rows[:40]):
        names = {norm(c): j for j, c in enumerate(row) if c is not None}
        if required <= names.keys():
            return i, names
    return None


def _cell_date(cell: Cell) -> date | None:
    if isinstance(cell, datetime):
        return cell.date()
    if isinstance(cell, date):
        return cell
    text = cell_text(cell)
    return parse_date(text) if text else None


def _metadata(rows: list[list[Cell]], until: int) -> dict[str, str]:
    """Label/value pairs above the table (e.g. 'Unique Client Code' -> '8951...')."""
    meta = {}
    for row in rows[:until]:
        cells = [c for c in row if c is not None and c != '']
        if len(cells) >= 2:
            v = cells[1]
            # Excel stores numeric IDs as floats: 8951774183.0 must stay '8951774183'.
            meta[norm(cells[0])] = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    return meta


def _find_header_from(rows: list[list[Cell]], required: set[str], start: int = 0) -> tuple[int, dict[str, int]] | None:
    for i in range(start, len(rows)):
        names = {norm(c): j for j, c in enumerate(rows[i]) if c is not None}
        if required <= names.keys():
            return i, names
    return None


def _money(cell: Cell) -> Decimal | None:
    v = cell_decimal(cell)
    return None if v is None else v.quantize(Decimal('0.01'))  # Excel float noise: 58183.299999999996 -> 58183.30


def _price(cell: Cell) -> Decimal | None:
    v = cell_decimal(cell)
    return None if v is None else v.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)


def _qty(cell: Cell) -> Decimal | None:
    v = cell_decimal(cell)
    return None if v is None else v.quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)


def _table(rows: list[list[Cell]], header: int, cols: dict[str, int]):
    """Yield (spreadsheet row number, getter) for rows under a header until the first blank row."""
    for i in range(header + 1, len(rows)):
        row = rows[i]
        if not row or cell_text(row[0]) is None:
            return
        yield i + 1, (lambda key, row=row: row[cols[key]] if key in cols and cols[key] < len(row) else None)


def _banner(rows: list[list[Cell]], until: int) -> str:
    return ' '.join(str(c) for row in rows[:until] for c in row if c is not None)


# ---- Groww fund ledger ---------------------------------------------------------------------------

_LEDGER_REQUIRED = {'transaction date', 'segment type', 'debit (rs.)', 'credit (rs.)', 'balance (rs.)'}


def classify_ledger(segment: str, txn_type: str, debit: Decimal, credit: Decimal) -> str:
    seg, tt = segment.upper(), txn_type.lower()
    if 'CHARGE' in seg or 'FEE' in seg or tt in ('charges', 'fee'):
        return 'charges'
    if 'SETTLEMENT' in seg or tt == 'bill':
        return 'buy_settlement' if debit > 0 else 'sell_settlement'
    if debit > 0 and ('WITHDRAW' in seg or 'PAYOUT' in seg):
        return 'withdrawal'
    if credit > 0 and (seg in ('UPI', 'NEFT', 'IMPS', 'RTGS', 'NETBANKING') or 'DEPOSIT' in seg or 'PAYIN' in seg):
        return 'deposit'
    return 'other'


class GrowwLedgerParser:
    name = 'groww_funds_ledger'

    def detect(self, rows: list[list[Cell]]) -> float:
        return 0.9 if _find_header(rows, _LEDGER_REQUIRED) else 0.0

    def parse(self, rows: list[list[Cell]], platform_hint: str | None = None, filename: str = '') -> ParsedLedger:
        found = _find_header(rows, _LEDGER_REQUIRED)
        assert found
        h, cols = found
        warnings: list[ExtractionWarning] = []
        meta = _metadata(rows, h)
        banner = _banner(rows, h)
        out: list[LedgerRow] = []

        def get(row, key):
            j = cols.get(key)
            return row[j] if j is not None and j < len(row) else None

        for i, row in enumerate(rows[h + 1:], start=h + 2):  # 1-based spreadsheet row numbers
            first = cell_text(row[0]) if row else None
            if not first:
                continue
            if first.lower().startswith('note'):
                break
            try:
                debit = cell_decimal(get(row, 'debit (rs.)')) or Decimal('0')
                credit = cell_decimal(get(row, 'credit (rs.)')) or Decimal('0')
                segment = cell_text(get(row, 'segment type')) or ''
                txn_type = cell_text(get(row, 'transaction type')) or ''
                kind = classify_ledger(segment, txn_type, debit, credit)
                out.append(LedgerRow(
                    entry_date=_cell_date(get(row, 'transaction date')),
                    settlement_date=_cell_date(get(row, 'settlement date')),
                    segment_type=segment[:40], transaction_type=txn_type[:40], kind=kind,
                    reference=(cell_text(get(row, 'voucher no.')) or '')[:64] or None,
                    debit=debit.quantize(Decimal('0.01')), credit=credit.quantize(Decimal('0.01')),
                    balance=b.quantize(Decimal('0.01')) if (b := cell_decimal(get(row, 'balance (rs.)'))) is not None else None,
                    source_row=i))
                if kind == 'other':
                    warnings.append(ExtractionWarning(code='unclassified_entry',
                                                      message=f'Row {i}: unrecognised segment {segment!r}'))
            except (ValueError, TypeError) as exc:
                warnings.append(ExtractionWarning(code='unparsed_row', message=f'Row {i}: {exc}'))
        if len(out) > 1 and out[0].entry_date > out[-1].entry_date:
            out.reverse()  # Groww lists newest first; reconciliation needs oldest first
        period_start = period_end = None
        if m := re.search(r'period from (\d{1,2}/\d{1,2}/\d{4}) to (\d{1,2}/\d{1,2}/\d{4})', banner, re.I):
            period_start, period_end = parse_date(m.group(1)), parse_date(m.group(2))
        platform = detect_platform(f'{banner} {filename} ' + ' '.join(r.segment_type for r in out), platform_hint)
        return ParsedLedger(parser=self.name, platform=platform, account_ref=mask(meta.get('unique client code')),
                            period_start=period_start, period_end=period_end, rows=out, warnings=warnings)


# ---- Groww mutual fund orders ------------------------------------------------------------------

_MF_REQUIRED = {'scheme name', 'transaction type', 'units', 'nav', 'amount', 'date'}
_BUY = re.compile(r'PURCHASE|BUY|SIP|SWITCH[ -]?IN|INVEST', re.I)
_SELL = re.compile(r'REDEEM|REDEMPTION|SELL|SWITCH[ -]?OUT|WITHDRAW', re.I)


class GrowwFundOrdersParser:
    name = 'groww_mf_orders'

    def detect(self, rows: list[list[Cell]]) -> float:
        return 0.8 if _find_header(rows, _MF_REQUIRED) else 0.0

    def parse(self, rows: list[list[Cell]], platform_hint: str | None = None, filename: str = '') -> ParsedFundOrders:
        found = _find_header(rows, _MF_REQUIRED)
        assert found
        h, cols = found
        banner = _banner(rows, h)
        warnings: list[ExtractionWarning] = []
        out: list[FundOrderRow] = []
        for i, row in enumerate(rows[h + 1:], start=h + 2):
            first = cell_text(row[cols['scheme name']]) if len(row) > cols['scheme name'] else None
            if not first:
                continue
            if 'NO TRANSACTIONS' in first.upper():
                break
            raw_type = cell_text(row[cols['transaction type']]) or ''
            kind = 'sell' if _SELL.search(raw_type) else 'buy' if _BUY.search(raw_type) else None
            if kind is None:
                warnings.append(ExtractionWarning(code='unknown_order_type', message=f'Row {i}: {raw_type!r} skipped'))
                continue
            try:
                order = FundOrderRow(
                    scheme_name=first, transaction_type=kind, units=cell_decimal(row[cols['units']]),
                    nav=cell_decimal(row[cols['nav']]),
                    amount=cell_decimal(row[cols['amount']]).quantize(Decimal('0.01')),
                    order_date=_cell_date(row[cols['date']]), source_row=i)
            except (ValueError, TypeError, AttributeError) as exc:
                warnings.append(ExtractionWarning(code='unparsed_row', message=f'Row {i}: {exc}'))
                continue
            if order.units and order.nav and abs(order.units * order.nav - order.amount) > order.amount * Decimal('0.01'):
                warnings.append(ExtractionWarning(code='units_nav_mismatch',
                                                  message=f'Row {i}: units x NAV differs from amount by more than 1%'))
            out.append(order)
        period_start = period_end = None
        if m := re.search(r'FROM\s+([A-Za-z]{3} \d{1,2} \d{4})\s+TO\s+([A-Za-z]{3} \d{1,2} \d{4})', banner, re.I):
            period_start, period_end = parse_date(m.group(1)), parse_date(m.group(2))
        platform = detect_platform(f'{banner} {filename}', platform_hint)
        return ParsedFundOrders(parser=self.name, platform=platform, account_ref=None, period_start=period_start,
                                period_end=period_end, rows=sorted(out, key=lambda r: r.order_date), warnings=warnings)


# ---- Groww stock holdings statement (with average buy price) -------------------------------------

_GH_REQUIRED = {'stock name', 'isin', 'quantity', 'average buy price', 'closing price', 'closing value'}


class GrowwStockHoldingsParser:
    name = 'groww_stock_holdings'

    def detect(self, rows: list[list[Cell]]) -> float:
        return 0.9 if _find_header(rows, _GH_REQUIRED) else 0.0

    def parse(self, rows: list[list[Cell]], platform_hint: str | None = None, filename: str = '') -> ParsedHoldings:
        h, cols = _find_header(rows, _GH_REQUIRED)
        meta, banner = _metadata(rows, h), _banner(rows, h)
        warnings: list[ExtractionWarning] = []
        out: list[HoldingRow] = []
        for i, get in _table(rows, h, cols):
            try:
                out.append(HoldingRow(
                    isin=cell_text(get('isin')), name=cell_text(get('stock name'))[:200],
                    quantity=_qty(get('quantity')), average_cost=_price(get('average buy price')),
                    cost_value=_money(get('buy value')), reported_rate=_price(get('closing price')),
                    reported_value=_money(get('closing value')), source_page=1, source_row=i))
            except (ValueError, TypeError) as exc:
                warnings.append(ExtractionWarning(code='unparsed_row', message=f'Row {i}: {exc}'))
        as_of = None
        if m := re.search(r'as on (\d{1,2}[-/]\d{1,2}[-/]\d{4})', banner, re.I):
            as_of = parse_date(m.group(1))
        # These layouts are Groww exports; the files themselves never say "Groww".
        platform = detect_platform(f'{banner} {filename}', platform_hint)
        return ParsedHoldings(parser=self.name, platform='GROWW' if platform == 'UNKNOWN' else platform,
                              account_ref=mask(meta.get('unique client code')), as_of=as_of,
                              stated_total_value=_money(meta.get('closing value')),
                              stated_cost_total=_money(meta.get('invested value')), rows=out, warnings=warnings)


# ---- Groww stocks P&L report (realised trades + open lots + charges) ------------------------------

_REALISED = {'stock name', 'isin', 'quantity', 'buy date', 'buy price', 'buy value', 'sell date', 'sell price',
             'sell value', 'realised p&l'}
_OPEN = {'stock name', 'isin', 'quantity', 'buy date', 'buy price', 'buy value', 'closing date', 'closing price',
         'closing value', 'unrealised p&l'}


class GrowwPnLParser:
    name = 'groww_stock_pnl'

    def detect(self, rows: list[list[Cell]]) -> float:
        return 0.95 if _find_header_from(rows, _REALISED) or _find_header_from(rows, _OPEN) else 0.0

    def parse(self, rows: list[list[Cell]], platform_hint: str | None = None, filename: str = '') -> ParsedTradeReport:
        warnings: list[ExtractionWarning] = []
        first = min(x[0] for x in (_find_header_from(rows, _REALISED), _find_header_from(rows, _OPEN)) if x)
        meta, banner = _metadata(rows, first), _banner(rows, first)
        charges: dict[str, Decimal] = {}
        in_charges = False
        for row in rows[:first]:
            label = cell_text(row[0]) if row else None
            if label is None:
                continue
            if norm(label) == 'charges':
                in_charges = True
                continue
            if in_charges and len(row) > 1:
                try:
                    if (v := _money(row[1])) is not None:
                        charges[label] = v
                except ValueError:
                    pass
            elif in_charges and norm(label) != 'charges':
                in_charges = norm(label) not in ('realised trades', 'unrealised trades')
        lots: list[LotRow] = []
        for required, realised in ((_REALISED, True), (_OPEN, False)):
            found = _find_header_from(rows, required)
            if not found:
                continue
            h, cols = found
            for i, get in _table(rows, h, cols):
                try:
                    lots.append(LotRow(
                        isin=cell_text(get('isin')), name=cell_text(get('stock name'))[:200],
                        quantity=cell_decimal(get('quantity')), buy_date=_cell_date(get('buy date')),
                        buy_price=_price(get('buy price')), buy_value=_money(get('buy value')),
                        sell_date=_cell_date(get('sell date' if realised else 'closing date')),
                        sell_price=_price(get('sell price' if realised else 'closing price')),
                        sell_value=_money(get('sell value' if realised else 'closing value')),
                        pnl=_money(get('realised p&l' if realised else 'unrealised p&l')),
                        realised=realised, source_row=i))
                except (ValueError, TypeError) as exc:
                    warnings.append(ExtractionWarning(code='unparsed_row', message=f'Row {i}: {exc}'))
        period_start = period_end = None
        if m := re.search(r'from (\d{1,2}-\d{1,2}-\d{4}) to (\d{1,2}-\d{1,2}-\d{4})', banner, re.I):
            period_start, period_end = parse_date(m.group(1)), parse_date(m.group(2))
        platform = detect_platform(f'{banner} {filename}', platform_hint)
        return ParsedTradeReport(
            parser=self.name, platform='GROWW' if platform == 'UNKNOWN' else platform,
            account_ref=mask(meta.get('unique client code')), period_start=period_start, period_end=period_end,
            stated_realised_pnl=_money(meta.get('realised p&l')),
            stated_unrealised_pnl=_money(meta.get('unrealised p&l')), charges=charges, lots=lots, warnings=warnings)


# ---- INDmoney US stocks holdings report (USD, fractional shares, tickers instead of ISINs) ----------

_US_REQUIRED = {'symbol', 'name', 'quantity', '($) cost price', '($) market price', '($) market value'}


class IndmoneyUSHoldingsParser:
    name = 'indmoney_us_holdings'

    def detect(self, rows: list[list[Cell]]) -> float:
        return 0.9 if _find_header(rows, _US_REQUIRED) else 0.0

    def parse(self, rows: list[list[Cell]], platform_hint: str | None = None, filename: str = '') -> ParsedHoldings:
        h, cols = _find_header(rows, _US_REQUIRED)
        meta = _metadata(rows, h)
        warnings: list[ExtractionWarning] = []
        out: list[HoldingRow] = []
        for i, get in _table(rows, h, cols):
            try:
                out.append(HoldingRow(
                    symbol=cell_text(get('symbol')).upper(), name=cell_text(get('name'))[:200], currency='USD',
                    quantity=_qty(get('quantity')), average_cost=_price(get('($) cost price')),
                    cost_value=_money(get('($) invested value')), reported_rate=_price(get('($) market price')),
                    reported_value=_money(get('($) market value')), source_page=1, source_row=i))
            except (ValueError, TypeError, AttributeError) as exc:
                warnings.append(ExtractionWarning(code='unparsed_row', message=f'Row {i}: {exc}'))
        as_of = None
        if raw := meta.get('as of date'):
            try:
                as_of = parse_date(raw[:10])
            except ValueError:
                warnings.append(ExtractionWarning(code='no_as_of_date', message=f'Unreadable as-of date {raw!r}'))
        platform = detect_platform(filename, platform_hint)
        return ParsedHoldings(parser=self.name, platform='INDMONEY' if platform == 'UNKNOWN' else platform,
                              account_ref=mask(meta.get('account number')), as_of=as_of, stated_total_value=None,
                              rows=out, warnings=warnings)


SHEET_PARSERS = (GrowwPnLParser(), GrowwStockHoldingsParser(), IndmoneyUSHoldingsParser(), GrowwLedgerParser(),
                 GrowwFundOrdersParser())
