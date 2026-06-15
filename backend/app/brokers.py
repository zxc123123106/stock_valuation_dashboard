from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


DEFAULT_BROKER_ID = "CATHAY"
STOCK_TRANSACTION_TAX_RATE = Decimal("0.003")
ETF_TRANSACTION_TAX_RATE = Decimal("0.001")


@dataclass(frozen=True)
class BrokerConfig:
    broker_id: str
    name: str
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    source_url: str


BROKERS = {
    "CATHAY": BrokerConfig(
        broker_id="CATHAY",
        name="國泰證券",
        buy_fee_rate=Decimal("0.000399"),
        sell_fee_rate=Decimal("0.000399"),
        source_url="https://www.cathaysec.com.tw/cathaysec/Products/TradeFee/TWS.aspx",
    ),
}


def get_broker(broker_id: str) -> BrokerConfig:
    normalized = broker_id.strip().upper()
    broker = BROKERS.get(normalized)
    if not broker:
        raise ValueError(f"Unsupported broker: {broker_id}")
    return broker


def broker_options() -> list[BrokerConfig]:
    return list(BROKERS.values())


def transaction_tax_rate(asset_type: str) -> Decimal:
    return ETF_TRANSACTION_TAX_RATE if asset_type.upper() == "ETF" else STOCK_TRANSACTION_TAX_RATE
