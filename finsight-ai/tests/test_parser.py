from datetime import date
from decimal import Decimal
import pytest
from app.ingest import extract_pdf_text
from app.parsers import choose_parser
from tests.pdf_factory import HEADER, make_pdf, row


def parse_lines(lines: list[str] | list[list[str]]):
    pages = lines if lines and isinstance(lines[0], list) else [lines]
    layout = extract_pdf_text(make_pdf(pages)).layout
    parser = choose_parser(layout)
    assert parser is not None
    return parser.parse(layout)


def test_demo_statement_extracts_every_row_and_header(demo_layout_pages):
    parsed = choose_parser(demo_layout_pages).parse(demo_layout_pages)
    s = parsed.summary
    assert parsed.parser == 'generic_table'
    assert (s.institution, s.account_identifier, s.currency) == ('DEMO BANK', 'XXXX0042', 'INR')
    assert (s.period_start, s.period_end) == (date(2026, 1, 1), date(2026, 11, 20))
    assert s.opening_balance == Decimal('50000.00') and s.closing_balance == Decimal('632900.00')
    assert s.total_credits == Decimal('960000.00') and s.total_debits == Decimal('377100.00')
    assert len(parsed.transactions) == 47
    assert parsed.warnings == []

    first, last = parsed.transactions[0], parsed.transactions[-1]
    assert (first.transaction_date, first.transaction_type, first.amount) == (date(2026, 1, 3), 'credit', Decimal('85000.00'))
    assert first.description == 'SALARY CREDIT - ACME TECH' and first.source_page == 1
    assert (last.transaction_date, last.amount, last.balance_after_transaction) == (
        date(2026, 11, 19), Decimal('4900.00'), Decimal('632900.00'))
    assert last.source_page == 2
    assert all(t.extraction_confidence >= 0.99 for t in parsed.transactions)


def test_collapsed_empty_cells_use_column_position_and_multiline_narration():
    parsed = parse_lines([
        'SAMPLE BANK - STATEMENT', 'Account No: 1234567890123', 'Opening Balance: 10,000.00', HEADER,
        row('03/01/2026', 'SALARY ACME', '', '50,000.00', '60,000.00'),
        row('05/01/2026', 'UPI/402112345678/SWIGGY/okaxis', '450.00', '', '59,550.00'),
        row('', 'ORDER 99 DINNER', '', '', ''),
        row('07/01/2026', 'NETFLIX SUBSCRIPTION', '649.00', '', '58,901.00'),
    ])
    types = [(t.transaction_type, t.amount) for t in parsed.transactions]
    assert types == [('credit', Decimal('50000.00')), ('debit', Decimal('450.00')), ('debit', Decimal('649.00'))]
    assert parsed.transactions[1].description == 'UPI/402112345678/SWIGGY/okaxis ORDER 99 DINNER'
    assert parsed.transactions[1].reference_number == '402112345678'
    assert parsed.summary.account_identifier == 'XXXX0123'  # full account number is never kept


def test_balance_arithmetic_overrides_ambiguous_column_position():
    # The deposit is printed under the withdrawal column, but the running balance proves it was a credit.
    parsed = parse_lines([
        'Opening Balance: 1,000.00', HEADER,
        row('03/01/2026', 'CASH DEPOSIT', '500.00', '', '1,500.00'),
    ])
    t = parsed.transactions[0]
    assert t.transaction_type == 'credit' and t.extraction_status == 'needs_review'
    assert [w.code for w in parsed.warnings] == ['direction_inferred_from_balance']


def test_dr_cr_balances_and_opening_balance_row():
    parsed = parse_lines([
        HEADER,
        row('01/01/2026', 'OPENING BALANCE', '', '', '200.00 Cr'),
        row('02/01/2026', 'ATM WITHDRAWAL', '500.00', '', '300.00 Dr'),
    ])
    assert parsed.summary.opening_balance == Decimal('200.00')
    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].balance_after_transaction == Decimal('-300.00')
    assert parsed.transactions[0].extraction_confidence == 0.99


