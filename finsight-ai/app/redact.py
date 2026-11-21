"""Turn a real statement into a shareable *layout skeleton* plus a parser diagnosis.

    python -m app.redact path\\to\\statement.pdf -o redacted.txt

What is kept: column positions/spacing, common statement vocabulary (headers, UPI/NEFT, Dr/Cr...),
punctuation, and the *shape* of dates and amounts. What is removed: every other word (names,
merchants, addresses -> XXXX) and every digit outside dates (account numbers, amounts, references
are replaced by deterministic pseudo-random digits). Nothing is sent anywhere.

ALWAYS read the output file yourself before sharing it.
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

KEEP_WORDS = {
    # table headers & summary labels
    'date', 'txn', 'tran', 'transaction', 'transactions', 'value', 'posting', 'narration', 'description',
    'particulars', 'details', 'remarks', 'chq', 'cheque', 'ref', 'reference', 'no', 'number', 'withdrawal',
    'withdrawals', 'deposit', 'deposits', 'debit', 'debits', 'credit', 'credits', 'amt', 'amount', 'balance',
    'closing', 'opening', 'total', 'totals', 'count', 'summary', 'statement', 'account', 'accounts', 'period',
    'from', 'to', 'of', 'page', 'branch', 'ifsc', 'micr', 'type', 'savings', 'current', 'currency', 'inr',
    'rs', 'dr', 'cr', 'bf', 'cf', 'brought', 'forward', 'carried', 'generated', 'on', 'customer', 'id',
    'nominee', 'joint', 'holder', 'holders', 'address', 'email', 'phone', 'mobile', 'sl', 's', 'sno', 'serial',
    'init', 'br', 'and', 'the', 'for', 'by', 'in', 'at',
    # banks
    'bank', 'hdfc', 'icici', 'sbi', 'state', 'india', 'axis', 'kotak', 'ltd', 'limited',
    # payment rails & common narration tokens (tell the parser what kind of row it is)
    'upi', 'neft', 'imps', 'rtgs', 'nach', 'ach', 'ecs', 'atm', 'pos', 'card', 'debitcard', 'atw', 'nwd',
    'salary', 'sal', 'interest', 'int', 'charges', 'charge', 'fee', 'gst', 'emi', 'loan', 'transfer', 'trf',
    'self', 'refund', 'reversal', 'cash', 'cms', 'mmt', 'inb', 'ib', 'mb', 'billpay', 'bil', 'onl', 'by',
    'sip', 'mf', 'mutual', 'fund', 'zerodha', 'groww', 'rent', 'tpt', 'clg', 'chg', 'fund',
}
DATE_RE = re.compile(r'\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}[ -][A-Za-z]{3}[ -]\d{2,4})\b')
WORD_RE = re.compile(r'[A-Za-z]+')
MONTHS = {'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec'}


def _scramble_digits(s: str, salt: str) -> str:
    digest = hashlib.sha256((salt + s).encode()).hexdigest()
    out, i = [], 0
    for ch in s:
        if ch.isdigit():
            out.append(str(int(digest[i % len(digest)], 16) % 10))
            i += 1
        else:
            out.append(ch)
    return ''.join(out)


def redact_line(line: str, salt: str) -> str:
    parts, last = [], 0
    for m in DATE_RE.finditer(line):  # dates keep their shape and values (needed to test date parsing)
        parts.append(_redact_segment(line[last:m.start()], salt))
        parts.append(m.group())
        last = m.end()
    parts.append(_redact_segment(line[last:], salt))
    return ''.join(parts)


def _redact_segment(seg: str, salt: str) -> str:
    seg = WORD_RE.sub(lambda m: m.group() if m.group().lower() in KEEP_WORDS | MONTHS
                      else ('X' * len(m.group())), seg)
    return _scramble_digits(seg, salt)


def diagnose(layout_pages: list[str]) -> list[str]:
    from app.parsers import choose_parser
    from app.reconciliation import reconcile
    out = []
    parser = choose_parser(layout_pages)
    if parser is None:
        return ['parser: NONE (no Date / Debit / Credit / Balance header line recognised)']
    parsed = parser.parse(layout_pages)
    recon = reconcile(parsed) if parsed.transactions else None
    s = parsed.summary
    out.append(f'parser: {parsed.parser}')
    out.append(f'transactions extracted: {len(parsed.transactions)}')
    out.append('summary fields found: ' + ', '.join(
        f for f in ('account_identifier', 'period_start', 'opening_balance', 'closing_balance', 'total_credits',
                    'total_debits') if getattr(s, f) is not None))
    if recon:
        out.append(f'reconciliation: {recon.status} ' + str({c.name: c.status for c in recon.checks}))
    review = sum(t.extraction_status == 'needs_review' for t in parsed.transactions)
    out.append(f'rows needing review: {review}')
    codes: dict[str, int] = {}
    for w in parsed.warnings:
        codes[w.code] = codes.get(w.code, 0) + 1
    out.append(f'warning counts: {codes or "none"}')  # messages may contain row text, so only counts
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdf', type=Path)
    ap.add_argument('-o', '--output', type=Path, default=Path('redacted_statement.txt'))
    ap.add_argument('--max-pages', type=int, default=3, help='only the first N pages (default 3)')
    args = ap.parse_args(argv)

    from app.ingest import extract_pdf_text
    text = extract_pdf_text(args.pdf.read_bytes())
    salt = hashlib.sha256(args.pdf.read_bytes()).hexdigest()[:16]
    lines = ['# FinSight redacted layout skeleton - REVIEW BEFORE SHARING', '# Parser diagnosis (original file):']
    lines += [f'#   {d}' for d in diagnose(text.layout)]
    for i, page in enumerate(text.layout[:args.max_pages], start=1):
        lines.append(f'\n===== PAGE {i} =====')
        lines += [redact_line(l, salt) for l in page.splitlines()]
    args.output.write_text('\n'.join(lines), encoding='utf-8')
    print(f'Wrote {args.output} ({min(len(text.layout), args.max_pages)} of {text.page_count} pages). '
          f'Open it and check nothing personal remains before sharing.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
