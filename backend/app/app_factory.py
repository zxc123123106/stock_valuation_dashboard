from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import ai, dashboard, data_management, futures, refresh, settings as settings_router, stocks, system
from .config import get_settings
from .db.bootstrap import init_database
from .services.refresh_service import create_refresh_manager
from .services.dashboard_service import DashboardSnapshotCache


settings = get_settings()
refresh_manager = create_refresh_manager(settings)


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_database()
    manager = application.state.refresh_manager
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


def create_app(manager=None) -> FastAPI:
    application = FastAPI(
        title="Stock Valuation Dashboard API",
        version=settings.api_version,
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if "*" in settings.cors_origins else settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
        expose_headers=["ETag"],
    )
    application.state.refresh_manager = manager or refresh_manager
    application.state.dashboard_snapshot_cache = DashboardSnapshotCache(ttl_seconds=1.0)
    application.state.data_import_in_progress = False

    @application.middleware("http")
    async def reject_writes_during_import(request, call_next):
        if (
            application.state.data_import_in_progress
            and request.method in {"POST", "PUT", "DELETE"}
            and request.url.path != "/api/data-management/import"
        ):
            return JSONResponse(status_code=503, content={"detail": "資料匯入進行中，請稍候。"})
        return await call_next(request)

    for api_router in (
        system.router,
        dashboard.router,
        data_management.router,
        settings_router.router,
        futures.router,
        stocks.router,
        refresh.router,
        ai.router,
    ):
        application.include_router(api_router)
    return application


app = create_app()
