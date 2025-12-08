import asyncio
import time
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

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
    _bpobds_logger: Optional[HummingbotLogger] = None
    _trading_pair_symbol_map: Dict[str, Mapping[str, str]] = {}
    _mapping_initialization_lock = asyncio.Lock()

    def __init__(
            self,
            trading_pairs: List[str],
            connector: 'CaishenPerpetualDerivative',
            api_factory: WebAssistantsFactory,
            domain: str = CONSTANTS.DOMAIN
    ):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._trading_pairs: List[str] = trading_pairs
        self._message_queue: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pair=trading_pairs)

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        """
        获取指定交易对的资金费率信息
        
        Args:
            trading_pair: Hummingbot 格式的交易对，如 "ETH-USDC"
            
        Returns:
            FundingInfo 对象，包含 index_price, mark_price, rate, next_funding_utc_timestamp
            
        API 响应格式:
        ticker_response: {
            "code": 0,
            "data": {
                "index_price": "92779.3",
                "mark_price": "90759.2",
                ...
            }
        }
        
        funding_response: {
            "code": 0,
            "data": {
                "real_time_funding_rates": [
                    {
                        "symbol": "BTC-USDC",
                        "funding_rate": "-0.04"
                    }
                ]
            }
        }
        """
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        
        # 获取 ticker 数据（包含 index_price 和 mark_price）
        ticker_response = await self._api_get(
            path_url=CONSTANTS.TICKER_PRICE_CHANGE_URL,
            params={"symbol": exchange_symbol}
        )
        
        # 获取资金费率信息
        funding_response: Dict = await self._request_complete_funding_info(trading_pair)
        
        # 从 ticker_response 中提取价格信息
        ticker_data = ticker_response.get("data", {})
        index_price = Decimal(str(ticker_data.get("index_price", "0")))
        mark_price = Decimal(str(ticker_data.get("mark_price", "0")))
        
        # 从 funding_response 中提取资金费率
        # response 格式: {"code": 0, "data": {"real_time_funding_rates": [...]}}
        funding_rate = Decimal("0")
        
        if funding_response.get("code") == 0:
            data = funding_response.get("data", {})
            real_time_funding_rates = data.get("real_time_funding_rates", [])
            
            # 遍历找到匹配的交易对
            for rate_info in real_time_funding_rates:
                if rate_info.get("symbol") == exchange_symbol:
                    funding_rate = Decimal(str(rate_info.get("funding_rate", "0")))
                    break
        
        # 计算下次资金费率结算时间
        next_funding_utc_timestamp = self._next_funding_time(trading_pair)
        
        funding_info = FundingInfo(
            trading_pair=trading_pair,
            index_price=index_price,
            mark_price=mark_price,
            next_funding_utc_timestamp=next_funding_utc_timestamp,
            rate=funding_rate,
        )
        
        return funding_info

    async def listen_for_funding_info(self, output: asyncio.Queue):
        """
        Reads the funding info events queue and updates the local funding info information.
        """
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
        """
        获取订单簿快照
        
        API: GET /v1/l2book?symbol=ETH-USDC&aggregation_level=1x&limit=200
        
        返回格式:
        {
            "sequence": "string",
            "timestamp": "2025-12-04T08:16:26.891Z",
            "asks": [{"price": "string", "size": "string"}],
            "bids": [{"price": "string", "size": "string"}]
        }
        """
        exchange_symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        
        params = {
            "symbol": exchange_symbol,
            "aggregation_level": "1x",
            "limit": 200
        }

        data = await self._connector._api_get(
            path_url=CONSTANTS.SNAPSHOT_REST_URL,
            params=params
        )
        return data

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        """
        解析订单簿快照并创建 OrderBookMessage
        """
        snapshot_response: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        
        # 从 ISO 8601 时间戳转换为 Unix 时间戳（秒）
        # 格式: "2025-12-04T08:16:26.891Z"
        timestamp_str = snapshot_response.get('timestamp', '')
        if timestamp_str:
            from datetime import datetime
            # 处理 ISO 8601 格式，去掉 'Z' 后缀
            timestamp_str_clean = timestamp_str.rstrip('Z')
            dt = datetime.fromisoformat(timestamp_str_clean)
            timestamp = int(dt.timestamp())
        else:
            timestamp = int(time.time())
        
        # 解析 bids 和 asks
        # 新格式: [{"price": "string", "size": "string"}]
        bids = [[float(item['price']), float(item['size'])] for item in snapshot_response.get('bids', [])]
        asks = [[float(item['price']), float(item['size'])] for item in snapshot_response.get('asks', [])]
        
        # 使用 sequence 作为 update_id，如果没有则使用 timestamp
        sequence = snapshot_response.get('sequence', '')
        update_id = int(sequence) if sequence and sequence.isdigit() else timestamp
        
        snapshot_msg: OrderBookMessage = OrderBookMessage(
            OrderBookMessageType.SNAPSHOT, 
            {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": bids,
                "asks": asks,
            }, 
            timestamp=timestamp
        )
        return snapshot_msg

    async def _connected_websocket_assistant(self) -> WSAssistant:
        url = f"{web_utils.wss_url(self._domain)}"
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=url, ping_timeout=CONSTANTS.HEARTBEAT_TIME_INTERVAL)
        return ws

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the trade events and diff orders events through the provided websocket connection.

        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            for trading_pair in self._trading_pairs:
                symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                trades_payload = {
                    "method": "subscribe",
                    "subscription": {
                        "type": CONSTANTS.TRADES_ENDPOINT_NAME,
                        "coin": symbol,
                    }
                }
                subscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trades_payload)

                order_book_payload = {
                    "method": "subscribe",
                    "subscription": {
                        "type": CONSTANTS.DEPTH_ENDPOINT_NAME,
                        "coin": symbol,
                    }
                }
                subscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=order_book_payload)

                await ws.send(subscribe_trade_request)
                await ws.send(subscribe_orderbook_request)

                self.logger().info("Subscribed to public order book, trade channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error("Unexpected error occurred subscribing to order book data streams.")
            raise

    async def listen_for_subscriptions(self):
        """
        WebSocket 暂时未实现，跳过 WebSocket 订阅。
        所有订单簿更新都通过 REST API 快照完成（_request_order_book_snapshots）。
        保留原有的 _connected_websocket_assistant 和 _subscribe_channels 方法，待后续实现。
        """
        # WebSocket 未实现，此方法为空实现
        # 订单簿更新通过 REST API 快照完成，由 listen_for_order_book_snapshots 方法处理
        # 该方法会在超时后自动调用 _request_order_book_snapshots
        self.logger().info("WebSocket subscriptions disabled. Using REST API for order book updates.")
        while True:
            await self._sleep(3600.0)  # 保持任务运行但不做任何事

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        channel = ""
        if "result" not in event_message:
            stream_name = event_message.get("channel")
            if "l2Book" in stream_name:
                channel = self._snapshot_messages_queue_key
            elif "trades" in stream_name:
                channel = self._trade_messages_queue_key
        return channel

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        pass
        # timestamp: float = raw_message["data"]["time"] * 1e-3
        # trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
        #     raw_message["data"]["coin"] + '-' + CONSTANTS.CURRENCY)
        # data = raw_message["data"]
        # order_book_message: OrderBookMessage = OrderBookMessage(OrderBookMessageType.DIFF, {
        #     "trading_pair": trading_pair,
        #     "update_id": data["time"],
        #     "bids": [[float(i['px']), float(i['sz'])] for i in data["levels"][0]],
        #     "asks": [[float(i['px']), float(i['sz'])] for i in data["levels"][1]],
        # }, timestamp=timestamp)
        # message_queue.put_nowait(order_book_message)

    async def _parse_order_book_snapshot_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        pass
        # timestamp: float = raw_message["data"]["time"] * 1e-3
        # trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
        #     raw_message["data"]["coin"] + '-' + CONSTANTS.CURRENCY)
        # data = raw_message["data"]
        # order_book_message: OrderBookMessage = OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
        #     "trading_pair": trading_pair,
        #     "update_id": data["time"],
        #     "bids": [[float(i['px']), float(i['sz'])] for i in data["levels"][0]],
        #     "asks": [[float(i['px']), float(i['sz'])] for i in data["levels"][1]],
        # }, timestamp=timestamp)
        # message_queue.put_nowait(order_book_message)

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        pass
        # data = raw_message["data"]
        # for trade_data in data:
        #     trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
        #         trade_data["coin"] + '-' + CONSTANTS.CURRENCY)
        #     trade_message: OrderBookMessage = OrderBookMessage(OrderBookMessageType.TRADE, {
        #         "trading_pair": trading_pair,
        #         "trade_type": float(TradeType.SELL.value) if trade_data["side"] == "A" else float(
        #             TradeType.BUY.value),
        #         "trade_id": trade_data["hash"],
        #         "price": float(trade_data["px"]),
        #         "amount": float(trade_data["sz"])
        #     }, timestamp=trade_data["time"] * 1e-3)

        #     message_queue.put_nowait(trade_message)

    async def _parse_funding_info_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        pass

    async def _request_complete_funding_info(self, trading_pair: str):
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        funding_interval_hours = self._connector._funding_interval_hours.get(trading_pair, 8)
        data = await self._connector._api_get(path_url=CONSTANTS.EXCHANGE_INFO_URL,
                                               params={"symbol": exchange_symbol, "funding_interval_hours": funding_interval_hours})
        return data

    def _next_funding_time(self, trading_pair: str) -> int:
        """
        计算指定交易对的下次资金费率结算时间戳
        
        不同交易对的结算周期不同：
        - ETH-USDC: 每 8 小时结算一次
        - BTC-USDC: 每 1 小时结算一次
        - SOL-USDC: 每 4 小时结算一次
        
        Args:
            trading_pair: Hummingbot 格式的交易对名称，如 "ETH-USDC"
            
        Returns:
            下次资金费率结算的 UTC 时间戳（秒）
        """
        # 从 connector 中获取该交易对的资金费率结算周期（小时）
        funding_interval_hours = self._connector._funding_interval_hours.get(trading_pair, 8)
        
        # 计算下次结算时间
        # 例如：当前时间 10:30，结算周期 8 小时，下次结算时间为 16:00
        current_time = time.time()
        interval_seconds = funding_interval_hours * 3600
        
        # 找到下一个结算时间点
        next_funding_time = int(((current_time // interval_seconds) + 1) * interval_seconds)
        
        return next_funding_time