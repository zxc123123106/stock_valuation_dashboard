from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Generator
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, create_engine, delete, func, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import PROJECT_ROOT, get_settings
from .brokers import DEFAULT_BROKER_ID
from .valuation import difference_percent, estimate_price, valuation_status


settings = get_settings()
SUPPORTED_EPS_TYPES = ("TTM", "LAST_YEAR")
LEGACY_DEMO_SOURCES = {"wantgoo-demo"}
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
CRAWLER_LOG_CLEANUP_KEY = "crawler_logs_last_cleanup_at"


def _resolve_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername != "sqlite":
        raise ValueError("Local MVP only supports SQLite DATABASE_URL values.")

    if not url.database or url.database == ":memory:":
        return database_url

    database_path = Path(url.database)
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    return f"sqlite:///{database_path}"


def _ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if not url.database or url.database == ":memory:":
        return

    Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


DATABASE_URL = _resolve_database_url(settings.database_url)
_ensure_sqlite_parent(DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


class Stock(Base):
    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    asset_type: Mapped[str] = mapped_column(String(16), default="STOCK")
    market: Mapped[str] = mapped_column(String(24), default="TWSE")
    currency: Mapped[str] = mapped_column(String(8), default="TWD")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    metrics: Mapped[list["StockMetric"]] = relationship(back_populates="stock")
    eps_rows: Mapped[list["StockEPS"]] = relationship(back_populates="stock")
    valuations: Mapped[list["StockValuation"]] = relationship(back_populates="stock")
    position: Mapped["StockPosition | None"] = relationship(back_populates="stock", uselist=False)
    broker_trading: Mapped["StockBrokerTrading | None"] = relationship(back_populates="stock", uselist=False)
    daily_prices: Mapped[list["StockDailyPrice"]] = relationship(back_populates="stock")
    pe_history: Mapped[list["StockPEHistory"]] = relationship(back_populates="stock")
    monthly_revenues: Mapped[list["StockMonthlyRevenue"]] = relationship(back_populates="stock")
    financial_quarters: Mapped[list["StockFinancialQuarter"]] = relationship(back_populates="stock")


class StockMetric(Base):
    __tablename__ = "stock_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    open_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    previous_close: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    day_high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    day_low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    current_pe: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    pe_average_3y: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    pe_min_3y: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    pe_max_3y: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    pe_vs_average_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    price_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pe_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="metrics")


class StockEPS(Base):
    __tablename__ = "stock_eps"
    __table_args__ = (UniqueConstraint("stock_id", "eps_type", name="uq_stock_eps_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    eps_type: Mapped[str] = mapped_column(String(24))
    eps_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    eps_period: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(120))
    eps_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="eps_rows")


class StockValuation(Base):
    __tablename__ = "stock_valuations"
    __table_args__ = (UniqueConstraint("stock_id", "eps_type", name="uq_stock_valuation_eps_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    eps_type: Mapped[str] = mapped_column(String(24))
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    current_pe: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    eps_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    estimated_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    price_difference: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    difference_percent: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    valuation_status: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(120))
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="valuations")


class StockPosition(Base):
    __tablename__ = "stock_positions"
    __table_args__ = (UniqueConstraint("stock_id", name="uq_stock_position_stock_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    buy_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="position")


class StockBrokerTrading(Base):
    __tablename__ = "stock_broker_trading"
    __table_args__ = (UniqueConstraint("stock_id", name="uq_stock_broker_trading_stock_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    trade_date: Mapped[str] = mapped_column(String(20))
    main_net_volume: Mapped[int] = mapped_column(Integer)
    main_buy_volume: Mapped[int] = mapped_column(Integer)
    main_sell_volume: Mapped[int] = mapped_column(Integer)
    volume_ratio_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="broker_trading")
    rows: Mapped[list["StockBrokerTradingRow"]] = relationship(back_populates="broker_trading")


class StockBrokerTradingRow(Base):
    __tablename__ = "stock_broker_trading_rows"
    __table_args__ = (UniqueConstraint("broker_trading_id", "side", "rank", name="uq_broker_trading_row_rank"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_trading_id: Mapped[int] = mapped_column(ForeignKey("stock_broker_trading.id"), index=True)
    side: Mapped[str] = mapped_column(String(8))
    rank: Mapped[int] = mapped_column(Integer)
    broker_name: Mapped[str] = mapped_column(String(80))
    buy_volume: Mapped[int] = mapped_column(Integer)
    sell_volume: Mapped[int] = mapped_column(Integer)
    net_volume: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    broker_trading: Mapped[StockBrokerTrading] = relationship(back_populates="rows")


class StockDailyPrice(Base):
    __tablename__ = "stock_daily_prices"
    __table_args__ = (UniqueConstraint("stock_id", "trade_date", name="uq_stock_daily_price_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    high_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    low_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    close_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="daily_prices")


class StockPEHistory(Base):
    __tablename__ = "stock_pe_history"
    __table_args__ = (UniqueConstraint("stock_id", "trade_date", name="uq_stock_pe_history_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    per: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    dividend_yield: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="pe_history")


class StockMonthlyRevenue(Base):
    __tablename__ = "stock_monthly_revenues"
    __table_args__ = (UniqueConstraint("stock_id", "month_date", name="uq_stock_monthly_revenue_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    month_date: Mapped[date] = mapped_column(Date, index=True)
    revenue: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    mom_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    yoy_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="monthly_revenues")


class StockFinancialQuarter(Base):
    __tablename__ = "stock_financial_quarters"
    __table_args__ = (UniqueConstraint("stock_id", "quarter_date", name="uq_stock_financial_quarter_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    quarter_date: Mapped[date] = mapped_column(Date, index=True)
    eps: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    gross_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    operating_income: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="financial_quarters")


class CrawlerLog(Base):
    __tablename__ = "crawler_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class StockRefreshState(Base):
    __tablename__ = "stock_refresh_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="idle")
    message: Mapped[str] = mapped_column(String(500), default="")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class AppMaintenanceState(Base):
    __tablename__ = "app_maintenance_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_database() -> None:
    Base.metadata.create_all(engine)
    ensure_stock_display_order_column()
    ensure_stock_asset_type_column()
    ensure_stock_metric_quote_columns()
    ensure_analysis_cache_columns()
    with SessionLocal() as session:
        backfill_display_order(session)
        remove_unsupported_eps(session)
        remove_legacy_histock_eps(session)
        seed_demo_data(session)
        seed_app_settings(session)
    run_startup_maintenance()


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


def apply_stock_snapshot(session: Session, snapshot) -> Stock:
    now = datetime.now(UTC)
    stock = session.scalar(select(Stock).where(Stock.symbol == snapshot.symbol))
    if not stock:
        stock = Stock(
            symbol=snapshot.symbol,
            name=snapshot.name,
            asset_type="STOCK",
            market=snapshot.market,
            currency=snapshot.currency,
            display_order=next_display_order(session),
        )
        session.add(stock)
        session.flush()

    if not stock.is_active:
        stock.display_order = next_display_order(session)

    stock.name = snapshot.name
    stock.asset_type = "STOCK"
    stock.market = snapshot.market
    stock.currency = snapshot.currency
    stock.is_active = True
    stock.updated_at = now

    session.add(
        StockMetric(
            stock_id=stock.id,
            open_price=snapshot.open_price,
            previous_close=snapshot.previous_close,
            day_high=snapshot.day_high,
            day_low=snapshot.day_low,
            current_price=snapshot.current_price,
            change_percent=snapshot.change_percent,
            current_pe=snapshot.current_pe,
            price_updated_at=snapshot.price_updated_at,
            pe_updated_at=snapshot.price_updated_at,
            source=snapshot.source,
        )
    )

    session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
    session.execute(delete(StockValuation).where(StockValuation.stock_id == stock.id))
    session.flush()

    for eps_snapshot in snapshot.eps_rows:
        if eps_snapshot.eps_type not in SUPPORTED_EPS_TYPES:
            continue

        eps_row = StockEPS(
            stock_id=stock.id,
            eps_type=eps_snapshot.eps_type,
            eps_value=eps_snapshot.eps_value,
            eps_period=eps_snapshot.eps_period,
            source=snapshot.source,
            eps_updated_at=snapshot.fetched_at,
        )
        session.add(eps_row)

        estimated = estimate_price(eps_snapshot.eps_value, snapshot.current_pe)
        price_difference = estimated - snapshot.current_price
        percent = difference_percent(snapshot.current_price, estimated)
        session.add(
            StockValuation(
                stock_id=stock.id,
                eps_type=eps_snapshot.eps_type,
                current_price=snapshot.current_price,
                current_pe=snapshot.current_pe,
                eps_value=eps_snapshot.eps_value,
                estimated_price=estimated,
                price_difference=price_difference,
                difference_percent=percent,
                valuation_status=valuation_status(percent),
                source=snapshot.source,
                calculated_at=snapshot.fetched_at,
            )
        )

    return stock


def apply_layered_stock_refresh(
    session: Session,
    *,
    profile,
    quote,
    current_pe: Decimal | None,
    pe_updated_at: datetime | None,
    eps_rows: list | None,
    eps_updated_at: datetime | None,
    source: str,
    calculated_at: datetime,
) -> Stock:
    now = datetime.now(UTC)
    stock = session.scalar(select(Stock).where(Stock.symbol == profile.symbol))
    if not stock:
        stock = Stock(
            symbol=profile.symbol,
            name=profile.name,
            asset_type=profile.asset_type,
            market=profile.market,
            currency=profile.currency,
            display_order=next_display_order(session),
        )
        session.add(stock)
        session.flush()

    if not stock.is_active:
        stock.display_order = next_display_order(session)

    stock.name = profile.name
    stock.asset_type = profile.asset_type
    stock.market = profile.market
    stock.currency = profile.currency
    stock.is_active = True
    stock.updated_at = now

    latest_metric = session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock.id)
        .order_by(StockMetric.created_at.desc())
        .limit(1)
    )
    metric_pe = current_pe
    metric_pe_updated_at = pe_updated_at
    if metric_pe is None and latest_metric:
        metric_pe = latest_metric.current_pe
        metric_pe_updated_at = latest_metric.pe_updated_at
    if metric_pe is None:
        metric_pe = Decimal("0.00")
    if metric_pe_updated_at is None:
        metric_pe_updated_at = quote.price_updated_at

    session.add(
        StockMetric(
            stock_id=stock.id,
            open_price=quote.open_price,
            previous_close=quote.previous_close,
            day_high=quote.day_high,
            day_low=quote.day_low,
            current_price=quote.current_price,
            change_percent=quote.change_percent,
            current_pe=metric_pe,
            pe_average_3y=latest_metric.pe_average_3y if latest_metric else None,
            pe_min_3y=latest_metric.pe_min_3y if latest_metric else None,
            pe_max_3y=latest_metric.pe_max_3y if latest_metric else None,
            pe_vs_average_percent=_pe_vs_average_percent(metric_pe, latest_metric.pe_average_3y if latest_metric else None),
            price_updated_at=quote.price_updated_at,
            pe_updated_at=metric_pe_updated_at,
            source=source,
        )
    )

    if stock.asset_type == "ETF":
        session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
        valuation_inputs = []
    elif eps_rows is not None:
        session.execute(delete(StockEPS).where(StockEPS.stock_id == stock.id))
        session.flush()
        valuation_inputs = []
        for eps_snapshot in eps_rows:
            if eps_snapshot.eps_type not in SUPPORTED_EPS_TYPES:
                continue

            session.add(
                StockEPS(
                    stock_id=stock.id,
                    eps_type=eps_snapshot.eps_type,
                    eps_value=eps_snapshot.eps_value,
                    eps_period=eps_snapshot.eps_period,
                    source=source,
                    eps_updated_at=eps_updated_at or calculated_at,
                )
            )
            valuation_inputs.append(
                (eps_snapshot.eps_type, eps_snapshot.eps_value, eps_snapshot.eps_period)
            )
    else:
        valuation_inputs = [
            (eps_row.eps_type, eps_row.eps_value, eps_row.eps_period)
            for eps_row in session.scalars(
                select(StockEPS)
                .where(StockEPS.stock_id == stock.id)
                .order_by(StockEPS.eps_type.desc())
            ).all()
            if eps_row.eps_type in SUPPORTED_EPS_TYPES
        ]

    session.execute(delete(StockValuation).where(StockValuation.stock_id == stock.id))
    session.flush()

    for eps_type, eps_value, _ in valuation_inputs:
        estimated = estimate_price(eps_value, metric_pe)
        price_difference = estimated - quote.current_price
        percent = difference_percent(quote.current_price, estimated)
        session.add(
            StockValuation(
                stock_id=stock.id,
                eps_type=eps_type,
                current_price=quote.current_price,
                current_pe=metric_pe,
                eps_value=eps_value,
                estimated_price=estimated,
                price_difference=price_difference,
                difference_percent=percent,
                valuation_status=valuation_status(percent),
                source=source,
                calculated_at=calculated_at,
            )
        )

    return stock


def apply_broker_trading_snapshot(session: Session, stock: Stock, snapshot) -> None:
    now = datetime.now(UTC)
    broker_trading = session.scalar(
        select(StockBrokerTrading).where(StockBrokerTrading.stock_id == stock.id)
    )
    if not broker_trading:
        broker_trading = StockBrokerTrading(
            stock_id=stock.id,
            trade_date=snapshot.trade_date,
            main_net_volume=snapshot.main_net_volume,
            main_buy_volume=snapshot.main_buy_volume,
            main_sell_volume=snapshot.main_sell_volume,
            volume_ratio_percent=snapshot.volume_ratio_percent,
            source=snapshot.source,
            fetched_at=snapshot.fetched_at,
        )
        session.add(broker_trading)
        session.flush()
    else:
        broker_trading.trade_date = snapshot.trade_date
        broker_trading.main_net_volume = snapshot.main_net_volume
        broker_trading.main_buy_volume = snapshot.main_buy_volume
        broker_trading.main_sell_volume = snapshot.main_sell_volume
        broker_trading.volume_ratio_percent = snapshot.volume_ratio_percent
        broker_trading.source = snapshot.source
        broker_trading.fetched_at = snapshot.fetched_at
        broker_trading.updated_at = now

    session.execute(
        delete(StockBrokerTradingRow).where(StockBrokerTradingRow.broker_trading_id == broker_trading.id)
    )
    session.flush()

    for side, rows in (("BUY", snapshot.buy_brokers), ("SELL", snapshot.sell_brokers)):
        for row in rows[:5]:
            session.add(
                StockBrokerTradingRow(
                    broker_trading_id=broker_trading.id,
                    side=side,
                    rank=row.rank,
                    broker_name=row.broker_name,
                    buy_volume=row.buy_volume,
                    sell_volume=row.sell_volume,
                    net_volume=row.net_volume,
                )
            )


def apply_daily_price_snapshots(session: Session, stock: Stock, snapshots: list) -> None:
    if not snapshots:
        return

    existing_rows = {
        row.trade_date: row
        for row in session.scalars(
            select(StockDailyPrice).where(
                StockDailyPrice.stock_id == stock.id,
                StockDailyPrice.trade_date >= snapshots[0].trade_date,
            )
        ).all()
    }
    now = datetime.now(UTC)
    for snapshot in snapshots:
        row = existing_rows.get(snapshot.trade_date)
        if not row:
            row = StockDailyPrice(stock_id=stock.id, trade_date=snapshot.trade_date)
            session.add(row)
        row.open_price = snapshot.open_price
        row.high_price = snapshot.high_price
        row.low_price = snapshot.low_price
        row.close_price = snapshot.close_price
        row.volume = snapshot.volume
        row.source = snapshot.source
        row.fetched_at = snapshot.fetched_at
        row.updated_at = now


def apply_pe_history_snapshots(session: Session, stock: Stock, snapshots: list) -> None:
    if not snapshots:
        return

    existing_rows = {
        row.trade_date: row
        for row in session.scalars(
            select(StockPEHistory).where(
                StockPEHistory.stock_id == stock.id,
                StockPEHistory.trade_date >= snapshots[0].trade_date,
            )
        ).all()
    }
    now = datetime.now(UTC)
    for snapshot in snapshots:
        row = existing_rows.get(snapshot.trade_date)
        if not row:
            row = StockPEHistory(stock_id=stock.id, trade_date=snapshot.trade_date)
            session.add(row)
        row.per = snapshot.per
        row.pbr = snapshot.pbr
        row.dividend_yield = snapshot.dividend_yield
        row.source = snapshot.source
        row.fetched_at = snapshot.fetched_at
        row.updated_at = now

    _update_latest_metric_pe_summary(session, stock)


def apply_monthly_revenue_snapshots(session: Session, stock: Stock, snapshots: list) -> None:
    if not snapshots:
        return

    existing_rows = {
        row.month_date: row
        for row in session.scalars(
            select(StockMonthlyRevenue).where(
                StockMonthlyRevenue.stock_id == stock.id,
                StockMonthlyRevenue.month_date >= snapshots[0].month_date,
            )
        ).all()
    }
    now = datetime.now(UTC)
    for snapshot in snapshots:
        row = existing_rows.get(snapshot.month_date)
        if not row:
            row = StockMonthlyRevenue(stock_id=stock.id, month_date=snapshot.month_date)
            session.add(row)
        row.revenue = snapshot.revenue
        row.mom_percent = snapshot.mom_percent
        row.yoy_percent = snapshot.yoy_percent
        row.source = snapshot.source
        row.fetched_at = snapshot.fetched_at
        row.updated_at = now


def apply_financial_quarter_snapshots(session: Session, stock: Stock, snapshots: list) -> None:
    if not snapshots:
        return

    existing_rows = {
        row.quarter_date: row
        for row in session.scalars(
            select(StockFinancialQuarter).where(
                StockFinancialQuarter.stock_id == stock.id,
                StockFinancialQuarter.quarter_date >= snapshots[0].quarter_date,
            )
        ).all()
    }
    now = datetime.now(UTC)
    for snapshot in snapshots:
        row = existing_rows.get(snapshot.quarter_date)
        if not row:
            row = StockFinancialQuarter(stock_id=stock.id, quarter_date=snapshot.quarter_date)
            session.add(row)
        row.eps = snapshot.eps
        row.revenue = snapshot.revenue
        row.gross_profit = snapshot.gross_profit
        row.operating_income = snapshot.operating_income
        row.net_income = snapshot.net_income
        row.source = snapshot.source
        row.fetched_at = snapshot.fetched_at
        row.updated_at = now


def _update_latest_metric_pe_summary(session: Session, stock: Stock) -> None:
    latest_metric = session.scalar(
        select(StockMetric)
        .where(StockMetric.stock_id == stock.id)
        .order_by(StockMetric.created_at.desc())
        .limit(1)
    )
    if not latest_metric:
        return

    pe_values = [
        value
        for value in session.scalars(
            select(StockPEHistory.per)
            .where(StockPEHistory.stock_id == stock.id, StockPEHistory.per.is_not(None))
            .order_by(StockPEHistory.trade_date.desc())
            .limit(365 * 3)
        ).all()
        if value is not None and value > 0
    ]
    if not pe_values:
        return

    pe_average = (sum(pe_values, Decimal("0.00")) / Decimal(len(pe_values))).quantize(Decimal("0.01"))
    latest_metric.pe_average_3y = pe_average
    latest_metric.pe_min_3y = min(pe_values)
    latest_metric.pe_max_3y = max(pe_values)
    latest_metric.pe_vs_average_percent = _pe_vs_average_percent(latest_metric.current_pe, pe_average)


def _pe_vs_average_percent(current_pe: Decimal | None, average_pe: Decimal | None) -> Decimal | None:
    if current_pe is None or average_pe is None or average_pe == 0:
        return None
    return ((current_pe - average_pe) / average_pe * Decimal("100")).quantize(Decimal("0.01"))


def next_display_order(session: Session) -> int:
    current_max = session.scalar(
        select(func.max(Stock.display_order)).where(Stock.is_active.is_(True))
    ) or 0
    return int(current_max) + 10


def log_crawler_result(
    session: Session,
    job_name: str,
    status: str,
    message: str,
    started_at: datetime,
    finished_at: datetime | None = None,
) -> None:
    session.add(
        CrawlerLog(
            job_name=job_name,
            status=status,
            message=message[:500],
            started_at=started_at,
            finished_at=finished_at or datetime.now(UTC),
        )
    )


def run_startup_maintenance() -> None:
    reset_interrupted_refresh_states()
    cleanup_crawler_logs_if_due()


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
        if states:
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
