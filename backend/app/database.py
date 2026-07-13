"""Backward-compatible database facade.

New code should import from ``backend.app.db`` modules directly. This module is
kept for one compatibility cycle so existing scripts and tests keep working.
"""

from .db.apply import *
from .db.bootstrap import *
from .db.models import *
from .db.session import *

__all__ = [name for name in globals() if not name.startswith("__")]
