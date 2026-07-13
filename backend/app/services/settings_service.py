from sqlalchemy.orm import Session

from ..brokers import BrokerConfig, broker_options, get_broker
from ..repositories import settings as settings_repository
from ..schema.settings import BrokerOptionResponse, BrokerSettingResponse


def _broker_option_response(broker: BrokerConfig) -> BrokerOptionResponse:
    return BrokerOptionResponse(
        broker_id=broker.broker_id,
        name=broker.name,
        buy_fee_rate=float(broker.buy_fee_rate),
        sell_fee_rate=float(broker.sell_fee_rate),
        source_url=broker.source_url,
    )


def _broker_setting_response(session: Session) -> BrokerSettingResponse:
    broker_id = settings_repository.get_value(session, "selected_broker", "CATHAY") or "CATHAY"
    try:
        selected = get_broker(broker_id)
    except ValueError:
        selected = get_broker("CATHAY")
    return BrokerSettingResponse(
        selected_broker=selected.broker_id,
        selected=_broker_option_response(selected),
        brokers=[_broker_option_response(broker) for broker in broker_options()],
    )


def get_broker_setting(session: Session):
    return _broker_setting_response(session)


def update_broker_setting(session: Session, broker_id: str):
    broker = get_broker(broker_id)
    settings_repository.set_value(session, "selected_broker", broker.broker_id)
    return _broker_setting_response(session)
