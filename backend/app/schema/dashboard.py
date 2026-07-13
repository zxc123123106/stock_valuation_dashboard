from pydantic import BaseModel

from .refresh import RefreshStatusResponse
from .stock import StockResponse
from .system import MetadataResponse


class DashboardSnapshotResponse(BaseModel):
    revision: str
    stocks: list[StockResponse]
    metadata: MetadataResponse
    refresh_status: RefreshStatusResponse
