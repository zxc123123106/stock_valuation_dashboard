"""Refresh-manager construction for the application lifespan."""

from ..config import Settings, get_settings
from ..refresh.manager import BackgroundRefreshManager


def create_refresh_manager(settings: Settings | None = None) -> BackgroundRefreshManager:
    config = settings or get_settings()
    return BackgroundRefreshManager(
        interval_seconds=config.background_refresh_seconds,
        finmind_token=config.finmind_token,
        quote_market_interval_seconds=config.quote_market_interval_seconds,
        quote_off_hours_interval_seconds=config.quote_off_hours_interval_seconds,
        pe_poll_interval_seconds=config.pe_poll_interval_seconds,
        monthly_revenue_release_interval_seconds=config.monthly_revenue_release_interval_seconds,
        futures_refresh_seconds=config.futures_refresh_seconds,
    )
