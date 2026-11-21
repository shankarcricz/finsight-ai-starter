from datetime import date
from decimal import Decimal
import pytest
from app.parsers.normalize import extract_reference, normalize_merchant, parse_amount, parse_date


@pytest.mark.parametrize('text,expected,marker', [
    ('85,000.00', Decimal('85000.00'), None),
    ('1,23,456.78', Decimal('123456.78'), None),        # Indian lakh grouping
    ('123,456.78', Decimal('123456.78'), None),
    ('INR 50,000.00', Decimal('50000.00'), None),
    ('₹ 1,200.50', Decimal('1200.50'), None),
    ('Rs. 99.00', Decimal('99.00'), None),
    ('1,200.00 Dr', Decimal('1200.00'), 'Dr'),
    ('1,200.00Cr', Decimal('1200.00'), 'Cr'),
    ('(500.00)', Decimal('-500.00'), None),
    ('-42.10', Decimal('-42.10'), None),
    ('0.01', Decimal('0.01'), None),
])
def test_parse_amount(text, expected, marker):
    assert parse_amount(text) == (expected, marker)


@pytest.mark.parametrize('bad', ['abc', '12.5', '1,2,3.00', '1,000', '', '12.345', '1,0000.00'])
def test_parse_amount_rejects_ambiguous_values(bad):
    with pytest.raises(ValueError):
        parse_amount(bad)


def test_parse_amount_is_exact_decimal():
    value, _ = parse_amount('0.10')
    assert isinstance(value, Decimal) and value * 3 == Decimal('0.30')  # float would give 0.30000000000000004


@pytest.mark.parametrize('text,expected', [
    ('2026-01-03', date(2026, 1, 3)),
    ('03/01/2026', date(2026, 1, 3)),  # day-first, Indian convention
    ('03-01-2026', date(2026, 1, 3)),
    ('03.01.2026', date(2026, 1, 3)),
    ('03/01/26', date(2026, 1, 3)),
    ('03-Jan-2026', date(2026, 1, 3)),
    ('03 Jan 2026', date(2026, 1, 3)),
    ('3 January 2026', date(2026, 1, 3)),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_parse_date_default_year_and_errors():
    assert parse_date('01 Jan', default_year=2026) == date(2026, 1, 1)
    with pytest.raises(ValueError):
        parse_date('01 Jan')
    with pytest.raises(ValueError):
        parse_date('31/02/2026')


@pytest.mark.parametrize('desc,merchant', [
    ('HOUSE RENT - UPI', 'HOUSE RENT'),
    ('GROCERY MART - DEBIT CARD', 'GROCERY MART'),
    ('UPI/402112345678/SWIGGY/swiggy@okaxis', 'SWIGGY'),
    ('NEFT-HDFC0001234-ACME TECH PVT LTD', 'ACME TECH PVT LTD'),
])
def test_normalize_merchant(desc, merchant):
    assert normalize_merchant(desc) == merchant


def test_extract_reference():
    assert extract_reference('IMPS REF 123456789012 TO JOHN') == '123456789012'
    assert extract_reference('UPI/402112345678/SWIGGY') == '402112345678'
    assert extract_reference('HOUSE RENT') is None