def test_header_carries_over_to_pages_without_header():
    parsed = parse_lines([
        [HEADER, row('02/01/2026', 'GROCERY', '100.00', '', '900.00')],
        [row('03/01/2026', 'PHARMACY', '50.00', '', '850.00')],
    ])
    assert [t.source_page for t in parsed.transactions] == [1, 2]


def test_unreadable_row_becomes_warning_not_a_guess():
    parsed = parse_lines([
        HEADER,
        row('02/01/2026', 'GROCERY', '100.00', '', '900.00'),
        row('03/01/2026', 'MYSTERY', '50.00', '60.00', '910.00'),  # both debit and credit filled
    ])
    assert len(parsed.transactions) == 1
    assert parsed.warnings[0].code == 'unparsed_row'


def test_document_without_transaction_table_is_not_parsed():
    layout = extract_pdf_text(make_pdf([['Annual tax summary', 'Nothing tabular here 1,000.00']])).layout
    assert choose_parser(layout) is None


@pytest.mark.parametrize('raw,message', [
    (b'hello', 'Not a valid PDF header'),
    (b'%PDF-1.4 garbage that is not a pdf', 'Unable to read PDF'),
])
def test_malformed_documents_are_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        extract_pdf_text(raw)


def test_image_only_pdf_is_rejected():
    with pytest.raises(ValueError, match='OCR'):
        extract_pdf_text(make_pdf([[]]))


GPAY_PAGE = '''
                                       Transaction       statement
                          9790530906,         someone@example.com
Transaction    statement     period                                       Sent                                       Received
01 May   2026   - 31  October    2026                                ₹1,050.50                                 ₹90,000.00

Date  & time               Transaction   details                                                                                  Amount

01 May,  2026              Paid to Kisetsu  Saison  Finance  India  Pvt Ltd                                                       ₹1,000
12:06 AM                   UPI Transaction  ID:612511782906
                               Paid by HDFC   Bank  6873

12May,   2026              Paid toChai  Corner                                                                                       ₹50.50
07:18PM                    UPI Transaction  ID: 123023619396
                               Paid by  HDFC  Bank  6873

23 May,  2026              Received   from  shankar   N                                                                           ₹90,000
10:14AM                   UPI Transaction  ID: 123561569902
                               Paid to HDFC  Bank  6873

Note:This statement reflectspayments  made  by you on the Google Pay app.
'''


def test_payment_app_statement_is_parsed_and_reconciled_against_printed_totals():
    from app.reconciliation import reconcile
    pages = [GPAY_PAGE]
    parser = choose_parser(pages)
    assert parser is not None and parser.name == 'payment_app'
    parsed = parser.parse(pages)
    s = parsed.summary
    assert (s.institution, s.account_identifier) == ('Google Pay', 'XXXX0906')
    assert (s.period_start, s.period_end) == (date(2026, 5, 1), date(2026, 10, 31))
    assert (s.total_debits, s.total_credits) == (Decimal('1050.50'), Decimal('90000.00'))
    got = [(t.transaction_date, t.transaction_type, t.amount, t.description) for t in parsed.transactions]
    assert got == [
        (date(2026, 5, 1), 'debit', Decimal('1000.00'), 'UPI/612511782906/Kisetsu Saison Finance India Pvt Ltd'),
        (date(2026, 5, 12), 'debit', Decimal('50.50'), 'UPI/123023619396/Chai Corner'),
        (date(2026, 5, 23), 'credit', Decimal('90000.00'), 'UPI/123561569902/shankar N'),
    ]
    assert parsed.warnings == []
    assert reconcile(parsed).status == 'reconciled'  # printed Sent/Received totals match exactly


def test_payment_app_totals_mismatch_is_reported_not_hidden():
    from app.reconciliation import reconcile
    pages = [GPAY_PAGE.replace('₹1,050.50', '₹9,999.00')]
    assert reconcile(choose_parser(pages).parse(pages)).status == 'mismatch'
