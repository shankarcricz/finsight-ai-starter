"""Read .xlsx / .csv uploads into plain rows, with size limits. Values stay as the file typed them."""
import csv
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MAX_ROWS = 20_000
MAX_COLS = 60
MAX_UNCOMPRESSED = 50 * 1024 * 1024  # zip-bomb guard for .xlsx
SPREADSHEET_EXTENSIONS = ('.xlsx', '.csv')

Cell = str | int | float | datetime | date | None


def read_rows(raw: bytes, filename: str) -> tuple[str, list[list[Cell]]]:
    """Return (sheet name, rows) of the first worksheet. Raises ValueError on bad input."""
    name = filename.lower()
    if name.endswith('.csv'):
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('CSV must be UTF-8 encoded') from exc
        rows = [r[:MAX_COLS] for r in csv.reader(io.StringIO(text))]
        if len(rows) > MAX_ROWS:
            raise ValueError(f'Spreadsheet exceeds {MAX_ROWS} rows')
        return 'csv', [[c if c != '' else None for c in r] for r in rows]
    if not name.endswith('.xlsx'):
        raise ValueError('Spreadsheets must be .xlsx or .csv (old .xls: re-save as .xlsx)')
    if not raw.startswith(b'PK\x03\x04'):
        raise ValueError('Not a valid .xlsx file')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNCOMPRESSED:
                raise ValueError('Spreadsheet is too large when uncompressed')
        import warnings
        import openpyxl
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')  # "Workbook contains no default style" from broker exports
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        # Some exporters (e.g. INDmoney) write a wrong <dimension ref="A1:A1">; read-only mode would trust it
        # and return a single row. Ignore the declared size and read what is actually there.
        ws.reset_dimensions()
        rows: list[list[Cell]] = []
        for row in ws.iter_rows(max_col=MAX_COLS, values_only=True):
            rows.append(list(row))
            if len(rows) > MAX_ROWS:
                raise ValueError(f'Spreadsheet exceeds {MAX_ROWS} rows')
        title = ws.title
        wb.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Unable to read spreadsheet') from exc
    return title, rows


def norm(cell: Cell) -> str:
    """Header text normaliser: 'Clearing\\nCorporation/ Member' -> 'clearing corporation/ member'."""
    return ' '.join(str(cell).split()).lower() if cell is not None else ''


_EMPTY = {'', 'n/a', 'na', '-', '--', 'nil'}


def cell_decimal(cell: Cell) -> Decimal | None:
    """Exact decimal from a spreadsheet cell. Floats go through repr(), which is the shortest string
    that round-trips, so 9084.43 stays 9084.43 (never 9084.4299999...)."""
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, int):
        return Decimal(cell)
    if isinstance(cell, float):
        return Decimal(repr(cell))
    s = str(cell).strip()
    if s.lower() in _EMPTY:
        return None
    s = s.replace(',', '').replace('₹', '').removeprefix('Rs.').removeprefix('INR').strip()
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f'Not a number: {cell!r}') from exc


def cell_text(cell: Cell) -> str | None:
    if cell is None:
        return None
    s = ' '.join(str(cell).split())
    return None if s.lower() in _EMPTY else s
