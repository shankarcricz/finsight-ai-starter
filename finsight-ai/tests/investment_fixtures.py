"""Fictional broker documents that mimic the real layouts (no real names, IDs, or holdings)."""
import io
import openpyxl
from tests.pdf_factory import make_pdf

FAKE_PAN = 'ABCDE1234F'
FAKE_NAME = 'Test Person'


def _dp_row(sr, isin, name, free, pldg, demat, remat, lock, tot, rate, value):
    return (f'{sr:>4}  {isin:<14}  {name:<28}  {free:>9}  {pldg:>6}  {demat:>6}  {remat:>6}  {lock:>7}  '
            f'{tot:>9}  {rate:>8}  {value:>10}')


def dp_holding_pdf(rows, total: str, as_of='19/11/2026', bo_id='1200000000001234', extra_lines=()):
    header = _dp_row('Sr.', 'ISIN Code', 'Company Name', 'Free Bal', 'Pldg', 'Demat', 'Remat', 'LockIn',
                     'Tot Qty', 'Rate', 'Value')
    pldg_col = header.index('Pldg')
    lines = ['INDSTOCKS PRIVATE LIMITED', f'DEPOSITORY HOLDING STATEMENT AS ON {as_of}',
             f'BO ID {bo_id}', f'BO Name {FAKE_NAME}', f'Holding as on {as_of}', header,
             ' ' * pldg_col + 'Bal']
    for r in rows:
        lines.append(_dp_row(*r))
    lines += list(extra_lines)
    lines.append(' ' * (len(header) - 18) + f'Total{total:>13}')
    return make_pdf([lines])


def _xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    # Fixed metadata timestamps so the same rows always produce byte-identical files (duplicate detection).
    from datetime import datetime
    wb.properties.created = wb.properties.modified = datetime(2026, 1, 1)
    ws = wb.active
    ws.title = 'Client Fund Ledger'
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


LEDGER_HEADER = ['Transaction Date', 'Settlement Date', 'Clearing\nCorporation/Clearing\nMember', 'Segment Type',
                 'Settlement No.', 'Bill/Chq No.', 'Transaction Type', 'Particulars / Narration', 'Voucher No.',
                 'Debit (Rs.)', 'Credit (Rs.)', 'Balance (Rs.)']

# Newest first, like Groww. Chronologically: deposits 15,000; withdrawals 20,049.75; buys 44,034.68;
# sells 50,000; closing balance 915.57.
LEDGER_ROWS = [
    ['12-08-2026', '13-08-2026', 'CM', 'STOCKS_SETTLEMENT', 'S7', 'S7', 'bill', 'N/A', 'SET0007', 9084.43, 0.0, 915.57],
    ['12-08-2026', '12-08-2026', 'N/A', 'UPI', 'N/A', 'u6', 'JV', 'N/A', 'DEP0006', 0.0, 10000.0, 10000.0],
    ['10-08-2026', '10-08-2026', 'N/A', 'STOCK_PAYOUT', 'N/A', 'p5', 'Payment', 'N/A', 'WTD0005', 49.75, 0.0, 0.0],
    ['05-08-2026', '06-08-2026', 'CM', 'STOCKS_SETTLEMENT', 'S4', 'S4', 'bill', 'N/A', 'SET0004', 34950.25, 0.0, 49.75],
    ['05-08-2026', '05-08-2026', 'N/A', 'UPI', 'N/A', 'u3', 'JV', 'N/A', 'DEP0003', 0.0, 5000.0, 35000.0],
    ['03-08-2026', '03-08-2026', 'N/A', 'GROWW_WITHDRAW', 'N/A', 'p2', 'Payment', 'N/A', 'WTD0002', 20000.0, 0.0, 30000.0],
    ['01-08-2026', '02-08-2026', 'CM', 'STOCKS_SETTLEMENT', 'S1', 'S1', 'bill', 'N/A', 'SET0001', 0.0, 50000.0, 50000.0],
]


def ledger_xlsx(rows=None, client_code='9000001234') -> bytes:
    return _xlsx([
        ['Client Name', FAKE_NAME], ['Unique Client Code', client_code], ['Backoffice Client Code', 'ACC000'],
        ['PAN', FAKE_PAN], [],
        ['Statement of Accounts of Funds for the period from 01/04/2026 to 20/11/2026'], [],
        LEDGER_HEADER, *(LEDGER_ROWS if rows is None else rows), [],
        ['Note - This is a computer generated statement.'],
    ])


