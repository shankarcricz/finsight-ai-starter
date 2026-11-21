"""Generic parser for selectable-text statements laid out as a table:

    Date | [Value date] | Narration | [Ref] | Debit/Withdrawal | Credit/Deposit | Balance

It works on pypdf's layout-mode text, where column positions survive as character offsets.
Anything it cannot read confidently becomes a warning or a needs_review row — never a guess.
"""
import re
from dataclasses import dataclass
from decimal import Decimal
from app.parsers.base import StatementParser
from app.parsers.normalize import (AMOUNT_TOKEN, DATE_TOKEN, EMPTY_CELL, extract_reference,
                                   parse_amount, parse_date)
from app.schemas import ExtractionWarning, ParsedStatement, ParsedTransaction, StatementSummary

_CELL = re.compile(r'\S+(?: \S+)*')  # cells are separated by 2+ spaces in layout text
_HDR_DEBIT = re.compile(r'\b(?:debits?|withdrawals?(?: amt\.?)?|dr\.?)\b(?:\s*\([A-Z₹]{1,3}\))?', re.I)
_HDR_CREDIT = re.compile(r'\b(?:credits?|deposits?(?: amt\.?)?|cr\.?)\b(?:\s*\([A-Z₹]{1,3}\))?', re.I)
_HDR_BALANCE = re.compile(r'\b(?:closing\s+)?balance\b(?:\s*\([A-Z₹]{1,3}\))?', re.I)
_HDR_DATE = re.compile(r'\b(?:txn\s+|transaction\s+|tran\s+)?date\b', re.I)
_MONEY = r'((?:INR|Rs\.?|₹)?\s*-?[\d,]+\.\d{2}(?:\s*(?:Dr|Cr)\b)?)'
_SUMMARY_LABELS = {
    'opening_balance': r'opening\s+bal(?:ance|\.)?|balance\s+b/?f|brought\s+forward',
    'closing_balance': r'closing\s+bal(?:ance|\.)?|balance\s+c/?f|carried\s+forward',
    'total_credits': r'total\s+(?:credits?|deposits?)',
    'total_debits': r'total\s+(?:debits?|withdrawals?)',
}
_ACCOUNT = re.compile(r'(?:account|a/c)\s*(?:no\.?|number|#)\s*:?\s*([A-Z0-9][A-Z0-9*\-]{3,30})', re.I)
_PERIOD = re.compile(
    r'(?:statement\s+period|statement\s+from|period)\s*:?\s*(.+?)\s+(?:-|to)\s+(.+?)(?=\s{2,}|$)', re.I)
_CURRENCY = re.compile(r'\bcurrency\s*:?\s*([A-Z]{3})\b', re.I)
_INSTITUTION = re.compile(r'\b([A-Z][A-Za-z&.]+(?: [A-Z][A-Za-z&.]+){0,3} BANK)\b')


@dataclass
class _Columns:
    debit: tuple[int, int]
    credit: tuple[int, int]
    balance: tuple[int, int]
    narration_start: int

    def nearest(self, start: int, end: int) -> str:
        def dist(span: tuple[int, int]) -> int:  # handles left- or right-aligned numbers
            return min(abs(end - span[1]), abs(start - span[0]))
        return min(('debit', 'credit', 'balance'), key=lambda c: dist(getattr(self, c)))

    def ordered(self) -> list[str]:
        return sorted(('debit', 'credit', 'balance'), key=lambda c: getattr(self, c)[0])


def _detect_header(line: str) -> _Columns | None:
    date = _HDR_DATE.search(line)
    balance = _HDR_BALANCE.search(line)
    if not (date and balance):
        return None
    debit = _HDR_DEBIT.search(line, date.end())
    credit = _HDR_CREDIT.search(line, date.end())
    if not (debit and credit) or debit.start() == credit.start():
        return None
    return _Columns(debit.span(), credit.span(), balance.span(), narration_start=date.end() + 1)


