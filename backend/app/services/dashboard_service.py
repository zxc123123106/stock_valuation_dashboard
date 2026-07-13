from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import Settings
from ..refresh.manager import BackgroundRefreshManager
from ..schema.dashboard import DashboardSnapshotResponse
from ..schema.refresh import RefreshStatusResponse
from ..schema.system import MetadataResponse
from .stock_service import list_stocks
from .system_service import metadata_counts


@dataclass(frozen=True)
class SerializedDashboardSnapshot:
    revision: str
    body: bytes


class DashboardSnapshotCache:
    """Coalesces near-simultaneous browser requests without caching DB sessions."""

    def __init__(self, ttl_seconds: float = 1.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()
        self._cached_at = 0.0
        self._cached: SerializedDashboardSnapshot | None = None

    async def snapshot(
        self,
        session: Session,
        manager: BackgroundRefreshManager,
        settings: Settings,
    ) -> SerializedDashboardSnapshot:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._ttl_seconds:
            return self._cached

        async with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._cached_at < self._ttl_seconds:
                return self._cached
            result = await build_dashboard_snapshot(session, manager, settings)
            self._cached = result
            self._cached_at = time.monotonic()
            return result

    def invalidate(self) -> None:
        self._cached_at = 0.0


async def build_dashboard_snapshot(
    session: Session,
    manager: BackgroundRefreshManager,
    settings: Settings,
) -> SerializedDashboardSnapshot:
    refresh_payload = await manager.snapshot()
    refresh_status = RefreshStatusResponse(**refresh_payload)
    counts = metadata_counts(session)
    metadata = MetadataResponse(
        data_source="TWSE/FinMind quote + TWSE/FinMind latest-date PE + FinMind EPS/fundamentals/daily prices + Yahoo broker trading",
        api_version=settings.api_version,
        stocks_count=counts["stocks_count"],
        valuations_count=counts["valuations_count"],
        refresh_status=refresh_payload["status"],
        refresh_interval_seconds=settings.quote_market_interval_seconds,
        auto_refresh_enabled=refresh_payload["auto_refresh_enabled"],
        market_session=refresh_payload["market_session"],
        refresh_window=refresh_payload["refresh_window"],
        next_auto_refresh_at=refresh_payload["next_auto_refresh_at"],
        last_refresh_finished_at=refresh_payload["last_refresh_finished_at"],
        last_close_verification_at=refresh_payload["last_close_verification_at"],
        latest_official_data_date=counts["latest_official_data_date"],
    )
    base_payload = {
        "stocks": [stock.model_dump(mode="json") for stock in list_stocks(session)],
        "metadata": metadata.model_dump(mode="json"),
        "refresh_status": refresh_status.model_dump(mode="json"),
    }
    canonical = json.dumps(
        base_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    revision = hashlib.sha256(canonical).hexdigest()
    response = DashboardSnapshotResponse(revision=revision, **base_payload)
    body = json.dumps(
        response.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return SerializedDashboardSnapshot(revision=revision, body=body)
