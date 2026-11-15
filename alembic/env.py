"""Alembic env — wires the migration runner to our SQLAlchemy metadata + DATABASE_URL."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from portfolio_tracker.config import get_settings
from portfolio_tracker.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A caller may pass an open connection (the schema_migrate rehearsal and apply
# paths do); only fall back to settings when it does not.
_given_connection = config.attributes.get("connection")
if _given_connection is None:
    config.set_main_option("sqlalchemy.url", get_settings().resolved_database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite-friendly
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if _given_connection is not None:
        context.configure(
            connection=_given_connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
