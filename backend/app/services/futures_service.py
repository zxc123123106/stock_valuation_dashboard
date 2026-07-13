"""WTX cached response service."""

from ..taifex_futures import latest_wtx_response


def get_latest_wtx() -> dict:
    return latest_wtx_response()
