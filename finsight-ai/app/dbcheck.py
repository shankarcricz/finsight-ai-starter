"""Check the configured database: connect, apply migrations, print a summary (never the URL/password).

    python -m app.dbcheck
"""
import sys
from sqlalchemy import inspect, text
from app.db import get_engine, init_db


def main() -> int:
    engine = get_engine()
    url = engine.url
    print(f'Database: {url.get_backend_name()} | host: {url.host or "(local file)"} | name: {url.database}')
    try:
        with engine.connect() as c:
            version = c.execute(text('select version()' if url.get_backend_name() == 'postgresql'
                                     else 'select sqlite_version()')).scalar()
        print(f'Connected. Server version: {str(version)[:60]}')
        init_db()
        with engine.connect() as c:
            rev = c.execute(text('select version_num from alembic_version')).scalar()
            tables = [t for t in inspect(engine).get_table_names() if t != 'alembic_version']
            counts = {t: c.execute(text(f'select count(*) from "{t}"')).scalar() for t in sorted(tables)}
        print(f'Migrations at revision {rev}. Rows per table: {counts}')
        return 0
    except Exception as exc:  # show the error type/message, which never includes the password
        print(f'FAILED: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
