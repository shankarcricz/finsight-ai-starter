"""Build tiny text PDFs for tests (monospace font, so spaces in a line become column gaps)."""


def _escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def make_pdf(pages: list[list[str]]) -> bytes:
    objects: list[bytes] = []
    n_pages = len(pages)
    font_id = 3
    page_ids = [4 + 2 * i for i in range(n_pages)]
    objects.append(b'<< /Type /Catalog /Pages 2 0 R >>')
    kids = ' '.join(f'{p} 0 R' for p in page_ids)
    objects.append(f'<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>'.encode())
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>')
    for i, lines in enumerate(pages):
        body = 'BT /F1 8 Tf 11 TL 20 800 Td ' + ' '.join(f'({_escape(l)}) Tj T*' for l in lines) + ' ET'
        stream = body.encode('latin-1')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 842] '
                       f'/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {page_ids[i] + 1} 0 R >>'.encode())
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for num, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f'{num} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    for off in offsets:
        out += f'{off:010d} 00000 n \n'.encode()
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return bytes(out)


def row(date: str, narration: str, debit: str, credit: str, balance: str) -> str:
    """One table row with fixed-width, right-aligned money columns."""
    return f'{date:<12}  {narration:<34}  {debit:>12}  {credit:>12}  {balance:>14}'


HEADER = row('Date', 'Narration', 'Withdrawal', 'Deposit', 'Balance')
