"""Deterministic normalization of amounts, dates, and descriptions found in bank statements."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_CURRENCY_PREFIX = re.compile(r'^(?:INR|Rs\.?|₹)\s*', re.IGNORECASE)
_DR_CR_SUFFIX = re.compile(r'\s*(Dr|Cr)\.?$', re.IGNORECASE)
# Accepts Indian (1,23,456.78) and Western (123,456.78) grouping; requires exactly 2 decimals.
_AMOUNT_BODY = re.compile(r'^\d{1,3}(?:,\d{2,3})*\.\d{2}$|^\d+\.\d{2}$')
AMOUNT_TOKEN = re.compile(
    r'^\(?-?(?:INR|Rs\.?|₹)?\s*(?:\d{1,3}(?:,\d{2,3})*|\d+)\.\d{2}\)?(?:\s*(?:Dr|Cr)\.?)?$', re.IGNORECASE)
EMPTY_CELL = frozenset({'-', '--', '—', '–', ''})

_DATE_FORMATS = (
    '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%d/%m/%y', '%d-%m-%y',
    '%d-%b-%Y', '%d %b %Y', '%d-%b-%y', '%d %b %y', '%d %B %Y', '%d-%B-%Y', '%b %d, %Y', '%b %d %Y',
)
DATE_TOKEN = re.compile(
    r'^(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2,4})(?=\s|$)')


def parse_amount(text: str) -> tuple[Decimal, str | None]:
    """Parse '1,23,456.78', 'INR 85,000.00', '(500.00)', '1,200.00 Dr'.

    Returns (signed amount, 'Dr'/'Cr' marker or None). Raises ValueError on anything ambiguous.
    """
    s = text.strip()
    negative = False
    if s.startswith('(') and s.endswith(')'):
        negative, s = True, s[1:-1].strip()
    if s.startswith('-'):
        negative, s = True, s[1:].strip()
    s = _CURRENCY_PREFIX.sub('', s)
    marker = None
    m = _DR_CR_SUFFIX.search(s)
    if m:
        marker = m.group(1).capitalize()
        s = s[:m.start()].strip()
    if not _AMOUNT_BODY.match(s):
        raise ValueError(f'Unrecognised amount: {text!r}')
    try:
        value = Decimal(s.replace(',', ''))
    except InvalidOperation as exc:
        raise ValueError(f'Unrecognised amount: {text!r}') from exc
    return (-value if negative else value), marker


def parse_date(text: str, *, default_year: int | None = None) -> date:
    """Parse common Indian statement date formats. Day-first is assumed for numeric dates."""
    s = ' '.join(text.strip().split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    if default_year is not None:  # e.g. '01 Jan' in a period header
        for fmt in ('%d %b', '%d-%b', '%d %B'):
            try:
                return datetime.strptime(f'{s} {default_year}', f'{fmt} %Y').date()
            except ValueError:
                continue
    raise ValueError(f'Unrecognised date: {text!r}')


_RAIL_TOKENS = re.compile(
    r'\b(?:UPI|IMPS|NEFT|RTGS|ACH|NACH|ECS|POS|DEBIT CARD|CREDIT CARD|CARD|ATM|TXN|TRF|TRANSFER TO|'
    r'TRANSFER FROM|BY|TO|INB|MB|VPS|IPS)\b')
_NOISE = re.compile(r'[A-Z0-9._-]+@[A-Z]+|\b[A-Z]*\d[A-Z\d]{5,}\b|[^A-Z& ]')


def normalize_merchant(description: str) -> str:
    """Reduce a narration to a stable merchant key, e.g. 'UPI/4021.../SWIGGY/okaxis' -> 'SWIGGY'."""
    s = description.upper()
    s = _NOISE.sub(' ', s.replace('/', ' '))
    s = _RAIL_TOKENS.sub(' ', s)
    s = ' '.join(s.split())
    return s[:120] or 'UNKNOWN'


_REFERENCE = re.compile(r'\b(?:REF|UTR|RRN|CHQ|TXN)\s*(?:NO\.?)?[\s:#-]*([A-Z0-9]{6,})\b', re.IGNORECASE)
_UPI_REF = re.compile(r'\bUPI[/-](\d{12})\b', re.IGNORECASE)


def extract_reference(description: str) -> str | None:
    m = _REFERENCE.search(description) or _UPI_REF.search(description)
    return m.group(1)[:64] if m else None
