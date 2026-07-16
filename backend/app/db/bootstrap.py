from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from ..brokers import DEFAULT_BROKER_ID
from ..config import get_settings
from ..valuation import difference_percent, estimate_price, valuation_status
from .apply import backfill_latest_metric_pe_from_history, next_display_order
from .migrations import run_schema_migrations
from .models import *
from .session import Base, SessionLocal, engine

settings = get_settings()
SUPPORTED_EPS_TYPES = ("TTM", "LAST_YEAR")
LEGACY_DEMO_SOURCES = {"wantgoo-demo"}
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
CRAWLER_LOG_CLEANUP_KEY = "crawler_logs_last_cleanup_at"

def init_database() -> None:
    run_schema_migrations(_prepare_legacy_database)
    with SessionLocal() as session:
        backfill_display_order(session)
        remove_unsupported_eps(session)
        remove_legacy_histock_eps(session)
        seed_demo_data(session)
        seed_app_settings(session)
        backfill_latest_metric_pe_from_history(session)
        session.commit()
    from ..data_quality import backfill_data_quality_states

    backfill_data_quality_states()
    run_startup_maintenance()


def _prepare_legacy_database() -> None:
    Base.metadata.create_all(engine)
    ensure_stock_display_order_column()
    ensure_stock_asset_type_column()
    ensure_stock_metric_quote_columns()
    ensure_analysis_cache_columns()
    ensure_ai_analysis_log_columns()


def ensure_stock_display_order_column() -> None:
    with engine.begin() as connection:
        columns = {
            row._mapping["name"]
            for row in connection.execute(text("PRAGMA table_info(stocks)"))
        }
        if "display_order" not in columns:
            connection.execute(text("ALTER TABLE stocks ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0"))


def ensure_stock_asset_type_column() -> None:
    with engine.begin() as connection:
        columns = {
            row._mapping["name"]
            for row in connection.execute(text("PRAGMA table_info(stocks)"))
        }
        if "asset_type" not in columns:
            connection.execute(text("ALTER TABLE stocks ADD COLUMN asset_type VARCHAR(16) NOT NULL DEFAULT 'STOCK'"))
        connection.execute(text("UPDATE stocks SET asset_type = 'STOCK' WHERE asset_type IS NULL OR asset_type = ''"))


def ensure_stock_metric_quote_columns() -> None:
    with engine.begin() as connection:
        columns = {
            row._mapping["name"]
            for row in connection.execute(text("PRAGMA table_info(stock_metrics)"))
        }
        if "open_price" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN open_price NUMERIC(12, 2)"))
        if "previous_close" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN previous_close NUMERIC(12, 2)"))
        if "day_high" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN day_high NUMERIC(12, 2)"))
        if "day_low" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN day_low NUMERIC(12, 2)"))
        if "change_percent" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN change_percent NUMERIC(8, 2)"))
        if "pe_average_3y" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN pe_average_3y NUMERIC(12, 2)"))
        if "pe_min_3y" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN pe_min_3y NUMERIC(12, 2)"))
        if "pe_max_3y" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN pe_max_3y NUMERIC(12, 2)"))
        if "pe_vs_average_percent" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN pe_vs_average_percent NUMERIC(8, 2)"))
        if "pe_data_date" not in columns:
            connection.execute(text("ALTER TABLE stock_metrics ADD COLUMN pe_data_date DATE"))


