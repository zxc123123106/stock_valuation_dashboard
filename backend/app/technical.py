from __future__ import annotations

from decimal import Decimal


MOVING_AVERAGE_PERIODS = (5, 10, 20, 60, 120, 240)


def moving_averages(closes: list[Decimal], periods: tuple[int, ...] = MOVING_AVERAGE_PERIODS) -> dict[int, Decimal | None]:
    averages: dict[int, Decimal | None] = {}
    for period in periods:
        if len(closes) < period:
            averages[period] = None
            continue
        averages[period] = sum(closes[-period:], Decimal("0")) / Decimal(period)
    return averages
