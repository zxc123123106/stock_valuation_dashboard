from fastapi import Request

from ..refresh.manager import BackgroundRefreshManager
from ..services.dashboard_service import DashboardSnapshotCache


def get_refresh_manager(request: Request) -> BackgroundRefreshManager:
    return request.app.state.refresh_manager


def get_dashboard_snapshot_cache(request: Request) -> DashboardSnapshotCache:
    return request.app.state.dashboard_snapshot_cache
