from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def estimate_price(eps_value: Decimal, current_pe: Decimal) -> Decimal:
    return quantize_money(eps_value * current_pe)


def difference_percent(current_price: Decimal, estimated_price_value: Decimal) -> Decimal:
    if current_price == 0:
        return Decimal("0.00")

    percent = (estimated_price_value - current_price) / current_price * Decimal("100")
    return quantize_money(percent)


def valuation_status(percent: Decimal) -> str:
    if percent > Decimal("10"):
        return "UNDERVALUED"
    if percent >= Decimal("3"):
        return "SLIGHTLY_UNDERVALUED"
    if percent >= Decimal("-3"):
        return "FAIR"
    if percent > Decimal("-10"):
        return "SLIGHTLY_OVERVALUED"
    return "OVERVALUED"
