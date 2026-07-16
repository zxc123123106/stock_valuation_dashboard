from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class UserStockExport(BaseModel):
    symbol: str
    name: str
    asset_type: str = "STOCK"
    market: str = "TWSE"
    currency: str = "TWD"
    display_order: int
    buy_price: float | None = None

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized or len(normalized) > 24:
            raise ValueError("標的代號格式不正確。")
        return normalized

    @field_validator("asset_type")
    @classmethod
    def validate_asset_type(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"STOCK", "ETF"}:
            raise ValueError("asset_type 只接受 STOCK 或 ETF。")
        return normalized

    @field_validator("buy_price")
    @classmethod
    def validate_buy_price(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("成交均價必須大於 0。")
        return value


class UserDataDocument(BaseModel):
    schema_version: int = 1
    format: str = "stock-valuation-user-data"
    exported_at: datetime | None = None
    selected_broker: str = "CATHAY"
    stocks: list[UserStockExport] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("目前只支援 schema_version 1。")
        return value

    @field_validator("format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        if value != "stock-valuation-user-data":
            raise ValueError("這不是股票估值看板的使用者資料檔案。")
        return value


class UserDataImportPreviewRequest(BaseModel):
    document: UserDataDocument


class UserDataImportPreviewResponse(BaseModel):
    preview_hash: str
    current_revision: str
    normalized_document: UserDataDocument
    added_symbols: list[str]
    retained_symbols: list[str]
    removed_symbols: list[str]
    position_change_count: int
    broker_changed: bool
    warnings: list[str]


class UserDataImportRequest(BaseModel):
    document: UserDataDocument
    preview_hash: str
    expected_revision: str
    confirm_replace: bool


class UserDataImportResponse(BaseModel):
    status: str
    added_symbols: list[str]
    retained_symbols: list[str]
    removed_symbols: list[str]
    backup_filename: str


class DatabaseBackupResponse(BaseModel):
    filename: str
    reason: str
    created_at: datetime
    size_bytes: int
    sha256: str
    alembic_revision: str | None = None
    backup_for_date: str | None = None


class DataManagementStatusResponse(BaseModel):
    journal_mode: str
    busy_timeout_ms: int
    foreign_keys_enabled: bool
    integrity_status: str
    current_revision: str | None
    migration_head: str
    backup_retention_count: int
    backup_hour: int
    last_backup_at: datetime | None
    backup_count: int
    import_in_progress: bool = False

