from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import PROJECT_ROOT, get_settings

settings = get_settings()

def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _resolve_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername != "sqlite":
        raise ValueError("Local MVP only supports SQLite DATABASE_URL values.")

    if not url.database or url.database == ":memory:":
        return database_url

    database_path = Path(url.database)
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    return f"sqlite:///{database_path}"


def _ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if not url.database or url.database == ":memory:":
        return

    Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


DATABASE_URL = _resolve_database_url(settings.database_url)
_ensure_sqlite_parent(DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 5},
    future=True,
)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def ping_database() -> bool:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True


def database_file_path() -> Path | None:
    url = make_url(DATABASE_URL)
    if not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()



__all__ = [name for name in globals() if not name.startswith("__")]
