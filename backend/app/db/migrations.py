from __future__ import annotations

from collections.abc import Callable

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from ..config import PROJECT_ROOT
from ..config import get_settings
from ..services.database_backup_service import create_database_backup, prune_database_backups
from .session import engine


BASELINE_REVISION = "0001_current_schema"


def alembic_config() -> Config:
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def migration_head() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head() or ""


def current_revision() -> str | None:
    with engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        if "alembic_version" not in tables:
            return None
        return connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))


def database_has_application_tables() -> bool:
    with engine.connect() as connection:
        return any(name != "alembic_version" for name in inspect(connection).get_table_names())


def run_schema_migrations(prepare_legacy: Callable[[], None]) -> None:
    config = alembic_config()
    current = current_revision()
    head = migration_head()

    created_safety_backup = False
    if current is None and database_has_application_tables():
        create_database_backup("pre-migration", prune=False)
        created_safety_backup = True
        prepare_legacy()
        command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")
    elif current is None:
        command.upgrade(config, "head")
    elif current != head:
        create_database_backup("pre-migration", prune=False)
        created_safety_backup = True
        command.upgrade(config, "head")

    verify_database_schema()
    if created_safety_backup:
        prune_database_backups(get_settings().database_backup_retention_count)


def verify_database_schema() -> None:
    with engine.connect() as connection:
        integrity = connection.scalar(text("PRAGMA integrity_check"))
        foreign_key_issues = list(connection.execute(text("PRAGMA foreign_key_check")))
        journal_mode = str(connection.scalar(text("PRAGMA journal_mode")) or "").lower()
        busy_timeout = int(connection.scalar(text("PRAGMA busy_timeout")) or 0)
        foreign_keys = int(connection.scalar(text("PRAGMA foreign_keys")) or 0)
    problems = []
    if integrity != "ok":
        problems.append(f"integrity_check={integrity}")
    if foreign_key_issues:
        problems.append(f"foreign_key_check={len(foreign_key_issues)} issue(s)")
    if journal_mode != "wal":
        problems.append(f"journal_mode={journal_mode}")
    if busy_timeout < 5000:
        problems.append(f"busy_timeout={busy_timeout}")
    if foreign_keys != 1:
        problems.append("foreign_keys=off")
    if problems:
        raise RuntimeError("SQLite database verification failed: " + ", ".join(problems))
