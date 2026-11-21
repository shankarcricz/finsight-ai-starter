import hashlib
import io
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pypdf import PdfReader
from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel
from sqlalchemy import select
from app.categorization import LLMClassifier
from app.db import session_scope
from app.investment_import import import_investment_pdf, import_investment_sheet
from app.models import Document
from app.observability import file_info, only, summarize_ingest, traceable
from app.spreadsheets import read_rows
from app.statement_import import import_statement

log = logging.getLogger(__name__)
MAX_SIZE = 10 * 1024 * 1024
MAX_PAGES = 200
VALID_CATEGORIES = {'bank', 'investment'}


@dataclass
class PdfText:
    plain: list[str]   # reading-order text: best for embeddings / RAG
    layout: list[str]  # column-preserving text: best for table parsing
    page_count: int


@traceable(name='extract_pdf_text', run_type='parser', process_inputs=lambda i: file_info(i.get('raw')),
           process_outputs=lambda t: {'pages': t.page_count, 'text_chars': sum(map(len, t.plain))})
def extract_pdf_text(raw: bytes) -> PdfText:
    if not raw.startswith(b'%PDF-'):
        raise ValueError('Not a valid PDF header')
    if len(raw) > MAX_SIZE:
        raise ValueError('PDF exceeds 10 MB')
    try:
        pdf = PdfReader(io.BytesIO(raw))
        if pdf.is_encrypted:
            raise ValueError('Encrypted PDF: export an unlocked copy locally; do not send its password')
        if len(pdf.pages) > MAX_PAGES:
            raise ValueError(f'PDF exceeds the {MAX_PAGES}-page demo limit')
        plain = [p.extract_text() or '' for p in pdf.pages]
        layout = []
        for p in pdf.pages:
            try:
                layout.append(p.extract_text(extraction_mode='layout') or '')
            except Exception:  # layout mode is stricter; structured import just sees an empty page
                layout.append('')
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Unable to read PDF') from exc
    if not any(p.strip() for p in plain):
        raise ValueError('Scanned/image-only PDF; OCR is not yet implemented')
    return PdfText(plain, layout, len(pdf.pages))


@traceable(name='index_in_pinecone', run_type='embedding', process_inputs=lambda i: {'chunks': len(i.get('chunks') or [])})
def index_in_pinecone(chunks: list[LCDocument], ids: list[str]) -> None:
    from app.rag import vector_store
    vector_store().add_documents(chunks, ids=ids, namespace='financial-docs')


def _safe_import(session, doc_id, run: Callable[[], BaseModel]) -> dict:
    """Run Pipeline B in a savepoint: a parser bug must not undo the document / RAG indexing record."""
    try:
        with session.begin_nested():
            return run().model_dump(mode='json')
    except Exception:
        log.exception('Structured import failed for document %s', doc_id)
        return {'status': 'failed', 'reconciliation_status': 'unverifiable',
                'warnings': [{'code': 'import_error', 'message': 'Structured extraction failed unexpectedly'}]}


def _structured_import(session, user_id, doc_id, category, text: PdfText, llm_classifier, platform_hint):
    if category == 'bank':
        return _safe_import(session, doc_id, lambda: import_statement(
            session, user_id=user_id, document_id=doc_id, layout_pages=text.layout, llm_classifier=llm_classifier))
    return _safe_import(session, doc_id, lambda: import_investment_pdf(
        session, user_id=user_id, document_id=doc_id, layout_pages=text.layout, platform_hint=platform_hint))


@traceable(name='ingest_spreadsheet', process_inputs=lambda i: {**file_info(i.get('raw'), i.get('filename')),
           'platform_hint': i.get('platform_hint')}, process_outputs=summarize_ingest)
