from pydantic import BaseModel


class BrokerOptionResponse(BaseModel):
    broker_id: str
    name: str
    buy_fee_rate: float
    sell_fee_rate: float
    source_url: str


class BrokerSettingResponse(BaseModel):
    selected_broker: str
    selected: BrokerOptionResponse
    brokers: list[BrokerOptionResponse]


class BrokerSettingRequest(BaseModel):
    broker_id: str
