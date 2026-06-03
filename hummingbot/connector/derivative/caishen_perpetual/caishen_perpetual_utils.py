# caishen_perpetual_utils.py

from decimal import Decimal

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

# === 手续费配置 ===
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0002"),  # 0.02%
    taker_percent_fee_decimal=Decimal("0.0005"),  # 0.05%
    buy_percent_fee_deducted_from_returns=True
)

CENTRALIZED = False  # DEX 设为 False
EXAMPLE_PAIR = "BTC-USDC"
BROKER_ID = ""


class CaishenPerpetualConfigMap(BaseConnectorConfigMap):
    """连接器配置映射类"""
    
    connector: str = "caishen_perpetual"
    
    caishen_perpetual_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your wallet private key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    
    caishen_perpetual_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your wallet address",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )


# 必须导出 KEYS
KEYS = CaishenPerpetualConfigMap.model_construct()

# === 测试网配置 ===
OTHER_DOMAINS = ["caishen_perpetual_testnet"]
OTHER_DOMAINS_PARAMETER = {"caishen_perpetual_testnet": "caishen_perpetual_testnet"}
OTHER_DOMAINS_EXAMPLE_PAIR = {"caishen_perpetual_testnet": "BTC-USDC"}
OTHER_DOMAINS_DEFAULT_FEES = {"caishen_perpetual_testnet": DEFAULT_FEES}


class CaishenPerpetualTestnetConfigMap(BaseConnectorConfigMap):
    """测试网配置"""
    connector: str = "caishen_perpetual_testnet"
    
    caishen_perpetual_testnet_api_secret: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your testnet wallet private key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    
    caishen_perpetual_testnet_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your testnet wallet address",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )

    model_config = ConfigDict(title="caishen_perpetual")



OTHER_DOMAINS_KEYS = {
    "caishen_perpetual_testnet": CaishenPerpetualTestnetConfigMap.model_construct()
}