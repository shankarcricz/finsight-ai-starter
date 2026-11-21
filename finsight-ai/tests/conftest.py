import os
import sys
from pathlib import Path

# Must run before any `app` import: environment variables override .env, so tests never use
# real API keys, never call OpenAI/Pinecone, and never touch data/finsight.sqlite3.
os.environ['OPENAI_API_KEY'] = 'test-openai-key'
os.environ['PINECONE_API_KEY'] = 'test-pinecone-key'
os.environ['LLM_CATEGORIZATION'] = 'false'
os.environ['ENABLE_SCHEDULER'] = 'false'
os.environ['FIREBASE_PROJECT_ID'] = ''  # dev mode by default; test_auth.py enables Firebase explicitly
os.environ['ADMIN_EMAILS'] = ''
# Never send traces to LangSmith from tests (test_observability.py uses an in-memory recording client).
for _key in ('LANGSMITH_API_KEY', 'LANGSMITH_KEY', 'LANGCHAIN_API_KEY'):
    os.environ[_key] = ''
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
# Never let a test reach a real mail server configured in .env.
for _key in ('SMTP_HOST', 'SMTP_FROM', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'ALERT_TO'):
    os.environ[_key] = ''
os.environ['DATABASE_URL'] = 'sqlite:///' + (Path(__file__).parent / '.unused.sqlite3').as_posix()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from app.db import configure_engine, init_db, session_scope  # noqa: E402

FIXTURES = Path(__file__).parent / 'fixtures'
USER = 'test-user'


# Optional: run the suite against PostgreSQL. Point this at a DEDICATED test database (e.g. a Neon
# "test" branch) - every table in it is dropped before each test. It never reads DATABASE_URL.
TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL')


def _reset(engine):
    from sqlalchemy import inspect, text
    with engine.begin() as conn:
        for table in inspect(conn).get_table_names():
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))


@pytest.fixture(autouse=True)
def db(tmp_path):
    """Fresh database per test, built through the real Alembic migrations."""
    if TEST_DATABASE_URL:
        engine = configure_engine(TEST_DATABASE_URL)
        _reset(engine)
    else:
        engine = configure_engine('sqlite:///' + (tmp_path / 'test.sqlite3').as_posix())
    init_db()
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    from app import ratelimit
    ratelimit.reset()
    yield


@pytest.fixture
def session():
    with session_scope() as s:
        yield s


@pytest.fixture
def demo_pdf_bytes() -> bytes:
    return (FIXTURES / 'demo_statement_2026.pdf').read_bytes()


@pytest.fixture
def demo_layout_pages(demo_pdf_bytes) -> list[str]:
    from app.ingest import extract_pdf_text
    return extract_pdf_text(demo_pdf_bytes).layout
