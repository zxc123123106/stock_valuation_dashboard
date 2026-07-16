from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from ..db.session import get_session
from ..db.bootstrap import reset_interrupted_refresh_states
from ..refresh.manager import BackgroundRefreshManager
from ..schema.data_management import (
    DataManagementStatusResponse,
    DatabaseBackupResponse,
    UserDataDocument,
    UserDataImportPreviewRequest,
    UserDataImportPreviewResponse,
    UserDataImportRequest,
    UserDataImportResponse,
)
from ..services import data_management_service
from ..services.ai_service import acquire_ai_analysis_job_lock, release_ai_analysis_job_lock
from ..services.dashboard_service import DashboardSnapshotCache
from ..services.database_backup_service import (
    DatabaseBackupError,
    backup_path_for_download,
    create_database_backup,
    list_database_backups,
)
from .dependencies import get_dashboard_snapshot_cache, get_refresh_manager


router = APIRouter(prefix="/api/data-management", tags=["data-management"])


@router.get("/status", response_model=DataManagementStatusResponse)
def status(request: Request, session: Session = Depends(get_session)) -> DataManagementStatusResponse:
    return DataManagementStatusResponse(
        **data_management_service.database_status(
            session,
            import_in_progress=bool(getattr(request.app.state, "data_import_in_progress", False)),
        )
    )


@router.get("/backups", response_model=list[DatabaseBackupResponse])
def backups() -> list[DatabaseBackupResponse]:
    return [DatabaseBackupResponse(**item) for item in list_database_backups()]


@router.post("/backups", response_model=DatabaseBackupResponse)
async def create_backup() -> DatabaseBackupResponse:
    try:
        metadata = await asyncio.to_thread(create_database_backup, "manual")
    except DatabaseBackupError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return DatabaseBackupResponse(**metadata)


@router.get("/backups/{filename}")
def download_backup(filename: str) -> FileResponse:
    try:
        path = backup_path_for_download(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="找不到指定的資料庫備份。") from exc
    return FileResponse(path, filename=path.name, media_type="application/vnd.sqlite3")


@router.get("/export")
def export_user_data(session: Session = Depends(get_session)) -> JSONResponse:
    document = data_management_service.export_user_data(session)
    filename = f"stock-dashboard-user-data-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    return JSONResponse(
        document.model_dump(mode="json"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import/preview", response_model=UserDataImportPreviewResponse)
def preview_import(
    payload: UserDataImportPreviewRequest,
    session: Session = Depends(get_session),
) -> UserDataImportPreviewResponse:
    try:
        return UserDataImportPreviewResponse(**data_management_service.preview_import(session, payload.document))
    except (ValueError, data_management_service.ImportValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import", response_model=UserDataImportResponse)
async def import_user_data(
    payload: UserDataImportRequest,
    request: Request,
    session: Session = Depends(get_session),
    manager: BackgroundRefreshManager = Depends(get_refresh_manager),
    snapshot_cache: DashboardSnapshotCache = Depends(get_dashboard_snapshot_cache),
) -> UserDataImportResponse:
    if getattr(request.app.state, "data_import_in_progress", False):
        raise HTTPException(status_code=409, detail="另一個資料匯入正在進行。")

    request.app.state.data_import_in_progress = True
    ai_job_lock_acquired = False
    try:
        await asyncio.to_thread(acquire_ai_analysis_job_lock)
        ai_job_lock_acquired = True
        await manager.stop()
        await asyncio.to_thread(reset_interrupted_refresh_states)
        backup = await asyncio.to_thread(create_database_backup, "pre-import")
        result = data_management_service.apply_import(
            session,
            payload.document,
            preview_hash=payload.preview_hash,
            expected_revision=payload.expected_revision,
            confirm_replace=payload.confirm_replace,
        )
        snapshot_cache.invalidate()
    except data_management_service.ImportConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (data_management_service.ImportValidationError, ValueError) as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DatabaseBackupError as exc:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise
    finally:
        try:
            await manager.start()
        finally:
            try:
                if ai_job_lock_acquired:
                    release_ai_analysis_job_lock()
            finally:
                request.app.state.data_import_in_progress = False

    for symbol in result["added_symbols"]:
        await manager.queue_symbol(symbol)
    return UserDataImportResponse(
        status="ok",
        added_symbols=result["added_symbols"],
        retained_symbols=result["retained_symbols"],
        removed_symbols=result["removed_symbols"],
        backup_filename=backup["filename"],
    )
