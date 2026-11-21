from app.parsers.base import StatementParser
from app.parsers.generic import GenericTableParser
from app.parsers.payment_app import PaymentAppParser

# Bank-specific parsers (HDFC, ICICI, SBI, ...) go before the generic fallback once real
# (redacted) sample layouts and tests exist for them.
PARSERS: list[StatementParser] = [PaymentAppParser(), GenericTableParser()]


def choose_parser(pages: list[str]) -> StatementParser | None:
    scored = [(p.detect(pages), p) for p in PARSERS]
    score, parser = max(scored, key=lambda s: s[0])
    return parser if score > 0 else None
