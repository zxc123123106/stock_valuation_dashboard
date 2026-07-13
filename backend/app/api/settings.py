from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db.session import get_session
from ..schema.settings import BrokerSettingRequest, BrokerSettingResponse
from ..services import settings_service
from ..services.dashboard_service import DashboardSnapshotCache
from .dependencies import get_dashboard_snapshot_cache


router = APIRouter()


@router.get("/api/settings/broker", response_model=BrokerSettingResponse)
def get_broker_setting(session: Session = Depends(get_session)) -> BrokerSettingResponse:
    return settings_service.get_broker_setting(session)


@router.put("/api/settings/broker", response_model=BrokerSettingResponse)
def update_broker_setting(
    payload: BrokerSettingRequest,
    session: Session = Depends(get_session),
    snapshot_cache: DashboardSnapshotCache = Depends(get_dashboard_snapshot_cache),
) -> BrokerSettingResponse:
    try:
        response = settings_service.update_broker_setting(session, payload.broker_id)
        snapshot_cache.invalidate()
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
