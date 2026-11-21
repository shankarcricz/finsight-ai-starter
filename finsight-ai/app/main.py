import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, File, Form, Request, UploadFile, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from app import jobs, observability, ratelimit
from app.agent import make_agent
from app.auth import CurrentUser, check_auth_config, is_admin
from app.config import settings
from app.db import init_db, session_scope
from app.finance import HoldingIn, upsert_holding, portfolio
from app.ingest import ingest_pdf, ingest_spreadsheet, MAX_SIZE
from app.jobs import ensure_user
from app.rag import make_rag_chain
from app.routers import alerts, bank, investments, markets, onboarding
from app.spreadsheets import SPREADSHEET_EXTENSIONS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
observability.configure()  # before any LangChain call, so every run uses the masked client
scheduler = AsyncIOScheduler(timezone='Asia/Kolkata')
STATIC = Path(__file__).parent.parent / 'static'

@asynccontextmanager
async def lifespan(app: FastAPI):
    check_auth_config()
    init_db()
    if settings.enable_scheduler:
        jobs.register(scheduler)
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if observability.client() is not None:
        observability.client().flush()  # send buffered traces before exit

app = FastAPI(title='FinSight AI (development)', lifespan=lifespan)
app.add_middleware(observability.TraceRequestsMiddleware)
app.include_router(bank.router)
app.include_router(investments.router)
app.include_router(alerts.router)
app.include_router(onboarding.router)
app.include_router(markets.router)
app.mount('/static', StaticFiles(directory=STATIC), name='static')

class ChatTurn(BaseModel):
    # Only user/assistant turns: a client can never inject a system message.
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    mode: str = 'agent'
    history: list[ChatTurn] = Field(default_factory=list, max_length=12)  # recent turns for follow-ups
    conversation_id: str | None = Field(None, pattern=r'^[A-Za-z0-9-]{8,64}$')  # groups turns into a trace thread


class FeedbackIn(BaseModel):
    run_id: uuid.UUID
    trace_id: uuid.UUID | None = None
    score: Literal[0, 1]          # thumbs down / up
    comment: str | None = Field(None, max_length=1000)

@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')

@app.get('/api/auth/config')
def auth_config():
    # Public Firebase *web* config (identifies the project; not a secret). No user data here.
    if not settings.auth_enabled:
        return {'enabled': False}
    return {'enabled': True, 'firebase': {'apiKey': settings.firebase_web_api_key,
                                          'authDomain': settings.firebase_auth_domain,
                                          'projectId': settings.firebase_project_id}}

@app.get('/api/me')
def me(request: Request, user_id: str = CurrentUser):
    # Remember the verified sign-in email: it is the only address notifications are ever sent to.
    claims = getattr(request.state, 'claims', None) or {}
    with session_scope() as session:
        if settings.auth_enabled:
            ensure_user(session, user_id, claims.get('email'), bool(claims.get('email_verified')))
        else:
            ensure_user(session, user_id)
    return {'user_id': user_id, 'auth_enabled': settings.auth_enabled, 'is_admin': is_admin(request),
            'feedback_enabled': observability.enabled()}

@app.post('/api/upload')
def upload(file: UploadFile = File(...), category: str = Form(...), local_only: bool = Form(False),
           platform: Literal['auto', 'INDMONEY', 'GROWW', 'ZERODHA'] = Form('auto'),
           user_id: str = ratelimit.UPLOAD):
    # Sync endpoint: parsing, embedding, and LLM categorization are blocking work, so FastAPI
    # runs this in a worker thread instead of stalling the event loop.
    filename = Path(file.filename or 'document.pdf').name
    lower = filename.lower()
    hint = None if platform == 'auto' else platform
    raw = file.file.read(MAX_SIZE + 1)
    try:
        if lower.endswith(SPREADSHEET_EXTENSIONS):
            if category != 'investment':
                raise HTTPException(400, 'Spreadsheets are supported for investment reports only (for now)')
            return ingest_spreadsheet(raw, filename, user_id, platform_hint=hint)
        if not lower.endswith('.pdf'):
            raise HTTPException(400, 'Upload a PDF, .xlsx or .csv file')
        classifier = None
        if settings.llm_categorization and not local_only and category == 'bank':
            from app.categorization import openai_classifier
            classifier = openai_classifier
        return ingest_pdf(raw, filename, category, user_id, llm_classifier=classifier, local_only=local_only,
                          platform_hint=hint)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

def _chat_trace(run_id: uuid.UUID) -> dict | None:
    """Ids the browser needs to attach 👍/👎 feedback to this answer in LangSmith."""
    if not observability.enabled():
        return None
    return {'run_id': str(run_id), 'trace_id': observability.current_trace_id()}


@app.post('/api/chat')
def chat(request: ChatIn, user_id: str = ratelimit.CHAT):
    if request.mode not in ('rag', 'agent'):
        raise HTTPException(400, 'mode must be rag or agent')
    run_id = uuid.uuid4()
    # LangChain run config: names the run, fixes its id (for feedback) and groups turns into a LangSmith thread.
    config = {'run_id': run_id, 'run_name': 'assistant_chat' if request.mode == 'agent' else 'document_search',
              'tags': [f'mode:{request.mode}'],
              'metadata': {'user_id': user_id, 'mode': request.mode, 'history_turns': len(request.history),
                           **({'thread_id': request.conversation_id} if request.conversation_id else {})}}
    if request.mode == 'rag':
        answer = make_rag_chain(user_id).invoke(request.question, config=config)
        return {'answer': answer, 'trace': _chat_trace(run_id)}
    messages = [t.model_dump() for t in request.history] + [{'role': 'user', 'content': request.question}]
    result = make_agent(user_id).invoke({'messages': messages}, config=config)
    result['messages'] = result['messages'][len(request.history):]  # only report tools used for this question
    tools_used = [c['name'] for m in result['messages'] for c in (getattr(m, 'tool_calls', None) or [])]
    return {'answer': result['messages'][-1].content, 'tools_used': tools_used, 'trace': _chat_trace(run_id)}


@app.post('/api/feedback')
def feedback(body: FeedbackIn, user_id: str = ratelimit.FEEDBACK):
    """User rating of an assistant answer, stored in LangSmith next to the traced run."""
    client = observability.client()
    if client is None:
        return {'recorded': False, 'reason': 'Tracing is not configured'}
    try:
        client.create_feedback(body.run_id, key='user_rating', score=body.score, trace_id=body.trace_id,
                               session_id=observability.project_id(),
                               comment=observability.anonymize(body.comment) if body.comment else None,
                               source_info={'user_id': user_id})
    except Exception as exc:
        log.warning('LangSmith feedback failed: %s', type(exc).__name__)
        raise HTTPException(502, 'Could not record feedback right now') from exc
    return {'recorded': True}

@app.post('/api/holdings')
def save_holding(holding: HoldingIn, user_id: str = CurrentUser):
    upsert_holding(user_id, holding)
    return {'saved': True}

@app.get('/api/portfolio')
def get_portfolio(user_id: str = CurrentUser):
    return portfolio(user_id)

@app.get('/health')
def health():
    return {'status': 'ok'}
