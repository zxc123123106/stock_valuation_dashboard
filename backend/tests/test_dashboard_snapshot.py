from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.api.dashboard import dashboard_snapshot
from backend.app.app_factory import create_app
from backend.app.config import get_settings
from backend.app.database import Base, Stock
from backend.app.services.dashboard_service import DashboardSnapshotCache, build_dashboard_snapshot


def refresh_payload() -> dict:
    return {
        "status": "idle",
        "current_symbol": None,
        "queue_length": 0,
        "auto_refresh_enabled": True,
        "market_session": "off_hours",
        "refresh_window": "24 小時分流排程 Asia/Taipei",
        "next_auto_refresh_at": None,
        "last_refresh_finished_at": None,
        "last_close_verification_at": None,
        "channels": {},
        "symbols": [],
    }


class DashboardSnapshotTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.manager = SimpleNamespace(snapshot=AsyncMock(return_value=refresh_payload()))

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    async def test_snapshot_reuses_one_refresh_snapshot_for_metadata_and_status(self) -> None:
        snapshot = await build_dashboard_snapshot(self.session, self.manager, get_settings())
        payload = json.loads(snapshot.body)

        self.manager.snapshot.assert_awaited_once()
        self.assertEqual(payload["metadata"]["refresh_status"], "idle")
        self.assertEqual(payload["refresh_status"]["status"], "idle")
        self.assertEqual(payload["revision"], snapshot.revision)

    async def test_revision_is_stable_and_changes_with_dashboard_data(self) -> None:
        first = await build_dashboard_snapshot(self.session, self.manager, get_settings())
        second = await build_dashboard_snapshot(self.session, self.manager, get_settings())
        self.assertEqual(first.revision, second.revision)

        self.session.add(Stock(symbol="2330", name="台積電", display_order=10))
        self.session.commit()
        third = await build_dashboard_snapshot(self.session, self.manager, get_settings())
        self.assertNotEqual(first.revision, third.revision)

    async def test_matching_etag_returns_304_without_body(self) -> None:
        cache = DashboardSnapshotCache(ttl_seconds=1)
        app = SimpleNamespace(state=SimpleNamespace(dashboard_snapshot_cache=cache))
        first_request = SimpleNamespace(app=app, headers={})
        first = await dashboard_snapshot(first_request, self.session, self.manager)

        second_request = SimpleNamespace(app=app, headers={"if-none-match": first.headers["etag"]})
        second = await dashboard_snapshot(second_request, self.session, self.manager)

        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.body, b"")
        self.assertEqual(second.headers["etag"], first.headers["etag"])

    async def test_matching_revision_query_returns_304_without_preflight_header(self) -> None:
        cache = DashboardSnapshotCache(ttl_seconds=1)
        app = SimpleNamespace(state=SimpleNamespace(dashboard_snapshot_cache=cache))
        first_request = SimpleNamespace(app=app, headers={}, query_params={})
        first = await dashboard_snapshot(first_request, self.session, self.manager)
        revision = first.headers["etag"].strip('"')

        second_request = SimpleNamespace(app=app, headers={}, query_params={"revision": revision})
        second = await dashboard_snapshot(second_request, self.session, self.manager)

        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.body, b"")

    async def test_one_second_cache_coalesces_near_simultaneous_requests(self) -> None:
        cache = DashboardSnapshotCache(ttl_seconds=1)
        await cache.snapshot(self.session, self.manager, get_settings())
        await cache.snapshot(self.session, self.manager, get_settings())
        self.manager.snapshot.assert_awaited_once()


class DashboardSnapshotAppContractTest(unittest.TestCase):
    def test_route_and_etag_cors_header_are_configured(self) -> None:
        app = create_app(manager=object())
        paths = {route.path for route in app.routes}
        self.assertIn("/api/dashboard/snapshot", paths)
        cors = next(middleware for middleware in app.user_middleware if middleware.cls is CORSMiddleware)
        self.assertIn("ETag", cors.kwargs["expose_headers"])


if __name__ == "__main__":
    unittest.main()
