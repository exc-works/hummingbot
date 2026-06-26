from decimal import Decimal
from typing import Any, Dict, Literal

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0008"),
    taker_percent_fee_decimal=Decimal("0.001"),
)

CENTRALIZED = True

EXAMPLE_PAIR = "BTC-USDT"


class OKXConfigMap(BaseConnectorConfigMap):
    connector: str = "okx"
    okx_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your OKX API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    okx_secret_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your OKX secret key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    okx_passphrase: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your OKX passphrase key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    okx_registration_sub_domain: Literal["www", "app", "my"] = Field(
        default="www",
        json_schema_extra={
            "prompt": "Which OKX subdomain did you register the key at? (www/app/my) - Generally www for most users, app for US users, my for EEA users.",
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )


KEYS = OKXConfigMap.model_construct()

OTHER_DOMAINS = ["okx_demo"]
OTHER_DOMAINS_PARAMETER = {"okx_demo": "okx_demo"}
OTHER_DOMAINS_EXAMPLE_PAIR = {"okx_demo": EXAMPLE_PAIR}
OTHER_DOMAINS_DEFAULT_FEES = {"okx_demo": DEFAULT_FEES}


class OKXDemoConfigMap(BaseConnectorConfigMap):
    connector: str = "okx_demo"
    okx_demo_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your OKX Demo Trading API key (create under Trade > Demo Trading > Personal Center > Demo Trading API)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    okx_demo_secret_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your OKX Demo Trading secret key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    okx_demo_passphrase: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your OKX Demo Trading passphrase",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    okx_demo_registration_sub_domain: Literal["www", "app", "my"] = Field(
        default="www",
        json_schema_extra={
            "prompt": "Which OKX subdomain did you register the demo key at? (www/app/my)",
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    model_config = ConfigDict(title="okx")


OTHER_DOMAINS_KEYS = {"okx_demo": OKXDemoConfigMap.model_construct()}


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    """
    Verifies if a trading pair is enabled to operate with based on its exchange information

    :param exchange_info: the exchange information for a trading pair

    :return: True if the trading pair is enabled, False otherwise
    """
    return (exchange_info.get("instType", None) == "SPOT" and exchange_info.get("baseCcy") != ""
            and exchange_info.get("quoteCcy") != "")