def ensure_analysis_cache_columns() -> None:
    with engine.begin() as connection:
        _ensure_table_columns(
            connection,
            "stock_pe_history",
            {
                "pbr": "NUMERIC(12, 2)",
                "dividend_yield": "NUMERIC(8, 2)",
                "source": "VARCHAR(120) NOT NULL DEFAULT 'FinMind TaiwanStockPER'",
                "fetched_at": "DATETIME",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        )
        _ensure_table_columns(
            connection,
            "stock_monthly_revenues",
            {
                "mom_percent": "NUMERIC(8, 2)",
                "yoy_percent": "NUMERIC(8, 2)",
                "source": "VARCHAR(120) NOT NULL DEFAULT 'FinMind TaiwanStockMonthRevenue'",
                "fetched_at": "DATETIME",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        )
        _ensure_table_columns(
            connection,
            "stock_financial_quarters",
            {
                "revenue": "NUMERIC(18, 2)",
                "gross_profit": "NUMERIC(18, 2)",
                "operating_income": "NUMERIC(18, 2)",
                "net_income": "NUMERIC(18, 2)",
                "source": "VARCHAR(120) NOT NULL DEFAULT 'FinMind TaiwanStockFinancialStatements'",
                "fetched_at": "DATETIME",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        )


def ensure_ai_analysis_log_columns() -> None:
    with engine.begin() as connection:
        _ensure_table_columns(
            connection,
            "stock_ai_analyses",
            {
                "analysis_mode": "VARCHAR(16) NOT NULL DEFAULT 'GENERAL'",
                "prompt_version": "VARCHAR(40) NOT NULL DEFAULT 'v1'",
                "raw_response_text": "TEXT",
                "provider_metadata_json": "TEXT",
                "validation_errors_json": "TEXT",
                "quality_flags_json": "TEXT",
                "grounding_errors_json": "TEXT",
                "run_id": "INTEGER REFERENCES stock_ai_analysis_runs(id) ON DELETE SET NULL",
            },
        )
        connection.execute(
            text("UPDATE stock_ai_analyses SET analysis_mode = 'GENERAL' WHERE analysis_mode IS NULL OR analysis_mode = ''")
        )
        connection.execute(
            text("UPDATE stock_ai_analyses SET prompt_version = 'v1' WHERE prompt_version IS NULL OR prompt_version = ''")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_stock_ai_analyses_analysis_mode ON stock_ai_analyses (analysis_mode)")
        )
        table_sql = connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'stock_ai_analyses'")
        )
        if table_sql and "uq_stock_ai_analysis_cache" in table_sql:
            _remove_legacy_ai_analysis_unique_constraint(connection)


def _remove_legacy_ai_analysis_unique_constraint(connection) -> None:
    _ensure_table_columns(
        connection,
        "stock_ai_analyses",
        {
            "quality_flags_json": "TEXT",
            "grounding_errors_json": "TEXT",
            "run_id": "INTEGER",
        },
    )
    connection.execute(text("DROP TABLE IF EXISTS stock_ai_analyses_v2"))
    connection.execute(
        text(
            """
            CREATE TABLE stock_ai_analyses_v2 (
                id INTEGER NOT NULL PRIMARY KEY,
                stock_id INTEGER NOT NULL,
                run_id INTEGER,
                provider VARCHAR(24) NOT NULL,
                model VARCHAR(120) NOT NULL,
                analysis_mode VARCHAR(16) NOT NULL DEFAULT 'GENERAL',
                prompt_version VARCHAR(40) NOT NULL DEFAULT 'v1',
                analysis_date DATE NOT NULL,
                input_hash VARCHAR(64) NOT NULL,
                request_payload_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                raw_response_text TEXT,
                provider_metadata_json TEXT,
                validation_errors_json TEXT,
                quality_flags_json TEXT,
                grounding_errors_json TEXT,
                status VARCHAR(24) NOT NULL,
                error_message VARCHAR(500),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(stock_id) REFERENCES stocks (id) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES stock_ai_analysis_runs (id) ON DELETE SET NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO stock_ai_analyses_v2 (
                id, stock_id, run_id, provider, model, analysis_mode, prompt_version,
                analysis_date, input_hash, request_payload_json, response_json,
                raw_response_text, provider_metadata_json, validation_errors_json,
                quality_flags_json, grounding_errors_json,
                status, error_message, created_at, updated_at
            )
            SELECT
                id, stock_id, run_id, provider, model, analysis_mode, prompt_version,
                analysis_date, input_hash, request_payload_json, response_json,
                raw_response_text, provider_metadata_json, validation_errors_json,
                quality_flags_json, grounding_errors_json,
                status, error_message, created_at, updated_at
            FROM stock_ai_analyses
            """
        )
    )
    connection.execute(text("DROP TABLE stock_ai_analyses"))
    connection.execute(text("ALTER TABLE stock_ai_analyses_v2 RENAME TO stock_ai_analyses"))
    connection.execute(text("CREATE INDEX ix_stock_ai_analyses_stock_id ON stock_ai_analyses (stock_id)"))
    connection.execute(text("CREATE INDEX ix_stock_ai_analyses_run_id ON stock_ai_analyses (run_id)"))
    connection.execute(text("CREATE INDEX ix_stock_ai_analyses_provider ON stock_ai_analyses (provider)"))
    connection.execute(text("CREATE INDEX ix_stock_ai_analyses_analysis_mode ON stock_ai_analyses (analysis_mode)"))
    connection.execute(text("CREATE INDEX ix_stock_ai_analyses_analysis_date ON stock_ai_analyses (analysis_date)"))
    connection.execute(text("CREATE INDEX ix_stock_ai_analyses_input_hash ON stock_ai_analyses (input_hash)"))


def _ensure_table_columns(connection, table_name: str, column_definitions: dict[str, str]) -> None:
    columns = {
        row._mapping["name"]
        for row in connection.execute(text(f"PRAGMA table_info({table_name})"))
    }
    if not columns:
        return
    for column_name, column_definition in column_definitions.items():
        if column_name not in columns:
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"))


def seed_app_settings(session: Session) -> None:
    if not session.get(AppSetting, "selected_broker"):
        session.add(AppSetting(key="selected_broker", value=DEFAULT_BROKER_ID))
        session.commit()


def get_app_setting(session: Session, key: str, default: str | None = None) -> str | None:
    setting = session.get(AppSetting, key)
    return setting.value if setting else default


def set_app_setting(session: Session, key: str, value: str) -> AppSetting:
    setting = session.get(AppSetting, key)
    if not setting:
        setting = AppSetting(key=key, value=value)
        session.add(setting)
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)
    session.commit()
    return setting


def backfill_display_order(session: Session) -> None:
    stocks = session.scalars(select(Stock).order_by(Stock.symbol)).all()
    if not stocks:
        return

    display_orders = [stock.display_order for stock in stocks]
    needs_backfill = any(order is None or order <= 0 for order in display_orders) or len(set(display_orders)) != len(display_orders)
    if not needs_backfill:
        return

    for index, stock in enumerate(stocks, start=1):
        stock.display_order = index * 10
    session.commit()


def seed_demo_data(session: Session) -> None:
    existing_stock = session.scalar(select(Stock).where(Stock.symbol == "2330"))
    if existing_stock:
        replace_legacy_demo_data(session, existing_stock)
        session.commit()
        return

    create_seed_snapshot(session)
    session.commit()


def replace_legacy_demo_data(session: Session, stock: Stock) -> None:
    latest_metric = session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock.id)
        .order_by(StockMetric.created_at.desc())
        .limit(1)
    )
    has_legacy_eps = session.scalar(
        select(StockEPS)
        .where(StockEPS.stock_id == stock.id, StockEPS.source.in_(LEGACY_DEMO_SOURCES))
        .limit(1)
    )
    if not latest_metric or latest_metric.source in LEGACY_DEMO_SOURCES or has_legacy_eps:
        session.execute(delete(StockMetric).where(StockMetric.stock_id == stock.id))
        session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
        session.execute(delete(StockValuation).where(StockValuation.stock_id == stock.id))
        create_seed_snapshot(session, stock)


