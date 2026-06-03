import asyncio
import time
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_constants as CONSTANTS
import hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_web_utils as web_utils
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.funding_info import FundingInfo, FundingInfoUpdate
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.perpetual_api_order_book_data_source import PerpetualAPIOrderBookDataSource
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_derivative import (
        CaishenPerpetualDerivative,
    )


class CaishenPerpetualAPIOrderBookDataSource(PerpetualAPIOrderBookDataSource):
    _logger: Optional[HummingbotLogger] = None
    _request_id: int = 1

    def __init__(
        self,
        trading_pairs: List[str],
        connector: "CaishenPerpetualDerivative",
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

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        exchange_symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        ticker_response = await self._connector._api_get(
            path_url=CONSTANTS.TICKER_PRICE_CHANGE_URL,
            params={
                "symbol": exchange_symbol,
                "trading_domain": CONSTANTS.TRADING_DOMAIN_PERP,
            },
        )

        funding_response: Dict = await self._request_complete_funding_info(trading_pair)

        ticker_data = ticker_response.get("data", {})
        index_price = Decimal(str(ticker_data.get("index_price", "0")))
        mark_price = Decimal(str(ticker_data.get("mark_price", "0")))

        funding_rate = Decimal("0")
        if funding_response.get("code") == 0:
            data = funding_response.get("data", {})
            real_time_funding_rates = data.get("real_time_funding_rates", [])
            for rate_info in real_time_funding_rates:
                if rate_info.get("symbol") == exchange_symbol:
                    funding_rate = Decimal(str(rate_info.get("funding_rate", "0")))
                    break

        next_funding_utc_timestamp = self._next_funding_time(trading_pair)

        return FundingInfo(
            trading_pair=trading_pair,
            index_price=index_price,
            mark_price=mark_price,
            next_funding_utc_timestamp=next_funding_utc_timestamp,
            rate=funding_rate,
        )

    async def listen_for_funding_info(self, output: asyncio.Queue):
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    funding_info = await self.get_funding_info(trading_pair)
                    funding_info_update = FundingInfoUpdate(
                        trading_pair=trading_pair,
                        index_price=funding_info.index_price,
                        mark_price=funding_info.mark_price,
                        next_funding_utc_timestamp=funding_info.next_funding_utc_timestamp,
                        rate=funding_info.rate,
                    )
                    output.put_nowait(funding_info_update)
                await self._sleep(CONSTANTS.FUNDING_RATE_UPDATE_INTERNAL_SECOND)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Unexpected error when processing public funding info updates from exchange")
                await self._sleep(CONSTANTS.FUNDING_RATE_UPDATE_INTERNAL_SECOND)

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        exchange_symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        params = {
            "symbol": exchange_symbol,
            "aggregation_level": "1x",
            "limit": 200,
            "trading_domain": CONSTANTS.TRADING_DOMAIN_PERP,
        }
        return await self._connector._api_get(
            path_url=CONSTANTS.SNAPSHOT_REST_URL,
            params=params,
        )

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot_response: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)

        response_code = snapshot_response.get("code", -1)
        if response_code != 0:
            msg = f"Failed to fetch order book snapshot: code={response_code}, msg={snapshot_response.get('msg', '')}"
            self.logger().error(msg)
            raise IOError(msg)

        data = snapshot_response.get("data", {})
        if not data:
            raise IOError("Order book snapshot response missing 'data' field")

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
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
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
                "trading_domain": CONSTANTS.TRADING_DOMAIN_PERP,
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
                "trading_domain": CONSTANTS.TRADING_DOMAIN_PERP,
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

    async def _parse_funding_info_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        pass

    async def _request_complete_funding_info(self, trading_pair: str):
        exchange_symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        return await self._connector._api_get(
            path_url=CONSTANTS.FUNDING_URL,
            params={"symbol": exchange_symbol},
        )

    def _next_funding_time(self, trading_pair: str) -> int:
        funding_interval_hours = self._connector._funding_interval_hours.get(trading_pair, 8)
        current_time = time.time()
        interval_seconds = funding_interval_hours * 3600
        return int(((current_time // interval_seconds) + 1) * interval_seconds)
