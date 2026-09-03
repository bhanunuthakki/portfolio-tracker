"""SQLAlchemy engine + session factory.

Uses a single shared engine. `SessionLocal` is the session factory; `get_session`
is a FastAPI dependency that yields a session and closes it on request teardown.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from portfolio_tracker.config import get_settings


def build_engine() -> Engine:
    settings = get_settings()
    url = settings.resolved_database_url
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    built_engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(built_engine, "connect", _enable_sqlite_foreign_keys)
    return built_engine


def _enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
    """Enable declared foreign-key constraints on every SQLite connection."""
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


engine: Engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