def create_seed_snapshot(session: Session, stock: Stock | None = None) -> None:
    now = datetime.now(TAIPEI_TZ)
    source = "demo-snapshot"
    if stock is None:
        stock = Stock(
            symbol="2330",
            name="台積電",
            asset_type="STOCK",
            market="TWSE",
            currency="TWD",
            display_order=next_display_order(session),
        )
        session.add(stock)
        session.flush()
    else:
        stock.name = "台積電"
        stock.asset_type = "STOCK"
        stock.market = "TWSE"
        stock.currency = "TWD"
        stock.is_active = True
        if stock.display_order <= 0:
            stock.display_order = next_display_order(session)
        stock.updated_at = now

    metric = StockMetric(
        stock_id=stock.id,
        open_price=Decimal("2400.00"),
        previous_close=Decimal("2380.00"),
        day_high=Decimal("2440.00"),
        day_low=Decimal("2390.00"),
        current_price=Decimal("2425.00"),
        change_percent=Decimal("1.04"),
        current_pe=Decimal("32.00"),
        price_updated_at=now,
        pe_updated_at=now,
        source=source,
    )
    session.add(metric)

    eps_rows = [
        StockEPS(
            stock_id=stock.id,
            eps_type="TTM",
            eps_value=Decimal("74.39"),
            eps_period="2026Q1 + 2025Q4 + 2025Q3 + 2025Q2",
            source=source,
            eps_updated_at=now,
        ),
        StockEPS(
            stock_id=stock.id,
            eps_type="LAST_YEAR",
            eps_value=Decimal("66.26"),
            eps_period="2025",
            source=source,
            eps_updated_at=now,
        ),
    ]
    session.add_all(eps_rows)

    for eps_row in eps_rows:
        estimated = estimate_price(eps_row.eps_value, metric.current_pe)
        price_difference = estimated - metric.current_price
        percent = difference_percent(metric.current_price, estimated)
        session.add(
            StockValuation(
                stock_id=stock.id,
                eps_type=eps_row.eps_type,
                current_price=metric.current_price,
                current_pe=metric.current_pe,
                eps_value=eps_row.eps_value,
                estimated_price=estimated,
                price_difference=price_difference,
                difference_percent=percent,
                valuation_status=valuation_status(percent),
                source=source,
                calculated_at=now,
            )
        )

    session.add(
        CrawlerLog(
            job_name="seed_demo_data",
            status="SUCCESS",
            message="Seeded demo valuation data for 2330.",
            started_at=now,
            finished_at=now,
        )
    )


