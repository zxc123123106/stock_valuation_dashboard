from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.session import get_session
from ..refresh.manager import BackgroundRefreshManager
from ..services.dashboard_service import DashboardSnapshotCache
from .dependencies import get_refresh_manager


router = APIRouter()
settings = get_settings()


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = {candidate.strip() for candidate in if_none_match.split(",")}
    return "*" in candidates or etag in candidates or f"W/{etag}" in candidates


def _revision_matches(request: Request, revision: str) -> bool:
    requested = request.query_params.get("revision") if hasattr(request, "query_params") else None
    return requested == revision


@router.get("/api/dashboard/snapshot")
async def dashboard_snapshot(
    request: Request,
    session: Session = Depends(get_session),
    manager: BackgroundRefreshManager = Depends(get_refresh_manager),
) -> Response:
    cache: DashboardSnapshotCache = request.app.state.dashboard_snapshot_cache
    snapshot = await cache.snapshot(session, manager, settings)
    etag = f'"{snapshot.revision}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache, must-revalidate",
    }
    if _etag_matches(request.headers.get("if-none-match"), etag) or _revision_matches(request, snapshot.revision):
        return Response(status_code=304, headers=headers)
    return Response(content=snapshot.body, media_type="application/json", headers=headers)
