from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.app_factory import create_app
from backend.app.database import Base, Stock
from backend.app.repositories import stocks as stock_repository


class RouterContractTest(unittest.TestCase):
    def test_existing_api_routes_remain_mounted(self) -> None:
        app = create_app(manager=object())
        routes = {
            (method, route.path)
            for route in app.routes
            for method in (route.methods or set())
            if route.path.startswith("/api")
        }
        expected = {
            ("GET", "/api/health"),
            ("GET", "/api/metadata"),
            ("GET", "/api/stocks"),
            ("POST", "/api/stocks/refresh"),
            ("GET", "/api/refresh/status"),
            ("GET", "/api/futures/wtx"),
            ("POST", "/api/stocks/{symbol}/ai-analysis"),
        }
        self.assertTrue(expected.issubset(routes))


class StockRepositoryTest(unittest.TestCase):
    def test_reorder_transaction_persists_display_order(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as session:
                session.add_all([
                    Stock(symbol="2330", name="台積電", display_order=10),
                    Stock(symbol="0050", name="元大台灣50", display_order=20),
                ])
                session.commit()
                stock_repository.reorder_active(session, ["0050", "2330"])
            with Session(engine) as session:
                self.assertEqual(
                    [(stock.symbol, stock.display_order) for stock in stock_repository.list_active(session)],
                    [("0050", 10), ("2330", 20)],
                )
        finally:
            engine.dispose()


class ProviderBoundaryTest(unittest.TestCase):
    def test_provider_modules_do_not_import_fastapi_or_database(self) -> None:
        providers = Path(__file__).parents[1] / "app" / "providers"
        violations: list[str] = []
        for path in providers.glob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                if any(module == "fastapi" or module.endswith(".database") or module == "database" for module in modules):
                    violations.append(f"{path.name}: {modules}")
        self.assertEqual(violations, [])


class ServiceBoundaryTest(unittest.TestCase):
    def test_service_modules_do_not_import_fastapi(self) -> None:
        services = Path(__file__).parents[1] / "app" / "services"
        violations: list[str] = []
        for path in services.glob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                if any(module == "fastapi" or module.startswith("fastapi.") for module in modules):
                    violations.append(f"{path.name}: {modules}")
        self.assertEqual(violations, [])


class LifespanInjectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_injected_refresh_manager_is_started_and_stopped(self) -> None:
        manager = unittest.mock.Mock()
        manager.start = AsyncMock()
        manager.stop = AsyncMock()
        app = create_app(manager=manager)
        with patch("backend.app.app_factory.init_database") as init_database:
            async with app.router.lifespan_context(app):
                self.assertIs(app.state.refresh_manager, manager)
                manager.start.assert_awaited_once()
            manager.stop.assert_awaited_once()
            init_database.assert_called_once()


if __name__ == "__main__":
    unittest.main()
