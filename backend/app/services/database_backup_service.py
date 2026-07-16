from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import PROJECT_ROOT, get_settings
from ..db.session import database_file_path


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
_BACKUP_LOCK = threading.Lock()
_SAFE_REASON = re.compile(r"[^a-z0-9_-]+")


class DatabaseBackupError(RuntimeError):
    pass


def backup_directory() -> Path:
    configured = Path(get_settings().database_backup_dir).expanduser()
    path = configured if configured.is_absolute() else PROJECT_ROOT / configured
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alembic_revision(path: Path) -> str | None:
    try:
        with closing(sqlite3.connect(path)) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def _validate_database(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise DatabaseBackupError(f"備份驗證失敗：{exc}") from exc
    if not result or result[0] != "ok":
        raise DatabaseBackupError(f"備份完整性檢查失敗：{result[0] if result else 'no result'}")


def create_database_backup(
    reason: str = "manual",
    *,
    prune: bool = True,
    backup_for_date: date | None = None,
) -> dict:
    source_path = database_file_path()
    if source_path is None or not source_path.exists():
        raise DatabaseBackupError("目前資料庫不是可備份的本機 SQLite 檔案。")

    safe_reason = _SAFE_REASON.sub("-", reason.strip().lower()).strip("-") or "manual"
    created_at = datetime.now(TAIPEI_TZ)
    stem = f"stock_valuation_{created_at:%Y%m%d_%H%M%S_%f}_{safe_reason}"
    destination = backup_directory() / f"{stem}.sqlite3"
    temporary = destination.with_suffix(".sqlite3.tmp")
    metadata_path = destination.with_suffix(".json")

    with _BACKUP_LOCK:
        try:
            with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
            _validate_database(temporary)
            digest = _sha256(temporary)
            os.replace(temporary, destination)
            metadata = {
                "filename": destination.name,
                "reason": safe_reason,
                "created_at": created_at.isoformat(),
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
                "alembic_revision": _alembic_revision(destination),
                "backup_for_date": backup_for_date.isoformat() if backup_for_date else None,
            }
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            if prune:
                prune_database_backups(get_settings().database_backup_retention_count)
            return metadata
        except Exception:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            raise


def list_database_backups() -> list[dict]:
    backups = []
    for database_path in backup_directory().glob("stock_valuation_*.sqlite3"):
        metadata_path = database_path.with_suffix(".json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {
                "filename": database_path.name,
                "reason": "unknown",
                "created_at": datetime.fromtimestamp(database_path.stat().st_mtime, TAIPEI_TZ).isoformat(),
                "size_bytes": database_path.stat().st_size,
                "sha256": _sha256(database_path),
                "alembic_revision": _alembic_revision(database_path),
            }
        backups.append(metadata)
    return sorted(backups, key=lambda item: item["created_at"], reverse=True)


def prune_database_backups(retention_count: int) -> None:
    for metadata in list_database_backups()[retention_count:]:
        database_path = backup_directory() / metadata["filename"]
        database_path.unlink(missing_ok=True)
        database_path.with_suffix(".json").unlink(missing_ok=True)


def backup_path_for_download(filename: str) -> Path:
    if Path(filename).name != filename or not filename.startswith("stock_valuation_") or not filename.endswith(".sqlite3"):
        raise FileNotFoundError(filename)
    path = backup_directory() / filename
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path


def daily_backup_is_due(now: datetime | None = None) -> bool:
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    settings = get_settings()
    target_date = current.date() if current.hour >= settings.database_backup_hour else current.date() - timedelta(days=1)
    return not any(
        item.get("reason") == "daily"
        and (item.get("backup_for_date") or datetime.fromisoformat(item["created_at"]).date().isoformat()) == target_date.isoformat()
        for item in list_database_backups()
    )


def ensure_daily_backup(now: datetime | None = None) -> dict | None:
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    if not daily_backup_is_due(current):
        return None
    settings = get_settings()
    target_date = current.date() if current.hour >= settings.database_backup_hour else current.date() - timedelta(days=1)
    return create_database_backup("daily", backup_for_date=target_date)