def remove_unsupported_eps(session: Session, stock_id: int | None = None) -> None:
    eps_statement = delete(StockEPS).where(StockEPS.eps_type.notin_(SUPPORTED_EPS_TYPES))
    valuation_statement = delete(StockValuation).where(StockValuation.eps_type.notin_(SUPPORTED_EPS_TYPES))

    if stock_id is not None:
        eps_statement = eps_statement.where(StockEPS.stock_id == stock_id)
        valuation_statement = valuation_statement.where(StockValuation.stock_id == stock_id)

    session.execute(eps_statement)
    session.execute(valuation_statement)


def remove_legacy_histock_eps(session: Session) -> None:
    stock_ids = session.scalars(
        select(StockEPS.stock_id)
        .where(func.lower(StockEPS.source).contains("histock"))
        .distinct()
    ).all()
    if not stock_ids:
        return

    session.execute(delete(StockEPS).where(StockEPS.stock_id.in_(stock_ids)))
    session.execute(delete(StockValuation).where(StockValuation.stock_id.in_(stock_ids)))
    session.commit()


def run_startup_maintenance() -> None:
    from ..services.database_backup_service import ensure_daily_backup

    reset_interrupted_refresh_states()
    cleanup_crawler_logs_if_due()
    ensure_daily_backup()


def reset_interrupted_refresh_states() -> None:
    now = datetime.now(UTC)
    with SessionLocal() as session:
        states = session.scalars(
            select(StockRefreshState).where(StockRefreshState.status.in_(("queued", "running", "refreshing")))
        ).all()
        for state in states:
            state.status = "failed"
            state.message = "前次更新未完成，使用快取"
            state.last_error = "Backend restarted before the refresh finished."
            state.finished_at = now
            state.updated_at = now
        quality_states = session.scalars(
            select(StockDataQualityState).where(StockDataQualityState.sync_status.in_(("queued", "running")))
        ).all()
        for state in quality_states:
            state.sync_status = "retry_wait"
            state.last_error_summary = "前次更新未完成"
            state.last_error_detail = "Backend restarted before this data category finished refreshing."
            state.last_error_at = now
            state.failure_count = (state.failure_count or 0) + 1
            state.next_retry_at = now
            state.is_cached = state.last_success_at is not None or state.fetched_at is not None
            state.updated_at = now
        if states or quality_states:
            session.commit()


def cleanup_crawler_logs_if_due(*, force: bool = False, now: datetime | None = None) -> bool:
    current_time = now or datetime.now(UTC)
    with SessionLocal() as session:
        state = session.get(AppMaintenanceState, CRAWLER_LOG_CLEANUP_KEY)
        last_cleanup_at = _parse_maintenance_datetime(state.value) if state and state.value else None
        interval = timedelta(hours=settings.crawler_log_cleanup_interval_hours)
        if not force and last_cleanup_at and current_time - last_cleanup_at < interval:
            return False

        cutoff = current_time - timedelta(days=settings.crawler_log_retention_days)
        session.execute(
            delete(CrawlerLog).where(or_(CrawlerLog.created_at < cutoff, CrawlerLog.started_at < cutoff))
        )
        if not state:
            state = AppMaintenanceState(key=CRAWLER_LOG_CLEANUP_KEY)
            session.add(state)
        state.value = current_time.isoformat()
        state.updated_at = current_time
        session.commit()
        return True


def _parse_maintenance_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def ping_database() -> bool:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True


def count_rows(session: Session, model: type[Base]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0

__all__ = [name for name in globals() if not name.startswith("__")]
