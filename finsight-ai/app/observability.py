"""LangSmith tracing for every FinSight flow.

What gets traced (when LANGSMITH_API_KEY is set):
  - every API request: a root run "GET /api/investments/live" with user id, status code and latency
    (high-frequency polling such as the notification bell is skipped; LANGSMITH_TRACE_READS=false skips all GETs)
  - AI flows with full prompts, tool calls and answers: assistant chat, document search (RAG), weekly portfolio
    review, LLM categorisation. These are the same texts already sent to OpenAI.
  - pipelines as metadata only: uploads, PDF extraction, parsing + reconciliation, Pinecone indexing, market data,
    FII/DII collection, email delivery and scheduled jobs. Sizes, counts, statuses, check names, timings, errors;
    never file contents, statement rows, amounts or email bodies (spreadsheets and local-only PDFs stay local).

Privacy: everything sent passes through `anonymize`, which masks emails / UPI ids, PAN, Aadhaar, phone, card and
long account numbers. Firebase uids identify users (look them up in your own DB); emails are never attached.
"""
import logging
import re
import time
from collections.abc import Callable
from pathlib import PurePath
from typing import Any
import langsmith as ls
from langsmith import Client, traceable
from langsmith.anonymizer import create_anonymizer
from app.config import settings

log = logging.getLogger(__name__)

__all__ = ['traceable', 'configure', 'enabled', 'client', 'anonymize', 'TraceRequestsMiddleware', 'only',
           'summarize_import', 'summarize_ingest', 'file_info', 'tag_current_run']

