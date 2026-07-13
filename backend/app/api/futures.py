from fastapi import APIRouter

from ..schema.futures import FuturesWtxResponse
from ..services.futures_service import get_latest_wtx


router = APIRouter()


@router.get("/api/futures/wtx", response_model=FuturesWtxResponse)
def get_wtx_futures() -> FuturesWtxResponse:
    return FuturesWtxResponse(**get_latest_wtx())
