import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import hummingbot.connector.exchange.caishen_spot.caishen_spot_constants as CONSTANTS
import hummingbot.connector.exchange.caishen_spot.caishen_spot_web_utils as web_utils
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.caishen_spot.caishen_spot_exchange import CaishenSpotExchange


class CaishenSpotAPIOrderBookDataSource(OrderBookTrackerDataSource):
    _logger: Optional[HummingbotLogger] = None
    _request_id: int = 1

    def __init__(
        self,
        trading_pairs: List[str],
        connector: "CaishenSpotExchange",
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DOMAIN,
    ):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain

    async def get_last_traded_prices(
        self, trading_pairs: List[str], domain: Optional[str] = None
    ) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    def _next_request_id(self) -> int:
        request_id = self._request_id
        self._request_id += 1
        return request_id

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        exchange_symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        params = {
            "symbol": exchange_symbol,
            "aggregation_level": "1x",
            "limit": 200,
            "trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT,
        }
        rest_assistant = await self._api_factory.get_rest_assistant()
        return await rest_assistant.execute_request(
            url=web_utils.rest_url(CONSTANTS.SNAPSHOT_REST_URL, self._domain),
            params=params,
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.ALL_ENDPOINTS_LIMIT,
        )

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot_response = await self._request_order_book_snapshot(trading_pair)
        if snapshot_response.get("code", -1) != 0:
            raise IOError(
                f"Failed to fetch order book snapshot: {snapshot_response.get('msg', snapshot_response)}"
            )

        data = snapshot_response.get("data", {})
        timestamp_str = data.get("timestamp", "")
        if timestamp_str:
            timestamp_str_clean = str(timestamp_str).rstrip("Z")
            timestamp = int(datetime.fromisoformat(timestamp_str_clean).timestamp())
        else:
            ts_ms = data.get("ts")
            timestamp = int(ts_ms / 1000) if ts_ms else int(time.time())

        bids = web_utils.parse_l2book_levels(data.get("bids", []))
        asks = web_utils.parse_l2book_levels(data.get("asks", []))
        sequence = data.get("sequence", timestamp)
        update_id = int(sequence) if str(sequence).isdigit() else timestamp

        return OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": bids,
                "asks": asks,
            },
            timestamp=timestamp,
        )

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=web_utils.wss_url(self._domain), ping_timeout=CONSTANTS.HEARTBEAT_TIME_INTERVAL)
        return ws

    async def _send_l2book_subscribe(self, ws: WSAssistant, trading_pair: str):
        symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        payload = {
            "jsonrpc": "2.0",
            "method": "l2book_subscribe",
            "params": {
                "symbol": symbol,
                "aggregation_level": "1x",
                "trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT,
            },
            "id": self._next_request_id(),
        }
        await ws.send(WSJSONRequest(payload=payload))
        self.logger().info(f"Subscribed to l2book for {trading_pair} ({symbol})")

    async def _send_l2book_unsubscribe(self, ws: WSAssistant, trading_pair: str):
        symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        payload = {
            "jsonrpc": "2.0",
            "method": "l2book_unsubscribe",
            "params": {
                "symbol": symbol,
                "aggregation_level": "1x",
                "trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT,
            },
            "id": self._next_request_id(),
        }
        await ws.send(WSJSONRequest(payload=payload))
        self.logger().info(f"Unsubscribed from l2book for {trading_pair} ({symbol})")

    async def _subscribe_channels(self, ws: WSAssistant):
        for trading_pair in self._trading_pairs:
            await self._send_l2book_subscribe(ws, trading_pair)

    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            self.logger().warning(f"Cannot subscribe to {trading_pair}: WebSocket not connected")
            return False

        try:
            await self._send_l2book_subscribe(self._ws_assistant, trading_pair)
            self.add_trading_pair(trading_pair)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Unexpected error subscribing to {trading_pair} order book")
            return False

    async def unsubscribe_from_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            self.logger().warning(f"Cannot unsubscribe from {trading_pair}: WebSocket not connected")
            return False

        try:
            await self._send_l2book_unsubscribe(self._ws_assistant, trading_pair)
            self.remove_trading_pair(trading_pair)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(f"Unexpected error unsubscribing from {trading_pair} order book")
            return False

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        method = event_message.get("method", "")
        if method == "l2book_notification":
            return self._diff_messages_queue_key
        if method == "symbol_trade_notification":
            return self._trade_messages_queue_key
        return ""

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        payload = web_utils.unwrap_ws_notification_payload(raw_message.get("params", raw_message))
        symbol = payload.get("symbol")
        if symbol is None:
            return
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
        for update in payload.get("updates", []):
            ts_ms = update.get("ts", int(time.time() * 1000))
            sequence_range = update.get("sequence_range", {})
            update_id = update.get("sequence", sequence_range.get("end", ts_ms))
            update_id = int(update_id) if str(update_id).isdigit() else int(ts_ms)
            bids = web_utils.parse_l2book_levels(update.get("bids", []))
            asks = web_utils.parse_l2book_levels(update.get("asks", []))
            if not bids and not asks:
                continue
            message_queue.put_nowait(
                OrderBookMessage(
                    OrderBookMessageType.DIFF,
                    {
                        "trading_pair": trading_pair,
                        "update_id": update_id,
                        "bids": bids,
                        "asks": asks,
                    },
                    timestamp=ts_ms / 1000.0,
                )
            )

    async def _parse_order_book_snapshot_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        await self._parse_order_book_diff_message(raw_message, message_queue)

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        payload = web_utils.unwrap_ws_notification_payload(raw_message.get("params", raw_message))
        symbol = payload.get("symbol")
        if symbol is None:
            return
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=symbol)
        for trade in payload.get("updates", []):
            side = trade.get("side", trade.get("is_buy"))
            trade_type = TradeType.BUY if side in (True, "buy", 0, "0") else TradeType.SELL
            ts_ms = trade.get("time", trade.get("ts", int(time.time() * 1000)))
            message_queue.put_nowait(
                OrderBookMessage(
                    OrderBookMessageType.TRADE,
                    {
                        "trading_pair": trading_pair,
                        "trade_type": float(trade_type.value),
                        "trade_id": str(trade.get("tx_hash", trade.get("id", ts_ms))),
                        "price": float(trade.get("price", 0)),
                        "amount": float(trade.get("size", trade.get("amount", 0))),
                    },
                    timestamp=ts_ms / 1000.0 if ts_ms else time.time(),
                )
            )
