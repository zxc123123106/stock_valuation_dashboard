"""Independent external fetchers used by background refresh consumers."""

from .broker import fetch_broker_channel
from .fundamentals import fetch_fundamentals_channel
from .history import fetch_history_channel
from .quote import fetch_quote_channel
from ..models import CHANNEL_BROKER, CHANNEL_FUNDAMENTALS, CHANNEL_HISTORY, CHANNEL_QUOTE, RefreshJob


def fetch_channel_payload(job: RefreshJob, finmind_token: str | None) -> dict:
    if job.channel == CHANNEL_QUOTE:
        return fetch_quote_channel(job, finmind_token)
    if job.channel == CHANNEL_FUNDAMENTALS:
        return fetch_fundamentals_channel(job, finmind_token)
    if job.channel == CHANNEL_BROKER:
        return fetch_broker_channel(job)
    if job.channel == CHANNEL_HISTORY:
        return fetch_history_channel(job, finmind_token)
    return {"results": {}, "errors": {job.channel: ValueError(f"Unknown refresh channel: {job.channel}")}}