class GenericTableParser(StatementParser):
    name = 'generic_table'

    def detect(self, pages: list[str]) -> float:
        for page in pages:
            for line in page.splitlines():
                if _detect_header(line):
                    return 0.5  # generic fallback; bank-specific parsers should score higher
        return 0.0

    def parse(self, pages: list[str]) -> ParsedStatement:
        warnings: list[ExtractionWarning] = []
        txns: list[ParsedTransaction] = []
        summary_lines: list[str] = []
        stated_opening: Decimal | None = None
        columns: _Columns | None = None
        # Seed the running balance with the printed opening balance so row 1 is cross-checked too.
        prev_balance: Decimal | None = None
        if m := re.search(rf'(?:{_SUMMARY_LABELS["opening_balance"]})\s*:?\s*{_MONEY}', '\n'.join(pages), re.I):
            try:
                value, marker = parse_amount(m.group(1))
                prev_balance = -abs(value) if marker == 'Dr' else value
            except ValueError:
                pass

        for page_no, page in enumerate(pages, start=1):
            last: ParsedTransaction | None = None
            for row_no, line in enumerate(page.splitlines(), start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                header = _detect_header(line)
                if header:
                    columns = header
                    last = None
                    continue
                if columns is None or not DATE_TOKEN.match(stripped):
                    if last and self._is_continuation(line, columns):
                        last.description = f'{last.description} {stripped}'[:500]
                        last.original_description = f'{last.original_description} {stripped}'[:500]
                        continue
                    last = None
                    summary_lines.append(line)
                    continue

                row = self._split_row(line)
                if row is None:
                    if AMOUNT_TOKEN.search(stripped.split()[-1]):
                        warnings.append(ExtractionWarning(code='unparsed_row', page=page_no,
                                                          message=f'Could not read row: {stripped[:120]}'))
                    summary_lines.append(line)
                    last = None
                    continue
                date_text, narration, numeric = row
                upper = narration.upper()
                if 'OPENING BALANCE' in upper or 'CLOSING BALANCE' in upper or upper.startswith('B/F'):
                    bal = self._balance_from_cells(numeric, columns)
                    if bal is not None and ('OPENING' in upper or upper.startswith('B/F')):
                        stated_opening = stated_opening if stated_opening is not None else bal
                        prev_balance = bal
                    continue
                try:
                    txn = self._build_txn(date_text, narration, numeric, columns, prev_balance,
                                          page_no, row_no, warnings)
                except ValueError as exc:
                    warnings.append(ExtractionWarning(code='unparsed_row', page=page_no,
                                                      message=f'{exc}: {stripped[:120]}'))
                    last = None
                    continue
                txns.append(txn)
                last = txn
                if txn.balance_after_transaction is not None:
                    prev_balance = txn.balance_after_transaction

        if columns is None:
            warnings.append(ExtractionWarning(code='no_table_header',
                                              message='No Date/Debit/Credit/Balance table header found'))
        summary = self._parse_summary('\n'.join(summary_lines), warnings)
        if summary.opening_balance is None and stated_opening is not None:
            summary.opening_balance = stated_opening
        if txns and summary.opening_balance is not None and txns[0].balance_after_transaction is not None:
            first = txns[0]
            signed = first.amount if first.transaction_type == 'credit' else -first.amount
            if summary.opening_balance + signed == first.balance_after_transaction:
                first.extraction_confidence = max(first.extraction_confidence, 0.99)
        return ParsedStatement(parser=self.name, summary=summary, transactions=txns, warnings=warnings)

    # ---- rows -------------------------------------------------------------------------------

    @staticmethod
    def _split_row(line: str):
        cells = [(m.start(), m.end(), m.group()) for m in _CELL.finditer(line)]
        if len(cells) < 3:
            return None
        date_cell = cells[0][2]
        date_m = DATE_TOKEN.match(date_cell)
        if not date_m:
            return None
        rest_of_first = date_cell[date_m.end():].strip()  # date and narration joined by one space
        body = cells[1:]
        numeric: list[tuple[int, int, str]] = []
        while body and (AMOUNT_TOKEN.match(body[-1][2]) or body[-1][2] in EMPTY_CELL
                        or body[-1][2].upper() in {'DR', 'CR'}):
            numeric.insert(0, body.pop())
        # Attach stand-alone 'Dr'/'Cr' cells to the amount before them.
        merged: list[tuple[int, int, str]] = []
        for start, end, text in numeric:
            if text.upper() in {'DR', 'CR'} and merged:
                ps, _, pt = merged[-1]
                merged[-1] = (ps, end, f'{pt} {text}')
            else:
                merged.append((start, end, text))
        narration_cells = [c[2] for c in body if not DATE_TOKEN.fullmatch(c[2])]  # drop value date
        narration = ' '.join(filter(None, [rest_of_first, *narration_cells]))
        if not narration or not merged or not any(AMOUNT_TOKEN.match(c[2]) for c in merged):
            return None
        return date_cell[:date_m.end()], narration, merged

    @staticmethod
    def _assign(numeric, columns: _Columns) -> dict[str, str]:
        if len(numeric) == 3:  # every column present ('-' placeholders): order is unambiguous
            return {col: cell[2] for col, cell in zip(columns.ordered(), numeric)}
        assigned: dict[str, str] = {}
        for start, end, text in numeric:
            col = columns.nearest(start, end)
            if col in assigned:
                raise ValueError('Two amounts fall in the same column')
            assigned[col] = text
        return assigned

    def _balance_from_cells(self, numeric, columns: _Columns) -> Decimal | None:
        try:
            text = self._assign(numeric, columns).get('balance') or numeric[-1][2]
            value, marker = parse_amount(text)
            return -abs(value) if marker == 'Dr' else value
        except ValueError:
            return None

    def _build_txn(self, date_text, narration, numeric, columns, prev_balance, page_no, row_no,
                   warnings) -> ParsedTransaction:
        cells = self._assign(numeric, columns)
        explicit = len(numeric) == 3
        debit = cells.get('debit', '-')
        credit = cells.get('credit', '-')
        has_debit, has_credit = debit not in EMPTY_CELL, credit not in EMPTY_CELL
        if has_debit == has_credit:
            raise ValueError('Expected exactly one of debit/credit')
        amount, marker = parse_amount(debit if has_debit else credit)
        amount = abs(amount)
        if amount == 0:
            raise ValueError('Zero amount')
        txn_type = 'debit' if has_debit else 'credit'
        if marker:
            txn_type = 'debit' if marker == 'Dr' else 'credit'

        balance = None
        if 'balance' in cells and cells['balance'] not in EMPTY_CELL:
            balance, bmarker = parse_amount(cells['balance'])
            if bmarker == 'Dr':  # overdrawn balance
                balance = -abs(balance)

        confidence, status = (0.95 if explicit or marker else 0.85), 'parsed'
        if balance is not None and prev_balance is not None:
            signed = amount if txn_type == 'credit' else -amount
            if prev_balance + signed == balance:
                confidence = 0.99
            elif prev_balance - signed == balance and not explicit and not marker:
                # Column position disagreed with the running balance; balance arithmetic wins.
                txn_type = 'credit' if txn_type == 'debit' else 'debit'
                confidence, status = 0.8, 'needs_review'
                warnings.append(ExtractionWarning(
                    code='direction_inferred_from_balance', page=page_no,
                    message=f'Direction of row dated {date_text} inferred from running balance'))
        elif balance is None:
            confidence = min(confidence, 0.8)

        return ParsedTransaction(
            transaction_date=parse_date(date_text),
            description=narration,
            original_description=narration,
            amount=amount,
            transaction_type=txn_type,
            balance_after_transaction=balance,
            reference_number=extract_reference(narration),
            source_page=page_no,
            source_row=row_no,
            extraction_confidence=confidence,
            extraction_status=status,
        )

    @staticmethod
    def _is_continuation(line: str, columns: _Columns | None) -> bool:
        if columns is None:
            return False
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        return (indent >= columns.narration_start - 2 and len(stripped) <= 80
                and not any(AMOUNT_TOKEN.match(c) for c in _CELL.findall(stripped)))

    # ---- summary block ----------------------------------------------------------------------

    @staticmethod
    def _parse_summary(text: str, warnings: list[ExtractionWarning]) -> StatementSummary:
        summary = StatementSummary()
        for field, label in _SUMMARY_LABELS.items():
            m = re.search(rf'(?:{label})\s*:?\s*{_MONEY}', text, re.I)
            if m:
                value, marker = parse_amount(m.group(1))
                if marker == 'Dr' and 'balance' in field:
                    value = -abs(value)
                setattr(summary, field, abs(value) if 'total' in field else value)

        if m := _ACCOUNT.search(text):
            ident = re.sub(r'[^A-Za-z0-9]', '', m.group(1))
            summary.account_identifier = 'XXXX' + ident[-4:]
        if m := _CURRENCY.search(text):
            summary.currency = m.group(1).upper()
        if m := _INSTITUTION.search(text):
            summary.institution = m.group(1).strip()
        for line in text.splitlines():
            if m := _PERIOD.search(line):
                try:
                    end = parse_date(m.group(2))
                    start = parse_date(m.group(1), default_year=end.year)
                    if start > end:
                        start = start.replace(year=end.year - 1)
                    summary.period_start, summary.period_end = start, end
                except ValueError:
                    warnings.append(ExtractionWarning(code='unparsed_period',
                                                      message=f'Unreadable statement period: {m.group(0)[:80]}'))
                break
        return summary
