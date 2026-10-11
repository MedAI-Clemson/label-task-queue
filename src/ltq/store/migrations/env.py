"""Alembic's environment: which database to change and which tables it should have.

The database URL is, in order: ``sqlalchemy.url`` if the caller set it on the Alembic
config (tests do), else the ``DATABASE_URI`` environment variable, else ``ltq.db`` in the
current directory. The engine comes from ``ltq.store.connect`` so the SQLite settings (WAL,
busy timeout, explicit transactions) are the same as the application's.

``target_metadata`` is what ``alembic revision --autogenerate`` compares the database to.
Autogenerate does not see triggers or partial-index conditions; add those to the new
revision by hand (see ``versions/0001_initial.py``).
"""

import os
from logging.config import fileConfig

from alembic import context

from ltq.store.connection import connect
from ltq.store.schema import JsonText, build_metadata

DEFAULT_URL = "sqlite:///ltq.db"

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = build_metadata()


def database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URI") or DEFAULT_URL


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Write our JSON-as-text column type as plain ``sa.Text()``, so a migration does not
    import application code (a migration is a frozen snapshot)."""
    if type_ == "type" and isinstance(obj, JsonText):
        return "sa.Text()"
    return False


def run_migrations_offline() -> None:
    """Write the SQL to the output instead of running it."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_item=render_item,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = connect(database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_item=render_item,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
