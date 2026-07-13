from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.session import get_session
from ..refresh.manager import BackgroundRefreshManager
from ..schema.system import HealthResponse, MetadataResponse
from ..services.system_service import health_snapshot, metadata_counts
from .dependencies import get_refresh_manager


router = APIRouter()
settings = get_settings()


@router.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    snapshot = health_snapshot()
    return HealthResponse(
        status="ok",
        app_env=settings.app_env,
        database=snapshot["database"],
        api_version=settings.api_version,
    )


@router.get("/api/metadata", response_model=MetadataResponse)
async def metadata(
    session: Session = Depends(get_session),
    manager: BackgroundRefreshManager = Depends(get_refresh_manager),
) -> MetadataResponse:
    refresh = await manager.snapshot()
    counts = metadata_counts(session)
    return MetadataResponse(
        data_source="TWSE/FinMind quote + TWSE/FinMind latest-date PE + FinMind EPS/fundamentals/daily prices + Yahoo broker trading",
        api_version=settings.api_version,
        stocks_count=counts["stocks_count"],
        valuations_count=counts["valuations_count"],
        refresh_status=refresh["status"],
        refresh_interval_seconds=settings.quote_market_interval_seconds,
        auto_refresh_enabled=refresh["auto_refresh_enabled"],
        market_session=refresh["market_session"],
        refresh_window=refresh["refresh_window"],
        next_auto_refresh_at=refresh["next_auto_refresh_at"],
        last_refresh_finished_at=refresh["last_refresh_finished_at"],
        last_close_verification_at=refresh["last_close_verification_at"],
        latest_official_data_date=counts["latest_official_data_date"],
    )
