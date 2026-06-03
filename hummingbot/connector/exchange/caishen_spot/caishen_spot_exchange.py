import asyncio
import base64
import decimal
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from bidict import bidict
from eth_utils import to_bytes

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_auth import CaishenPerpetualAuth
from hummingbot.connector.derivative.caishen_perpetual.deepproto.schema import tx_pb2
from hummingbot.connector.exchange.caishen_spot import (
    caishen_spot_constants as CONSTANTS,
    caishen_spot_web_utils as web_utils,
)
from hummingbot.connector.exchange.caishen_spot.caishen_spot_api_order_book_data_source import (
    CaishenSpotAPIOrderBookDataSource,
)
from hummingbot.connector.exchange.caishen_spot.caishen_spot_api_user_stream_data_source import (
    CaishenSpotAPIUserStreamDataSource,
)
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class CaishenSpotExchange(ExchangePyBase):
    web_utils = web_utils

    def __init__(
        self,
        caishen_spot_api_key: str = None,
        caishen_spot_api_secret: str = None,
        balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
        rate_limits_share_pct: Decimal = Decimal("100"),
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DOMAIN,
        **kwargs,
    ):
        self.api_key = caishen_spot_api_key
        self.secret_key = caishen_spot_api_secret
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._domain = domain
        self._base_token_ids: Dict[str, int] = {}
        self._quote_token_ids: Dict[str, int] = {}
        self._token_symbols: Dict[int, str] = {}
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @property
    def name(self) -> str:
        return self._domain

    @property
    def authenticator(self) -> Optional[CaishenPerpetualAuth]:
        if self._trading_required:
            return CaishenPerpetualAuth(self.api_key, self.secret_key)
        return None

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self) -> str:
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self) -> str:
        return CONSTANTS.EXCHANGE_INFO_URL

    @property
    def trading_pairs_request_path(self) -> str:
        return CONSTANTS.EXCHANGE_INFO_URL

    @property
    def check_network_request_path(self) -> str:
        return CONSTANTS.PING_URL

    @property
    def trading_pairs(self) -> List[str]:
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self) -> List[OrderType]:
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception) -> bool:
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return "not found in open orders" in str(status_update_exception).lower()

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return str(CONSTANTS.ORDER_NOT_EXIST_CODE) in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(throttler=self._throttler, auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return CaishenSpotAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self._domain,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return CaishenSpotAPIUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self._domain,
        )

    def _get_fee(
        self,
        base_currency: str,
        quote_currency: str,
        order_type: OrderType,
        order_side: TradeType,
        amount: Decimal,
        price: Decimal = s_decimal_NaN,
        is_maker: Optional[bool] = None,
    ) -> TradeFeeBase:
        is_maker = is_maker or (order_type is OrderType.LIMIT_MAKER)
        return build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )

    async def _api_request(
        self,
        path_url,
        overwrite_url: Optional[str] = None,
        method: RESTMethod = RESTMethod.GET,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        is_auth_required: bool = False,
        return_err: bool = False,
        limit_id: Optional[str] = None,
        trading_pair: Optional[str] = None,
        headers: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        if limit_id is None:
            limit_id = web_utils.get_rest_api_limit_id_for_endpoint(
                endpoint=path_url,
                trading_pair=trading_pair,
            )
        return await super()._api_request(
            path_url=path_url,
            overwrite_url=overwrite_url,
            method=method,
            params=params,
            data=data,
            is_auth_required=is_auth_required,
            return_err=return_err,
            limit_id=limit_id,
            headers=headers,
            **kwargs,
        )

    async def _make_trading_rules_request(self) -> Any:
        return await self._api_get(
            path_url=self.trading_rules_request_path,
            params={"trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT},
        )

    async def _make_trading_pairs_request(self) -> Any:
        return await self._make_trading_rules_request()

    def _is_exchange_information_valid(self, exchange_info: Dict[str, Any]) -> bool:
        return web_utils.is_exchange_information_valid(exchange_info)

    async def _initialize_trading_pair_symbol_map(self):
        try:
            exchange_info = await self._make_trading_pairs_request()
            self._initialize_trading_pair_symbols_from_exchange_info(exchange_info=exchange_info)
        except Exception:
            self.logger().exception("Error requesting exchange info.")

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        if exchange_info.get("code") != 0:
            self.logger().warning(f"Failed to load symbols: {exchange_info.get('msg')}")
            self._set_trading_pair_symbol_map(mapping)
            return

        for symbol_info in exchange_info.get("data", {}).get("symbols", []):
            try:
                if symbol_info.get("trading_domain") not in (1, "1", None):
                    continue
                if symbol_info.get("status") != 1:
                    continue
                exchange_symbol = symbol_info["symbol"]
                base_symbol = symbol_info.get("base_token_symbol") or exchange_symbol.split("-")[0]
                quote_symbol = symbol_info.get("quote_token_symbol") or exchange_symbol.split("-")[1]
                trading_pair = combine_to_hb_trading_pair(base_symbol, quote_symbol)
                mapping[exchange_symbol] = trading_pair
                if symbol_info.get("base_token") is not None:
                    self._base_token_ids[trading_pair] = int(symbol_info["base_token"])
                    self._token_symbols[int(symbol_info["base_token"])] = base_symbol
                if symbol_info.get("quote_token") is not None:
                    self._quote_token_ids[trading_pair] = int(symbol_info["quote_token"])
                    self._token_symbols[int(symbol_info["quote_token"])] = quote_symbol
            except Exception:
                self.logger().exception(f"Error parsing symbol info: {symbol_info}")

        self._set_trading_pair_symbol_map(mapping)

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        trading_rules = []
        if exchange_info_dict.get("code") != 0:
            return trading_rules

        for symbol_info in exchange_info_dict.get("data", {}).get("symbols", []):
            try:
                if not self._is_exchange_information_valid(symbol_info):
                    continue
                exchange_symbol = symbol_info["symbol"]
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=exchange_symbol)
                quote_symbol = symbol_info.get("quote_token_symbol", CONSTANTS.CURRENCY)
                trading_rules.append(
                    TradingRule(
                        trading_pair=trading_pair,
                        min_order_size=Decimal(str(symbol_info.get("min_size", "0"))),
                        min_price_increment=Decimal(str(symbol_info.get("tick_size", "0.01"))),
                        min_base_amount_increment=Decimal(str(symbol_info.get("lot_size", "0.000001"))),
                        min_notional_size=Decimal(str(symbol_info.get("min_notional", "0"))),
                        buy_order_collateral_token=quote_symbol,
                        sell_order_collateral_token=quote_symbol,
                    )
                )
            except Exception:
                self.logger().exception(f"Error parsing trading rule: {symbol_info}")
        return trading_rules

    async def get_latest_block_hash(self) -> bytes:
        result = await self._api_get(path_url=CONSTANTS.GET_LATEST_BLOCK_URL)
        block_hash = result["data"]["block"]["hash"]
        return to_bytes(hexstr=block_hash)

    async def submit_tx(self, message_bytes: bytes) -> Dict[str, Any]:
        payload = {
            "message": base64.b64encode(message_bytes).decode("utf-8"),
            "async": False,
            "include_raw_tx_result": True,
        }
        return await self._api_post(path_url=CONSTANTS.SUBMIT_TX_URL, data=payload, is_auth_required=False)

    def _parse_place_order_result(self, order_id: str, order_result: Dict[str, Any]) -> Tuple[str, float]:
        response_code = order_result.get("code", -1)
        if response_code != 0:
            raise IOError(f"Error submitting order {order_id}: code={response_code}, msg={order_result.get('msg')}")

        data = order_result.get("data", {})
        raw_data_base64 = data.get("raw_tx_result", "")
        if not raw_data_base64:
            raise IOError(f"Error submitting order {order_id}: No raw_tx_result in response")

        raw_data_bytes = base64.b64decode(raw_data_base64)
        tx_result = tx_pb2.TxResult()
        tx_result.ParseFromString(raw_data_bytes)

        if tx_result.HasField("error"):
            error = tx_result.error
            error_code = getattr(error, "code", None)
            error_message = getattr(error, "message", "")
            raise IOError(f"Error submitting order {order_id}: {error_code} - {error_message}")

        if tx_result.HasField("place_order_result"):
            exchange_order_id = tx_result.place_order_result.id
            if not exchange_order_id:
                raise IOError(f"Error submitting order {order_id}: No order ID in place_order_result")
            return str(exchange_order_id), time.time()

        raise IOError(f"Error submitting order {order_id}: TxResult has no place_order_result")

    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        **kwargs,
    ) -> Tuple[str, float]:
        base_token_id = self._base_token_ids[trading_pair]
        quote_token_id = self._quote_token_ids[trading_pair]
        side = 0 if trade_type is TradeType.BUY else 1

        if order_type is OrderType.MARKET:
            order_type_int = CONSTANTS.ORDER_TYPE["MARKET"]
            time_in_force = CONSTANTS.ORDER_TIME_IN_FORCE["IOC"]
        elif order_type is OrderType.LIMIT_MAKER:
            order_type_int = CONSTANTS.ORDER_TYPE["LIMIT"]
            time_in_force = CONSTANTS.ORDER_TIME_IN_FORCE["POST_ONLY"]
        else:
            order_type_int = CONSTANTS.ORDER_TYPE["LIMIT"]
            time_in_force = CONSTANTS.ORDER_TIME_IN_FORCE["GTC"]

        form_data = {
            "base_token": base_token_id,
            "quote_token": quote_token_id,
            "side": side,
            "type": order_type_int,
            "time_in_force": time_in_force,
            "size": str(amount),
        }
        if order_type.is_limit_type() and price is not None and not price.is_nan():
            form_data["price"] = str(price)

        block_hash_bytes = await self.get_latest_block_hash()
        encoded_message = await self.authenticator.make_tx(
            self.api_key, block_hash_bytes, CONSTANTS.SPOT_PLACE_ORDER_ACTION, form_data
        )
        order_result = await self.submit_tx(encoded_message)
        return self._parse_place_order_result(order_id, order_result)

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        exchange_order_id = tracked_order.exchange_order_id
        if not exchange_order_id:
            exchange_order_id = await tracked_order.get_exchange_order_id()

        form_data = {"order_id": int(exchange_order_id)}
        block_hash_bytes = await self.get_latest_block_hash()
        encoded_message = await self.authenticator.make_tx(
            self.api_key, block_hash_bytes, CONSTANTS.SPOT_CANCEL_ORDER_ACTION, form_data
        )
        cancel_result = await self.submit_tx(encoded_message)

        response_code = cancel_result.get("code", -1)
        if response_code != 0:
            response_msg = cancel_result.get("msg", "")
            if response_code == CONSTANTS.ORDER_NOT_EXIST_CODE or "not found" in response_msg.lower():
                await self._order_tracker.process_order_not_found(order_id)
                return True
            raise IOError(f"Cancel failed for {order_id}: code={response_code}, msg={response_msg}")

        result = cancel_result.get("data", {}).get("result", {})
        error_info = result.get("error")
        block_result = result.get("block_result")
        if error_info and not block_result:
            error_code = error_info.get("code", "")
            error_message = error_info.get("message", "")
            if error_code == CONSTANTS.ORDER_NOT_EXIST_CODE or "not found" in str(error_message).lower():
                await self._order_tracker.process_order_not_found(order_id)
                return True
            raise IOError(f"Cancel failed for {order_id}: {error_code} - {error_message}")
        return True

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        exchange_order_id = tracked_order.exchange_order_id
        if not exchange_order_id:
            exchange_order_id = await tracked_order.get_exchange_order_id()

        response = await self._api_get(
            path_url=CONSTANTS.ORDER_OPEN_URL,
            params={
                "account": self.api_key,
                "trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT,
            },
        )
        if response.get("code") != 0:
            raise IOError(f"Failed to fetch open orders: {response.get('msg')}")

        open_orders = response.get("data", {}).get("open_orders", [])
        matched_order = None
        exchange_order_id_str = str(exchange_order_id)
        for order in open_orders:
            if str(order.get("order_id", "")) == exchange_order_id_str:
                matched_order = order
                break

        if matched_order is None:
            raise IOError(f"Order {exchange_order_id} not found in open orders.")

        timestamp_str = matched_order.get("on_chain_created_at", "0")
        try:
            timestamp_ms = int(timestamp_str)
        except (ValueError, TypeError):
            timestamp_ms = 0
        update_timestamp = timestamp_ms / 1000.0 if timestamp_ms > 0 else time.time()

        return OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=update_timestamp,
            new_state=CONSTANTS.get_order_state(matched_order.get("status")),
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id_str,
        )

    async def _update_balances(self):
        response = await self._api_get(
            path_url=CONSTANTS.BALANCES_URL,
            params={
                "account": self.api_key,
                "trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT,
            },
        )
        if response.get("code") != 0:
            self.logger().warning(f"Failed to fetch balances: {response.get('msg')}")
            return

        data = response.get("data", {})
        balances = data.get("balances")
        if balances is None and isinstance(data.get("balance"), dict):
            balances = [data["balance"]]
        if balances is None and data:
            balances = [data]

        if not balances:
            return

        for balance_info in balances:
            if not isinstance(balance_info, dict):
                continue
            asset = balance_info.get("symbol")
            if asset is None and balance_info.get("token_id") is not None:
                asset = self._token_symbols.get(int(balance_info["token_id"]))
            if asset is None:
                continue
            try:
                total = Decimal(str(balance_info.get("wallet", balance_info.get("total", "0")) or "0"))
                frozen = Decimal(str(balance_info.get("order_frozen", balance_info.get("cross_order_frozen", "0")) or "0"))
                available = total - frozen
                if available < Decimal("0"):
                    available = Decimal("0")
                self._account_balances[asset] = total
                self._account_available_balances[asset] = available
            except (ValueError, TypeError, decimal.InvalidOperation):
                self.logger().warning(f"Unable to parse balance entry: {balance_info}")

    async def _update_trading_fees(self):
        pass

    async def _user_stream_event_listener(self):
        async for event_message in self._iter_user_event_queue():
            try:
                method = event_message.get("method")
                payload = web_utils.unwrap_ws_notification_payload(
                    event_message.get("params", event_message)
                )

                if method == "order_changed_notification" and payload.get("order") is not None:
                    await self._process_order_message(payload["order"])
                elif method == "trade_notification":
                    await self._process_trade_message(payload)
                elif method == "balances_notification" and payload.get("balance") is not None:
                    await self._process_balance_message(payload["balance"])
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Unexpected error in user stream listener")

    async def _process_order_message(self, order_msg: Dict[str, Any]):
        exchange_order_id = str(order_msg.get("order_id", ""))
        if not exchange_order_id:
            return

        tracked_order = None
        for order in self._order_tracker.all_updatable_orders.values():
            if order.exchange_order_id == exchange_order_id:
                tracked_order = order
                break
        if tracked_order is None:
            return

        ts_ms = order_msg.get("updated_at", order_msg.get("ts", int(time.time() * 1000)))
        try:
            update_timestamp = int(ts_ms) / 1000.0
        except (ValueError, TypeError):
            update_timestamp = time.time()

        order_update = OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=update_timestamp,
            new_state=CONSTANTS.get_order_state(order_msg.get("status")),
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id,
        )
        self._order_tracker.process_order_update(order_update)

    async def _process_trade_message(self, trade_msg: Dict[str, Any]):
        exchange_order_id = str(trade_msg.get("order_id", ""))
        if not exchange_order_id:
            return

        tracked_order = None
        for order in self._order_tracker.all_fillable_orders.values():
            if order.exchange_order_id == exchange_order_id:
                tracked_order = order
                break
        if tracked_order is None:
            return

        trading_pair = tracked_order.trading_pair
        base, quote = trading_pair.split("-")
        fill_base_amount = Decimal(str(trade_msg.get("size", "0")))
        fill_price = Decimal(str(trade_msg.get("price", "0")))
        fee_amount = Decimal(str(trade_msg.get("fee", "0")))
        is_maker = trade_msg.get("is_maker", False)
        ts_ms = trade_msg.get("time", int(time.time() * 1000))
        try:
            fill_timestamp = int(ts_ms) / 1000.0
        except (ValueError, TypeError):
            fill_timestamp = time.time()

        fee = TradeFeeBase.new_spot_fee(
            fee_schema=self.trade_fee_schema(),
            trade_type=tracked_order.trade_type,
            percent_token=quote,
            flat_fees=[TokenAmount(quote, abs(fee_amount))],
        )
        trade_update = TradeUpdate(
            trade_id=str(trade_msg.get("tx_hash", f"{exchange_order_id}-{ts_ms}")),
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=trading_pair,
            fee=fee,
            fill_base_amount=fill_base_amount,
            fill_quote_amount=fill_base_amount * fill_price,
            fill_price=fill_price,
            fill_timestamp=fill_timestamp,
            is_taker=not is_maker,
        )
        self._order_tracker.process_trade_update(trade_update)

    async def _process_balance_message(self, balance_msg: Dict[str, Any]):
        asset = balance_msg.get("symbol")
        if asset is None and balance_msg.get("token_id") is not None:
            asset = self._token_symbols.get(int(balance_msg["token_id"]))
        if asset is None:
            return
        try:
            total = Decimal(str(balance_msg.get("wallet", "0") or "0"))
            frozen = Decimal(str(balance_msg.get("order_frozen", balance_msg.get("cross_order_frozen", "0")) or "0"))
            available = total - frozen
            if available < Decimal("0"):
                available = Decimal("0")
            self._account_balances[asset] = total
            self._account_available_balances[asset] = available
        except (ValueError, TypeError, decimal.InvalidOperation):
            self.logger().warning(f"Unable to parse balance websocket message: {balance_msg}")

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        response = await self._api_get(
            path_url=CONSTANTS.TICKER_PRICE_CHANGE_URL,
            params={
                "symbol": exchange_symbol,
                "trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT,
            },
        )
        if response.get("code") != 0:
            return 0.0
        return float(response.get("data", {}).get("price", "0"))

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        if not order.exchange_order_id:
            return []

        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=order.trading_pair)
        to_ts_ms = int(time.time() * 1000)
        from_ts_ms = to_ts_ms - 24 * 3600 * 1000
        response = await self._api_get(
            path_url=CONSTANTS.ACCOUNT_TRADE_LIST_URL,
            params={
                "account": self.api_key,
                "symbol": exchange_symbol,
                "cursor": "",
                "limit": 100,
                "from": from_ts_ms,
                "to": to_ts_ms,
                "trading_domain": CONSTANTS.TRADING_DOMAIN_SPOT,
            },
        )
        if response.get("code") != 0:
            return []

        trade_updates = []
        trades = response.get("data", {}).get("trades", [])
        base, quote = order.trading_pair.split("-")
        for trade in trades:
            if str(trade.get("order_id", "")) != str(order.exchange_order_id):
                continue
            fill_base_amount = Decimal(str(trade.get("size", "0")))
            fill_price = Decimal(str(trade.get("price", "0")))
            fee_amount = Decimal(str(trade.get("fee", "0")))
            ts_ms = trade.get("time", int(time.time() * 1000))
            fee = TradeFeeBase.new_spot_fee(
                fee_schema=self.trade_fee_schema(),
                trade_type=order.trade_type,
                percent_token=quote,
                flat_fees=[TokenAmount(quote, abs(fee_amount))],
            )
            trade_updates.append(
                TradeUpdate(
                    trade_id=str(trade.get("tx_hash", f"{order.exchange_order_id}-{ts_ms}")),
                    client_order_id=order.client_order_id,
                    exchange_order_id=str(order.exchange_order_id),
                    trading_pair=order.trading_pair,
                    fee=fee,
                    fill_base_amount=fill_base_amount,
                    fill_quote_amount=fill_base_amount * fill_price,
                    fill_price=fill_price,
                    fill_timestamp=int(ts_ms) / 1000.0,
                    is_taker=not trade.get("is_maker", False),
                )
            )
        return trade_updates
