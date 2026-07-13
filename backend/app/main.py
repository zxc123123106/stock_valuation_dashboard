"""ASGI entrypoint kept stable for ``uvicorn backend.app.main:app``."""

from .app_factory import app, create_app

__all__ = ["app", "create_app"]
