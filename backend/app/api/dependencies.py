from fastapi import Request

from ..refresh.manager import BackgroundRefreshManager


def get_refresh_manager(request: Request) -> BackgroundRefreshManager:
    return request.app.state.refresh_manager
