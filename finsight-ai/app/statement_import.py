"""Pipeline B orchestration: parse -> validate -> categorize -> reconcile -> persist (idempotently)."""
import hashlib
import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.categorization import LLMClassifier, categorize
from app.models import BankTransactionRecord, FinancialAccount, MerchantCategoryOverride, StatementImport
from app.observability import only, summarize_import, traceable
from app.parsers import choose_parser
from app.parsers.normalize import normalize_merchant
from app.reconciliation import reconcile
from app.schemas import ExtractionWarning, ParsedTransaction


class ImportSummary(BaseModel):
    import_id: str
    document_id: str
    parser: str
    status: str
    reconciliation_status: str
    institution: str | None
    account: str | None
    period_start: date | None
    period_end: date | None
    currency: str
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    transactions_found: int
    transactions_inserted: int
    duplicates_skipped: int
    checks: list[dict]
    warnings: list[dict]


def summarize(si: StatementImport) -> ImportSummary:
    recon = json.loads(si.reconciliation_json or '{}')
    return ImportSummary(
        import_id=si.id, document_id=si.document_id, parser=si.parser, status=si.status,
        reconciliation_status=si.reconciliation_status,
        institution=si.account.institution if si.account else None,
        account=si.account.account_mask if si.account else None,
        period_start=si.period_start, period_end=si.period_end, currency=si.currency,
        opening_balance=si.opening_balance, closing_balance=si.closing_balance,
        transactions_found=si.transactions_found, transactions_inserted=si.transactions_inserted,
        duplicates_skipped=si.duplicates_skipped, checks=recon.get('checks', []),
        warnings=json.loads(si.warnings_json or '[]'),
    )


def fingerprint(account_id: str, t: ParsedTransaction, occurrence: int) -> str:
    """Stable identity for a transaction across overlapping statements of the same account.

    `occurrence` separates genuinely identical rows (same day, amount, narration) in one statement.
    """
    key = '|'.join([account_id, t.transaction_date.isoformat(), t.transaction_type, str(t.amount),
                    ' '.join(t.description.upper().split()), str(t.balance_after_transaction), str(occurrence)])
    return hashlib.sha256(key.encode()).hexdigest()


def _account(session: Session, user_id: str, institution: str, mask: str, currency: str) -> FinancialAccount:
    account = session.scalar(select(FinancialAccount).where(
        FinancialAccount.user_id == user_id, FinancialAccount.institution == institution,
        FinancialAccount.account_mask == mask))
    if account is None:
        account = FinancialAccount(user_id=user_id, institution=institution, account_mask=mask, currency=currency)
        session.add(account)
        session.flush()
    return account


@traceable(name='import_bank_statement', run_type='parser', process_outputs=summarize_import,
           process_inputs=lambda i: {'document_id': i.get('document_id'), 'pages': len(i.get('layout_pages') or []),
                                     'llm_categorization': i.get('llm_classifier') is not None})
def import_statement(session: Session, *, user_id: str, document_id: str, layout_pages: list[str],
                     llm_classifier: LLMClassifier | None = None) -> ImportSummary:
    existing = session.scalar(select(StatementImport).where(StatementImport.document_id == document_id))
    if existing and existing.status == 'unsupported_layout':
        session.delete(existing)  # retry: a newer parser may understand this layout now
        session.flush()
    elif existing:
        return summarize(existing)

    parser = choose_parser(layout_pages)
    parsed = parser.parse(layout_pages) if parser else None
    if parsed is None or not parsed.transactions:
        warnings = [w.model_dump() for w in (parsed.warnings if parsed else [])]
        warnings.append(ExtractionWarning(
            code='unsupported_layout',
            message='No transactions could be extracted. The document is still searchable in chat (RAG), '
                    'but it is excluded from analytics.').model_dump())
        si = StatementImport(user_id=user_id, document_id=document_id, parser=parser.name if parser else 'none',
                             status='unsupported_layout', reconciliation_status='unverifiable',
                             warnings_json=json.dumps(warnings))
        session.add(si)
        session.flush()
        return summarize(si)

    s = parsed.summary
    warnings = list(parsed.warnings)
    if not s.account_identifier:
        warnings.append(ExtractionWarning(code='account_unidentified',
                                          message='Account number not found; filed under an UNKNOWN account'))
    # PostgreSQL enforces VARCHAR lengths (SQLite does not), so clamp parsed text to column sizes.
    account = _account(session, user_id, s.institution[:80], s.account_identifier or 'UNKNOWN', s.currency[:3])
    recon = reconcile(parsed)
    overrides = dict(session.execute(
        select(MerchantCategoryOverride.merchant, MerchantCategoryOverride.category)
        .where(MerchantCategoryOverride.user_id == user_id)).all())
    categories = categorize(parsed.transactions, overrides, llm_classifier)

    known = set(session.scalars(select(BankTransactionRecord.fingerprint)
                                .where(BankTransactionRecord.account_id == account.id)))
    si = StatementImport(
        user_id=user_id, document_id=document_id, account_id=account.id, parser=parsed.parser, status='imported',
        period_start=s.period_start, period_end=s.period_end, currency=s.currency,
        opening_balance=recon.opening_balance, closing_balance=recon.closing_balance,
        stated_total_credits=s.total_credits, stated_total_debits=s.total_debits,
        reconciliation_status=recon.status, reconciliation_json=recon.model_dump_json(),
        transactions_found=len(parsed.transactions))
    session.add(si)
    session.flush()

    seen: Counter = Counter()
    inserted = skipped = review = 0
    for t, cat in zip(parsed.transactions, categories):
        base = (t.transaction_date, t.transaction_type, t.amount, t.description.upper(), t.balance_after_transaction)
        seen[base] += 1
        fp = fingerprint(account.id, t, seen[base])
        if fp in known:
            skipped += 1
            continue
        known.add(fp)
        review += t.extraction_status == 'needs_review'
        session.add(BankTransactionRecord(
            user_id=user_id, account_id=account.id, document_id=document_id, import_id=si.id,
            source_page=t.source_page, source_row=t.source_row, transaction_date=t.transaction_date,
            description=t.description, original_description=t.original_description,
            merchant=normalize_merchant(t.description), amount=t.amount, transaction_type=t.transaction_type,
            currency=s.currency, balance_after_transaction=t.balance_after_transaction,
            reference_number=t.reference_number, category=cat.category, category_source=cat.source,
            category_confidence=cat.confidence, extraction_status=t.extraction_status,
            extraction_confidence=t.extraction_confidence, fingerprint=fp))
        inserted += 1

    if skipped:
        warnings.append(ExtractionWarning(code='duplicates_skipped',
                                          message=f'{skipped} transaction(s) were already imported from another '
                                                  f'statement of this account and were skipped'))
    si.transactions_inserted, si.duplicates_skipped = inserted, skipped
    si.warnings_json = json.dumps([w.model_dump() for w in warnings])
    if recon.status != 'reconciled' or review:
        si.status = 'needs_review'
    session.flush()
    return summarize(si)