def ingest_spreadsheet(raw: bytes, filename: str, user_id: str, *, platform_hint: str | None = None) -> dict:
    """Broker exports (.xlsx/.csv) go straight to SQL; they are never embedded or sent to an LLM."""
    if len(raw) > MAX_SIZE:
        raise ValueError('File exceeds 10 MB')
    _, rows = read_rows(raw, filename)
    digest = hashlib.sha256(raw).hexdigest()
    with session_scope() as session:
        doc = session.scalar(select(Document).where(
            Document.user_id == user_id, Document.sha256 == digest, Document.category == 'investment'))
        duplicate = doc is not None
        if doc is None:
            doc = Document(id=str(uuid.uuid4()), user_id=user_id, category='investment', filename=filename[:200],
                           sha256=digest, pages=1, indexed_in_rag=False)
            session.add(doc)
            session.flush()
        structured = _safe_import(session, doc.id, lambda: import_investment_sheet(
            session, user_id=user_id, document_id=doc.id, rows=rows, filename=filename, platform_hint=platform_hint))
        return {'document_id': doc.id, 'duplicate': duplicate, 'chunks': 0, 'indexed_in_rag': False,
                'structured': structured}


def _chunks(text: PdfText, user_id: str, doc_id: str, category: str, filename: str) -> list[LCDocument]:
    documents = [
        LCDocument(page_content=page, metadata={
            'user_id': user_id, 'document_id': doc_id, 'category': category,
            'filename': filename[:200], 'page': i + 1,
        }) for i, page in enumerate(text.plain) if page.strip()
    ]
    return RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150).split_documents(documents)


@traceable(name='ingest_pdf', process_inputs=lambda i: {**file_info(i.get('raw'), i.get('filename')),
           **only('category', 'local_only', 'platform_hint')(i), 'llm_categorization': i.get('llm_classifier') is not None},
           process_outputs=summarize_ingest)
def ingest_pdf(raw: bytes, filename: str, category: str, user_id: str, *,
               index_chunks: Callable[[list[LCDocument], list[str]], None] | None = None,
               llm_classifier: LLMClassifier | None = None, local_only: bool = False,
               platform_hint: str | None = None) -> dict:
    """local_only=True keeps the document entirely on this machine: no embeddings, no Pinecone,
    and no LLM categorization (the caller passes llm_classifier=None)."""
    index_chunks = index_chunks or index_in_pinecone
    if category not in VALID_CATEGORIES:
        raise ValueError('category must be bank or investment')
    text = extract_pdf_text(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if local_only:
        llm_classifier = None

    with session_scope() as session:
        existing = session.scalar(select(Document).where(
            Document.user_id == user_id, Document.sha256 == digest, Document.category == category))
        if existing:
            result = {'document_id': existing.id, 'pages': existing.pages, 'duplicate': True, 'chunks': 0}
            if not existing.indexed_in_rag and not local_only:
                # First uploaded local-only; the user now opted in to RAG for it.
                chunks = _chunks(text, user_id, existing.id, category, existing.filename)
                index_chunks(chunks, [f'{existing.id}-{i}' for i in range(len(chunks))])
                existing.indexed_in_rag = True
                result['chunks'] = len(chunks)
            result['indexed_in_rag'] = existing.indexed_in_rag
            # Imports are idempotent per document, so this also back-fills uploads made before a parser existed.
            result['structured'] = _structured_import(session, user_id, existing.id, category, text,
                                                      llm_classifier, platform_hint)
            return result

    doc = Document(id=str(uuid.uuid4()), user_id=user_id, category=category, filename=filename[:200],
                   sha256=digest, pages=text.page_count, indexed_in_rag=not local_only)
    chunks: list[LCDocument] = []
    if not local_only:
        chunks = _chunks(text, user_id, doc.id, category, filename)
        index_chunks(chunks, [f'{doc.id}-{i}' for i in range(len(chunks))])

    with session_scope() as session:
        session.add(doc)
        session.flush()
        result = {'document_id': doc.id, 'pages': text.page_count, 'chunks': len(chunks), 'duplicate': False,
                  'indexed_in_rag': not local_only}
        result['structured'] = _structured_import(session, user_id, doc.id, category, text, llm_classifier,
                                                  platform_hint)
    return result
