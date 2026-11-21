"""Migrations must upgrade databases that already contain linked data (not just empty ones)."""
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from app.config import PROJECT_ROOT
from app.db import configure_engine, normalize_url


def test_provider_urls_use_psycopg_driver():
    assert normalize_url('postgres://u:p@h/db?sslmode=require') == 'postgresql+psycopg://u:p@h/db?sslmode=require'
    assert normalize_url('postgresql://u:p@h/db') == 'postgresql+psycopg://u:p@h/db'
    assert normalize_url('sqlite:///x.db') == 'sqlite:///x.db'


def upgrade(engine, revision: str) -> None:
    cfg = Config(str(PROJECT_ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(PROJECT_ROOT / 'migrations'))
    with engine.begin() as conn:
        cfg.attributes['connection'] = conn
        command.upgrade(cfg, revision)


def test_0002_upgrades_populated_db_with_leftover_temp_table(tmp_path):
    engine = configure_engine('sqlite:///' + (tmp_path / 'old.sqlite3').as_posix())
    upgrade(engine, '0001')
    with engine.begin() as c:
        c.execute(text("INSERT INTO documents(id,user_id,category,filename,sha256,pages) VALUES('d1','u','bank','s.pdf','h',1)"))
        c.execute(text("INSERT INTO financial_accounts VALUES('a1','u','BANK','XXXX0001','bank','INR','2026-01-01')"))
        c.execute(text("INSERT INTO statement_imports(id,user_id,document_id,account_id,parser,status,currency,"
                       "reconciliation_status,reconciliation_json,warnings_json,transactions_found,"
                       "transactions_inserted,duplicates_skipped,created_at) VALUES('i1','u','d1','a1','generic',"
                       "'imported','INR','reconciled','{}','[]',0,0,0,'2026-01-01')"))
        # Simulate the residue of an earlier failed batch-mode attempt.
        c.execute(text('CREATE TABLE _alembic_tmp_documents (id TEXT)'))

    upgrade(engine, 'head')

    insp = inspect(engine)
    assert '_alembic_tmp_documents' not in insp.get_table_names()
    assert 'indexed_in_rag' in {col['name'] for col in insp.get_columns('documents')}
    with engine.connect() as c:
        assert c.execute(text('SELECT id, indexed_in_rag FROM documents')).all() == [('d1', 1)]
        assert c.execute(text('SELECT count(*) FROM statement_imports')).scalar() == 1
    engine.dispose()
