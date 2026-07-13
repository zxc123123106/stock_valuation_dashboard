from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base

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
    ai_analyses: Mapped[list["StockAIAnalysis"]] = relationship(back_populates="stock")
    data_quality_states: Mapped[list["StockDataQualityState"]] = relationship(back_populates="stock")


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
    pe_data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
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


class StockDataQualityState(Base):
    __tablename__ = "stock_data_quality_states"
    __table_args__ = (UniqueConstraint("stock_id", "category", name="uq_stock_data_quality_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_period: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sync_status: Mapped[str] = mapped_column(String(24), default="idle")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_summary: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_cached: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="data_quality_states")


class StockAIAnalysis(Base):
    __tablename__ = "stock_ai_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    provider: Mapped[str] = mapped_column(String(24), index=True)
    model: Mapped[str] = mapped_column(String(120))
    analysis_mode: Mapped[str] = mapped_column(String(16), default="GENERAL", index=True)
    prompt_version: Mapped[str] = mapped_column(String(40), default="v1")
    analysis_date: Mapped[date] = mapped_column(Date, index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    request_payload_json: Mapped[str] = mapped_column(Text)
    response_json: Mapped[str] = mapped_column(Text)
    raw_response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_flags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    grounding_errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="success")
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    stock: Mapped[Stock] = relationship(back_populates="ai_analyses")


class StockAIFeedback(Base):
    __tablename__ = "stock_ai_feedback"
    __table_args__ = (UniqueConstraint("analysis_id", name="uq_stock_ai_feedback_analysis"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("stock_ai_analyses.id"), index=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    analysis_mode: Mapped[str] = mapped_column(String(16), index=True)
    rating: Mapped[str] = mapped_column(String(24), index=True)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class FuturesSnapshot(Base):
    __tablename__ = "futures_snapshots"

    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    session_type: Mapped[str] = mapped_column(String(16), default="closed")
    session_label: Mapped[str] = mapped_column(String(24), default="最近一盤")
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    open_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    difference_points: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    difference_percent: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    price_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class FuturesIntradayPoint(Base):
    __tablename__ = "futures_intraday_points"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "session_type",
            "session_date",
            "point_time",
            name="uq_futures_intraday_point_minute",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    session_type: Mapped[str] = mapped_column(String(16), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    point_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    open_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    difference_percent: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    source: Mapped[str] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))



__all__ = [name for name in globals() if not name.startswith("__")]