# Order matters: card before Aadhaar (a card number starts with an Aadhaar-shaped prefix).
PII_RULES = [
    {'pattern': re.compile(r'[\w.+-]+@[A-Za-z][\w-]*(?:\.[\w-]+)*'), 'replace': '<email>'},       # email / UPI id
    {'pattern': re.compile(r'\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}(?:[ -]?\d{1,3})?\b'), 'replace': '<card>'},
    {'pattern': re.compile(r'\b\d{4}[ -]\d{4}[ -]\d{4}\b'), 'replace': '<aadhaar>'},
    {'pattern': re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b'), 'replace': '<pan>'},
    {'pattern': re.compile(r'(?<![\w.])(?:\+91[ -]?)?[6-9]\d{4}[ -]?\d{5}(?![\w])(?!\.\d)'), 'replace': '<phone>'},
    {'pattern': re.compile(r'(?<![\w.])\d{9,18}(?![\w])(?!\.\d)'), 'replace': '<number>'},       # account/folio/BO id
]
anonymize = create_anonymizer(PII_RULES)

_client: Client | None = None
_project_id: str | None = None


def configure(client: Client | None = None) -> bool:
    """Set up tracing once at startup. Explicitly disables tracing when no key is configured, so a stray
    LANGSMITH_TRACING env var can never send data with default (unmasked) settings."""
    global _client, _project_id
    if client is None and settings.langsmith_tracing and settings.langsmith_api_key:
        client = Client(api_key=settings.langsmith_api_key, api_url=settings.langsmith_endpoint or None,
                        anonymizer=anonymize)
    _client, _project_id = client, None
    if client is None:
        ls.configure(enabled=False, client=None)
        return False
    ls.configure(client=client, enabled=True, project_name=settings.langsmith_project,
                 tags=[f'env:{settings.app_env}'], metadata={'app': 'finsight', 'env': settings.app_env})
    log.info('LangSmith tracing enabled (project "%s", PII masking on)', settings.langsmith_project)
    return True


def enabled() -> bool:
    return _client is not None


def client() -> Client | None:
    return _client


def project_id() -> str | None:
    """LangSmith project id (feedback should name its project). Looked up once; None until the project exists,
    which happens with the first trace."""
    global _project_id
    if _project_id is None and _client is not None:
        try:
            _project_id = str(_client.read_project(project_name=settings.langsmith_project).id)
        except Exception:
            return None
    return _project_id


def current_trace_id() -> str | None:
    run = ls.get_current_run_tree()
    return str(run.trace_id) if run is not None else None


def tag_current_run(**metadata: Any) -> None:
    """Attach metadata (e.g. user_id) to the run currently executing, if any."""
    run = ls.get_current_run_tree()
    if run is not None:
        run.add_metadata({k: v for k, v in metadata.items() if v is not None})


# ---- input / output shaping (metadata-only pipelines) ------------------------------------------------------

def only(*keys: str) -> Callable[[dict], dict]:
    """process_inputs that keeps just the named, harmless arguments (drops sessions, bytes, page text)."""
    return lambda inputs: {k: inputs.get(k) for k in keys if k in inputs}


def file_info(raw: bytes | None, filename: str | None = None) -> dict:
    return {'size_bytes': len(raw) if raw is not None else None,
            'file_type': (PurePath(filename).suffix.lower() or None) if filename else None}


_IMPORT_KEYS = ('import_id', 'document_id', 'parser', 'kind', 'platform', 'status', 'reconciliation_status',
                'period_start', 'period_end', 'as_of', 'currency', 'transactions_found', 'transactions_inserted',
                'rows_found', 'rows_inserted', 'duplicates_skipped')


def summarize_import(result: Any) -> dict:
    """Allowlist of an import summary: statuses, counts and check names. No amounts, accounts or rows."""
    data = result.model_dump(mode='json') if hasattr(result, 'model_dump') else dict(result or {})
    out = {k: data[k] for k in _IMPORT_KEYS if k in data}
    out['checks'] = [{'name': c.get('name'), 'status': c.get('status')} for c in data.get('checks') or []]
    out['warnings'] = [w.get('code') for w in data.get('warnings') or []]
    return out


def summarize_ingest(result: dict) -> dict:
    out = {k: result.get(k) for k in ('document_id', 'pages', 'chunks', 'duplicate', 'indexed_in_rag') if k in result}
    if isinstance(result.get('structured'), dict):
        out['structured'] = summarize_import(result['structured'])
    return out


# ---- one root run per API request -------------------------------------------------------------------------

SKIP_PATHS = ('/static', '/health', '/favicon.ico', '/api/auth/config', '/api/categories')
SKIP_GET = ('/api/notifications',)  # polled every minute by every open tab


class TraceRequestsMiddleware:
    """Pure ASGI middleware (same task as the endpoint, so child runs nest under it)."""

    def __init__(self, app):
        self.app = app

    def _should_trace(self, scope) -> bool:
        if scope['type'] != 'http' or not enabled():
            return False
        path, method = scope['path'], scope['method']
        if path == '/' or path.startswith(SKIP_PATHS) or method in ('OPTIONS', 'HEAD'):
            return False
        if method == 'GET' and (not settings.langsmith_trace_reads or path.startswith(SKIP_GET)):
            return False
        return path.startswith('/api/')

    async def __call__(self, scope, receive, send):
        if not self._should_trace(scope):
            return await self.app(scope, receive, send)
        method, path = scope['method'], scope['path']
        query = scope.get('query_string', b'').decode('latin-1')
        status = {'code': 500}
        started = time.perf_counter()

        async def send_wrapper(message):
            if message['type'] == 'http.response.start':
                status['code'] = message['status']
            await send(message)

        with ls.trace(f'{method} {path}', run_type='chain', metadata={'kind': 'http'},
                      inputs={'method': method, 'path': path, 'query': query[:500]}) as run:
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                # Name by route template ("GET /api/reviews/{review_id}") so requests group in LangSmith; the
                # router has stored the matched route on the scope by now (the name is re-sent when the run ends).
                route = getattr(scope.get('route'), 'path', None) or path
                run.name = f'{method} {route}'
                state = scope.get('state') or {}
                code = status['code']
                run.add_metadata({'route': route, 'status_code': code, 'user_id': state.get('user_id'),
                                  'latency_ms': round((time.perf_counter() - started) * 1000)})
                if code >= 400:
                    run.add_tags([f'http_{code // 100}xx'])
                # An exception still propagates; trace() then re-ends the run with the traceback as the error.
                run.end(outputs={'status_code': code}, error=f'HTTP {code}' if code >= 500 else None)
