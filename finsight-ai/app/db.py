from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from app.config import PROJECT_ROOT, settings

SessionLocal = sessionmaker(expire_on_commit=False)
_engine: Engine | None = None


def normalize_url(url: str) -> str:
    """Providers (Neon, Supabase) hand out 'postgres://' / 'postgresql://' URLs; use the psycopg 3 driver."""
    for prefix in ('postgres://', 'postgresql://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg://' + url.removeprefix(prefix)
    return url


def configure_engine(url: str) -> Engine:
    """(Re)bind the session factory. Tests call this with a temporary database URL."""
    global _engine
    url = normalize_url(url)
    if url.startswith('sqlite:///'):
        Path(url.removeprefix('sqlite:///')).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url)
    else:
        # Serverless Postgres (e.g. Neon) suspends idle compute and drops connections: ping before use
        # and recycle connections before the provider closes them.
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5)
    if _engine.dialect.name == 'sqlite':
        @event.listens_for(_engine, 'connect')
        def _sqlite_fk(dbapi_conn, _):
            dbapi_conn.execute('PRAGMA foreign_keys=ON')
    SessionLocal.configure(bind=_engine)
    return _engine


def get_engine() -> Engine:
    return _engine or configure_engine(settings.database_url)


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def init_db() -> None:
    """Apply Alembic migrations up to head (creates the database on first run)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(PROJECT_ROOT / 'migrations'))
    with get_engine().begin() as connection:
        cfg.attributes['connection'] = connection  # migrations/env.py reuses this connection
        command.upgrade(cfg, 'head')
