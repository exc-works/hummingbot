# caishen_perpetual_web_utils.py
from decimal import Decimal
from typing import Any, Dict, Optional,Tuple

import hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_constants as CONSTANTS
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class CaishenPerpetualRESTPreProcessor(RESTPreProcessorBase):
    
    async def pre_process(self, request: RESTRequest) -> RESTRequest:
        if request.headers is None:
            request.headers = {}
        request.headers["Content-Type"] = "application/json"
        return request


def public_rest_url(path_url: str, domain: str = CONSTANTS.DOMAIN) -> str:
    base_url = CONSTANTS.PERPETUAL_BASE_URL if domain == CONSTANTS.DOMAIN else CONSTANTS.TESTNET_BASE_URL
    return base_url + path_url


def private_rest_url(path_url: str, domain: str = CONSTANTS.DOMAIN) -> str:
    return public_rest_url(path_url, domain)


def wss_url(domain: str = CONSTANTS.DOMAIN) -> str:
    return CONSTANTS.PERPETUAL_WS_URL if domain == CONSTANTS.DOMAIN else CONSTANTS.TESTNET_WS_URL


def build_api_factory(
        throttler: Optional[AsyncThrottler] = None,
        auth: Optional[AuthBase] = None
) -> WebAssistantsFactory:
    throttler = throttler or create_throttler()
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        rest_pre_processors=[CaishenPerpetualRESTPreProcessor()],
        auth=auth
    )
    return api_factory


def create_throttler() -> AsyncThrottler:
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


async def get_current_server_time(throttler, domain) -> float:
    import time
    return time.time()


def is_exchange_information_valid(rule: Dict[str, Any]) -> bool:
    return True

def float_to_int_for_hashing(x: float) -> int:
    return float_to_int(x, 8)


def float_to_int(x: float, power: int) -> int:
    with_decimals = x * 10 ** power
    if abs(round(with_decimals) - with_decimals) >= 1e-3:
        raise ValueError("float_to_int causes rounding", x)
    return round(with_decimals)


def str_to_bytes16(x: str) -> bytearray:
    assert x.startswith("0x")
    return bytearray.fromhex(x[2:])


def float_to_wire(x: float) -> str:
    rounded = "{:.8f}".format(x)
    if abs(float(rounded) - x) >= 1e-12:
        raise ValueError("float_to_wire causes rounding", x)
    if rounded == "-0":
        rounded = "0"
    normalized = Decimal(rounded).normalize()
    return f"{normalized:f}"


def get_rest_api_limit_id_for_endpoint(endpoint: str, trading_pair: Optional[str] = None) -> str:
    """
    根据 endpoint 获取对应的 rate limit ID
    
    由于 caishen_perpetual 只有一个全局 rate limit，所有 endpoint 都返回 "All"
    如果将来需要为不同 endpoint 设置不同的 rate limit，可以在这里扩展
    
    Args:
        endpoint: API endpoint 路径，如 "/v1/symbols"
        trading_pair: 交易对（可选，目前未使用）
        
    Returns:
        rate limit ID，目前总是返回 "All"
    """
    # 目前所有 endpoint 都使用全局 rate limit
    return CONSTANTS.ALL_ENDPOINTS_LIMIT