MF_HEADER = ['Scheme Name', 'Transaction Type', 'Units', 'NAV', 'Amount', 'Date']

GOLDX = 'INF000X01019'  # fictional ETF ISIN


def indmoney_us_xlsx() -> bytes:
    """Fictional INDmoney US holdings report: USD, fractional shares, tickers instead of ISINs."""
    hdr = ['Symbol', 'Name', 'Quantity', '($) Cost Price', '($) Invested Value', '($) Market Price',
           '($) Market Value', '($) Unrealized Returns', 'Broker']
    return _xlsx([['Holdings Details'], ['As Of Date', '2026-11-21'], ['Account Number', 'WXYZ009876'], [], [], hdr,
                  ['ACME', 'Acme Corp', 0.93130636, 223.073747719, 207.75, 246.82, 229.865, 22.115, 'INDMONEY GLOBAL'],
                  ['GLOBX', 'Globex', 0.321648909, 347.273049678, 111.7, 265.29, 85.3302, -26.3698, 'INDMONEY GLOBAL'],
                  [], [], ['Disclaimer:-']])


def groww_holdings_xlsx(client_code=9000001234.0) -> bytes:
    # Rows sum to 3003.30; summary "Invested" uses unrounded cost 3003.60 -> within the rounding tolerance.
    return _xlsx([['Name', FAKE_NAME], ['Unique Client Code', client_code], [],
                  ['Holdings statement for stocks as on 20-11-2026'], [], ['Summary'],
                  ['Invested Value', 3003.6], ['Closing Value', 3000.0], ['Unrealised P&L', -3.6], [],
                  ['Stock Name', 'ISIN', 'Quantity', 'Average buy price', 'Buy value', 'Closing price', 'Closing value', 'Unrealised P&L'],
                  ['ALPHA POWER LIMITED', 'INE001A01036', 10.0, 100.33, 1003.3, 120.0, 1200.0, 196.7],
                  ['GOLDX ETF', GOLDX, 100.0, 20.0, 2000.0, 18.0, 1800.0, -200.0]])


def groww_pnl_xlsx(client_code=9000001234.0, realised_total=50.0) -> bytes:
    lot_hdr = ['Stock name', 'ISIN', 'Quantity', 'Buy date', 'Buy price', 'Buy value']
    return _xlsx([['Name', FAKE_NAME], ['Unique Client Code', client_code], [],
                  ['P&L Statement for stocks from 01-04-2026 TO 20-11-2026'], [], ['Summary'], [], ['P&L'],
                  ['Realised P&L', realised_total], ['Unrealised P&L', -3.3], [], ['Charges'],
                  ['Brokerage', 20.0], ['STT', 5.0], ['Total GST', 3.6], ['Total', 28.6], [], ['Realised trades'], [],
                  lot_hdr + ['Sell date', 'Sell price', 'Sell value', 'Realised P&L', 'Remark'],
                  ['BETA BANK LIMITED', 'INE002B01024', 5.0, '01-02-2026', 100.0, 500.0, '01-06-2026', 110.0, 550.0, 50.0, ''],
                  [], ['Unrealised trades'], [],
                  lot_hdr + ['Closing date', 'Closing price', 'Closing value', 'Unrealised P&L', 'Remark'],
                  ['ALPHA POWER LIMITED', 'INE001A01036', 10.0, '15-03-2026', 100.33, 1003.3, '20-11-2026', 120.0, 1200.0, 196.7, ''],
                  ['GOLDX ETF', GOLDX, 100.0, '10-01-2026', 20.0, 2000.0, '20-11-2026', 18.0, 1800.0, -200.0, ''],
                  [], ['Disclaimer: '], ['Consult a tax advisor.']])


def mf_orders_xlsx(rows) -> bytes:
    return _xlsx([['Personal Details'], ['Name', FAKE_NAME], ['Mobile Number', '9000000000'], ['PAN', FAKE_PAN],
                  [], ['TRANSACTIONS FROM Jan 01 2026 TO Nov 21 2026'], MF_HEADER,
                  *(rows or [['NO TRANSACTIONS FOUND']])])
