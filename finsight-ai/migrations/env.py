from alembic import context
from sqlalchemy import create_engine
from app.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option('sqlalchemy.url')
    if url:
        return url
    from app.config import settings
    return settings.database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    # render_as_batch lets ALTER TABLE migrations work on SQLite as well as PostgreSQL.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    shared = config.attributes.get('connection')  # passed in by app.db.init_db()
    if shared is not None:
        _migrate(shared)
        return
    engine = create_engine(_url())  # `alembic upgrade head` from the CLI
    with engine.connect() as connection:
        _migrate(connection)
        connection.commit()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
