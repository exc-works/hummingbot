from decimal import Decimal

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0004"),
    taker_percent_fee_decimal=Decimal("0.0007"),
    buy_percent_fee_deducted_from_returns=True,
)

CENTRALIZED = False
EXAMPLE_PAIR = "ETH-USDC"

OTHER_DOMAINS = ["caishen_spot_testnet", "caishen_spot_testnet_2", "caishen_spot_testnet_3"]
OTHER_DOMAINS_PARAMETER = {
    "caishen_spot_testnet": "caishen_spot_testnet",
    "caishen_spot_testnet_2": "caishen_spot_testnet",   # 共用 testnet API 端点，独立密钥
    "caishen_spot_testnet_3": "caishen_spot_testnet",
}
OTHER_DOMAINS_EXAMPLE_PAIR = {
    "caishen_spot_testnet": "ETH-USDC",
    "caishen_spot_testnet_2": "ETH-USDC",
    "caishen_spot_testnet_3": "ETH-USDC",
}
OTHER_DOMAINS_DEFAULT_FEES = {
    "caishen_spot_testnet": DEFAULT_FEES,
    "caishen_spot_testnet_2": DEFAULT_FEES,
    "caishen_spot_testnet_3": DEFAULT_FEES,
}


class CaishenSpotConfigMap(BaseConnectorConfigMap):
    connector: str = "caishen_spot"

    caishen_spot_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your wallet private key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    caishen_spot_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your wallet address",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )


KEYS = CaishenSpotConfigMap.model_construct()


class CaishenSpotTestnetConfigMap(BaseConnectorConfigMap):
    connector: str = "caishen_spot_testnet"

    caishen_spot_testnet_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your testnet wallet private key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    caishen_spot_testnet_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your testnet wallet address",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    model_config = ConfigDict(title="caishen_spot")


class CaishenSpotTestnet2ConfigMap(BaseConnectorConfigMap):
    connector: str = "caishen_spot_testnet_2"

    caishen_spot_testnet_2_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your testnet wallet private key (account 2)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    caishen_spot_testnet_2_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your testnet wallet address (account 2)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    model_config = ConfigDict(title="caishen_spot")


class CaishenSpotTestnet3ConfigMap(BaseConnectorConfigMap):
    connector: str = "caishen_spot_testnet_3"

    caishen_spot_testnet_3_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your testnet wallet private key (account 3)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    caishen_spot_testnet_3_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your testnet wallet address (account 3)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    model_config = ConfigDict(title="caishen_spot")


OTHER_DOMAINS_KEYS = {
    "caishen_spot_testnet": CaishenSpotTestnetConfigMap.model_construct(),
    "caishen_spot_testnet_2": CaishenSpotTestnet2ConfigMap.model_construct(),
    "caishen_spot_testnet_3": CaishenSpotTestnet3ConfigMap.model_construct(),
}
