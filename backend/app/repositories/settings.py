"""Application-setting persistence."""

from sqlalchemy.orm import Session

from ..db.bootstrap import get_app_setting, set_app_setting


def get_value(session: Session, key: str, default: str | None = None) -> str | None:
    return get_app_setting(session, key, default)


def set_value(session: Session, key: str, value: str):
    return set_app_setting(session, key, value)
