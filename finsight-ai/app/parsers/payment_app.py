"""Parser for UPI / payment-app statements (Google Pay and similar) that list one *block* per payment:

    01 May, 2026   Paid to Swiggy                        ₹533
    12:06 AM       UPI Transaction ID: 612511782906
                   Paid by HDFC Bank 6873

There is no Debit/Credit/Balance table and no running balance, so direction comes from the wording
("Paid to" / "Received from") and the only cross-check is the Sent/Received totals in the header.
pypdf's layout text is whitespace-noisy ("Paid  to", "01May, 2026", "Paid toChai"), so every pattern
here tolerates missing or doubled spaces.
"""
import re
from datetime import date
from decimal import Decimal
from app.parsers.base import StatementParser
from app.parsers.normalize import extract_reference, parse_date
from app.schemas import ExtractionWarning, ParsedStatement, ParsedTransaction, StatementSummary

_DATE = r'(\d{1,2})\s*([A-Za-z]{3,9})\s*,?\s*(\d{4})'
_MONEY = r'(?:₹|INR|Rs\.?)\s*([\d,]+(?:\.\d{1,2})?)'
_HEADER = re.compile(r'date\s*&?\s*time.*transaction\s*details.*amount', re.I)
_ENTRY = re.compile(rf'^\s*{_DATE}\s+(.*)$')
_DEBIT_WORDS = r'paid\s*to|sent\s*to|payment\s*to|debited'
_CREDIT_WORDS = r'received\s*from|refund(?:ed)?\s*from|credited'
_DIRECTION = re.compile(rf'^(?:(?P<debit>{_DEBIT_WORDS})|(?P<credit>{_CREDIT_WORDS}))\s*(?P<name>.*)$', re.I)
_TRAILING_AMOUNT = re.compile(rf'\s*{_MONEY}\s*$')
_TXN_ID = re.compile(r'transaction\s*id\s*:?\s*([A-Z0-9]{6,})', re.I)
_TIME = re.compile(r'^\s*\d{1,2}:\d{2}\s*[AP]M\b', re.I)
_FOOTER = re.compile(r'^\s*(?:note\s*:|page\s*\d+\s*of\s*\d+)', re.I)
_PERIOD = re.compile(rf'{_DATE}\s*(?:-|–|to)\s*{_DATE}', re.I)
_TOTALS = re.compile(rf'{_MONEY}\s+{_MONEY}')
_PHONE = re.compile(r'(?<!\d)(\d{10})(?!\d)')


def _amount(text: str) -> Decimal:
    return Decimal(text.replace(',', '')).quantize(Decimal('0.01'))


def _to_date(day: str, month: str, year: str) -> date:
    return parse_date(f'{day} {month} {year}')


class PaymentAppParser(StatementParser):
    name = 'payment_app'

    def detect(self, pages: list[str]) -> float:
        text = '\n'.join(pages)
        if not _HEADER.search(text):
            return 0.0
        entries = sum(1 for line in text.splitlines()
                      if (m := _ENTRY.match(line)) and _DIRECTION.match(' '.join(m.group(4).split())))
        return 0.7 if entries else 0.0

    def parse(self, pages: list[str]) -> ParsedStatement:
        warnings: list[ExtractionWarning] = []
        txns: list[ParsedTransaction] = []
        summary = self._parse_header('\n'.join(pages))

        for page_no, page in enumerate(pages, start=1):
            block: list[tuple[int, str]] = []  # (row number, line) of the entry being collected

            def flush():
                if block:
                    self._finish_block(block, page_no, txns, warnings)
                    block.clear()

            for row_no, line in enumerate(page.splitlines(), start=1):
                if not line.strip():
                    continue
                if _FOOTER.match(line):
                    flush()
                    continue
                m = _ENTRY.match(line)
                if m and _DIRECTION.match(' '.join(m.group(4).split())):
                    flush()
                    block.append((row_no, line))
                elif block:
                    block.append((row_no, line))
            flush()

        if txns:
            summary.period_start = summary.period_start or min(t.transaction_date for t in txns)
            summary.period_end = summary.period_end or max(t.transaction_date for t in txns)
        else:
            warnings.append(ExtractionWarning(code='no_entries', message='No "Paid to"/"Received from" entries found'))
        return ParsedStatement(parser=self.name, summary=summary, transactions=txns, warnings=warnings)

    # ---- blocks -----------------------------------------------------------------------------

    @staticmethod
    def _finish_block(block, page_no: int, txns: list[ParsedTransaction], warnings: list[ExtractionWarning]) -> None:
        row_no, first = block[0]
        m = _ENTRY.match(first)
        head = ' '.join(m.group(4).split())
        amount_m = _TRAILING_AMOUNT.search(head)
        if amount_m is None:  # a long counterparty name can wrap; the amount is then on a later line
            for _, extra in block[1:]:
                if _TIME.match(extra) and (amount_m := _TRAILING_AMOUNT.search(extra)):
                    head = f'{head} {extra[:amount_m.start()].strip()}'.strip()
                    break
        else:
            head = head[:amount_m.start()].strip()
        try:
            if amount_m is None:
                raise ValueError('no amount')
            d = _DIRECTION.match(head)
            amount = _amount(amount_m.group(1))
            if amount == 0:
                raise ValueError('zero amount')
            when = _to_date(m.group(1), m.group(2), m.group(3))
        except ValueError as exc:
            warnings.append(ExtractionWarning(code='unparsed_row', page=page_no,
                                              message=f'Could not read entry ({exc}): {" ".join(first.split())[:120]}'))
            return

        name = d.group('name').strip() or 'Unknown'
        ids = _TXN_ID.search(' '.join(' '.join(line.split()) for _, line in block[1:]))
        # "UPI/<id>/<name>" mirrors bank narrations, so merchant normalisation and categorisation just work.
        description = f'UPI/{ids.group(1)}/{name}' if ids else f'UPI/{name}'
        txns.append(ParsedTransaction(
            transaction_date=when, description=description[:500], original_description=description[:500],
            amount=amount, transaction_type='debit' if d.group('debit') else 'credit',
            reference_number=extract_reference(description) or (ids.group(1)[:64] if ids else None),
            source_page=page_no, source_row=row_no,
            # Direction and amount are both printed explicitly; no balance exists to cross-check against.
            extraction_confidence=0.95 if ids else 0.85, extraction_status='parsed'))

    # ---- header -----------------------------------------------------------------------------

    @staticmethod
    def _parse_header(text: str) -> StatementSummary:
        summary = StatementSummary(institution='Google Pay' if re.search(r'google\s*pay|gpay', text, re.I)
                                   else 'UPI payment app')
        lines = text.splitlines()[:40]  # period, phone and totals sit above the first table
        head_text = '\n'.join(lines)
        if m := _PHONE.search(head_text):
            summary.account_identifier = 'XXXX' + m.group(1)[-4:]
        if m := _PERIOD.search(head_text):
            try:
                summary.period_start = _to_date(*m.group(1, 2, 3))
                summary.period_end = _to_date(*m.group(4, 5, 6))
            except ValueError:
                pass
        for i, line in enumerate(lines):
            if re.search(r'\bsent\b', line, re.I) and re.search(r'\breceived\b', line, re.I):
                for nxt in lines[i + 1:i + 4]:
                    if t := _TOTALS.search(nxt):
                        summary.total_debits, summary.total_credits = _amount(t.group(1)), _amount(t.group(2))
                        break
                break
        return summary
