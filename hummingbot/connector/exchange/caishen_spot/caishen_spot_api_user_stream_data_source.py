import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import hummingbot.connector.exchange.caishen_spot.caishen_spot_constants as CONSTANTS
import hummingbot.connector.exchange.caishen_spot.caishen_spot_web_utils as web_utils
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.caishen_spot.caishen_spot_exchange import CaishenSpotExchange


class CaishenSpotAPIUserStreamDataSource(UserStreamTrackerDataSource):
    _logger: Optional[HummingbotLogger] = None
    HEARTBEAT_TIME_INTERVAL = CONSTANTS.HEARTBEAT_TIME_INTERVAL
    _request_id: int = 1

    def __init__(
        self,
        auth: AuthBase,
        trading_pairs: List[str],
        connector: "CaishenSpotExchange",
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DOMAIN,
    ):
        super().__init__()
        self._auth = auth
        self._trading_pairs = trading_pairs
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._ws_assistant: Optional[WSAssistant] = None

    @property
    def last_recv_time(self) -> float:
        if self._ws_assistant is not None:
            return self._ws_assistant.last_recv_time
        return 0

    def _next_request_id(self) -> int:
        request_id = self._request_id
        self._request_id += 1
        return request_id

    async def _connected_websocket_assistant(self) -> WSAssistant:
        self._ws_assistant = await self._api_factory.get_ws_assistant()
        await self._ws_assistant.connect(
            ws_url=web_utils.wss_url(self._domain),
            ping_timeout=self.HEARTBEAT_TIME_INTERVAL,
        )
        return self._ws_assistant

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        account = self._connector.api_key
        subscriptions = [
            ("order_changed_subscribe", {}),
            ("trade_subscribe", {}),
            ("balances_subscribe", {}),
        ]
        for method, extra in subscriptions:
            params = {
                "account": account,
                "trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT,
                **extra,
            }
            payload = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": self._next_request_id(),
            }
            await websocket_assistant.send(WSJSONRequest(payload=payload))
        self.logger().info("Subscribed to Caishen spot private user streams")

    async def _process_event_message(self, event_message: Dict[str, Any], queue: asyncio.Queue):
        if event_message.get("error") is not None:
            err_msg = event_message.get("error", {}).get("message", event_message.get("error"))
            raise IOError({"label": "WSS_ERROR", "message": f"Error received via websocket - {err_msg}."})

        method = event_message.get("method")
        if method in (
            "order_changed_notification",
            "trade_notification",
            "balances_notification",
        ):
            queue.put_nowait(event_message)

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant, queue: asyncio.Queue):
        while True:
            try:
                await super()._process_websocket_messages(
                    websocket_assistant=websocket_assistant,
                    queue=queue,
                )
            except asyncio.TimeoutError:
                continue
