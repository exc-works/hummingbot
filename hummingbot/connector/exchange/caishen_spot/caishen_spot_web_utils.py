from typing import Optional

import hummingbot.connector.exchange.caishen_spot.caishen_spot_constants as CONSTANTS
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class CaishenSpotRESTPreProcessor(RESTPreProcessorBase):
    async def pre_process(self, request: RESTRequest) -> RESTRequest:
        if request.headers is None:
            request.headers = {}
        request.headers["Content-Type"] = "application/json"
        return request


def rest_url(path_url: str, domain: str = CONSTANTS.DOMAIN) -> str:
    base_url = CONSTANTS.MAINNET_REST_URL if domain == CONSTANTS.DOMAIN else CONSTANTS.TESTNET_REST_URL
    return base_url + path_url


def public_rest_url(path_url: str, domain: str = CONSTANTS.DOMAIN) -> str:
    return rest_url(path_url, domain)


def private_rest_url(path_url: str, domain: str = CONSTANTS.DOMAIN) -> str:
    return rest_url(path_url, domain)


def wss_url(domain: str = CONSTANTS.DOMAIN) -> str:
    return CONSTANTS.MAINNET_WS_URL if domain == CONSTANTS.DOMAIN else CONSTANTS.TESTNET_WS_URL


def build_api_factory(
    throttler: Optional[AsyncThrottler] = None,
    auth: Optional[AuthBase] = None,
) -> WebAssistantsFactory:
    throttler = throttler or create_throttler()
    return WebAssistantsFactory(
        throttler=throttler,
        rest_pre_processors=[CaishenSpotRESTPreProcessor()],
        auth=auth,
    )


def create_throttler() -> AsyncThrottler:
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


def is_exchange_information_valid(symbol_info: dict) -> bool:
    return (
        symbol_info.get("status") == 1
        and symbol_info.get("trading_domain") in (1, "1", None)
    )


def unwrap_ws_notification_payload(params: dict) -> dict:
    """
    Plato WS notifications wrap payloads as params.result (not params directly).
    """
    if not isinstance(params, dict):
        return {}
    result = params.get("result")
    if isinstance(result, dict):
        return result
    return params
