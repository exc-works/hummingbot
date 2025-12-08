# your_dex_perpetual_derivative.py

import asyncio
import json
import time
from decimal import Decimal
from typing import Any, AsyncIterable, Dict, List, Optional, Tuple

from bidict import bidict


from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.derivative.caishen_perpetual import (
    caishen_perpetual_constants as CONSTANTS,
    caishen_perpetual_web_utils as web_utils,
)
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_auth import CaishenPerpetualAuth
from hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_api_order_book_data_source import (
    CaishenPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_user_stream_data_source import (
    CaishenPerpetualUserStreamDataSource,
)
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather
from hummingbot.connector.derivative.caishen_perpetual.caishen_perpetual_auth import make_and_submit_tx


class CaishenPerpetualDerivative(PerpetualDerivativePyBase):
    """
    Caishen DEX 永续合约连接器主类
    """
    
    web_utils = web_utils
    SHORT_POLL_INTERVAL = 5.0
    LONG_POLL_INTERVAL = 12.0
    
    def __init__(
            self,
            caishen_perpetual_api_key: str = None,
            caishen_perpetual_api_secret: str = None,
            trading_pairs: Optional[List[str]] = None,
            trading_required: bool = True,
            domain: str = CONSTANTS.DOMAIN,
            **kwargs
    ):
        self.api_key = caishen_perpetual_api_key
        self.secret_key = caishen_perpetual_api_secret
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._domain = domain
        # 存储每个交易对的资金费率结算周期（小时）
        self._funding_interval_hours: Dict[str, int] = {}
        # 存储每个交易对的 base_token 和 quote_token（整数 token ID，用于 API 调用）
        self._base_token_ids: Dict[str, int] = {}
        self._quote_token_ids: Dict[str, int] = {}
        super().__init__(**kwargs)
    
    @property
    def name(self) -> str:
        return self._domain
    
    @property
    def authenticator(self) -> Optional[CaishenPerpetualAuth]:
        if self._trading_required:
            return CaishenPerpetualAuth(self.api_key, self.secret_key)
        return None
    
    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        return CONSTANTS.RATE_LIMITS
    
    @property
    def domain(self) -> str:
        return self._domain
    
    @property
    def client_order_id_max_length(self) -> int:
        return CONSTANTS.MAX_ORDER_ID_LEN
    
    @property
    def client_order_id_prefix(self) -> str:
        return CONSTANTS.BROKER_ID
    
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
    
    @property
    def funding_fee_poll_interval(self) -> int:
        return 120

    async def _make_network_check_request(self):
        await self._api_get(path_url=self.check_network_request_path)
    
    def supported_order_types(self) -> List[OrderType]:
        return [OrderType.LIMIT, OrderType.MARKET, OrderType.LIMIT_MAKER]
    
    def supported_position_modes(self) -> List[PositionMode]:
        return [PositionMode.ONEWAY]
    
    def get_buy_collateral_token(self, trading_pair: str) -> str:
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.buy_order_collateral_token
    
    def get_sell_collateral_token(self, trading_pair: str) -> str:
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.sell_order_collateral_token
    
    def get_base_token_id(self, trading_pair: str) -> int:
        """
        获取指定交易对的 base_token ID（整数）
        
        Args:
            trading_pair: Hummingbot 格式的交易对，如 "ETH-USDC"
            
        Returns:
            base_token ID（整数），如 102
            
        Raises:
            KeyError: 如果交易对不存在
        """
        if trading_pair not in self._base_token_ids:
            raise KeyError(f"Base token ID not found for trading pair: {trading_pair}")
        return self._base_token_ids[trading_pair]
    
    def get_quote_token_id(self, trading_pair: str) -> int:
        """
        获取指定交易对的 quote_token ID（整数）
        
        Args:
            trading_pair: Hummingbot 格式的交易对，如 "ETH-USDC"
            
        Returns:
            quote_token ID（整数），如 101
            
        Raises:
            KeyError: 如果交易对不存在
        """
        if trading_pair not in self._quote_token_ids:
            raise KeyError(f"Quote token ID not found for trading pair: {trading_pair}")
        return self._quote_token_ids[trading_pair]
    
    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        return False

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            auth=self._auth)

    async def _make_trading_rules_request(self) -> Any:
        exchange_info = await self._api_get(path_url=self.trading_rules_request_path)
        return exchange_info

    async def _make_trading_pairs_request(self) -> Any:
        exchange_info = await self._api_get(path_url=self.trading_pairs_request_path)
        return exchange_info

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        # TODO: fixme
        return False

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        # TODO: fixme
        return False

    def quantize_order_price(self, trading_pair: str, price: Decimal) -> Decimal:
        """
        将订单价格量化为符合交易规则的精度
        """
        # 如果价格是 NaN，直接返回
        if price.is_nan():
            return price
        
        # 获取价格量化单位（tick_size）
        price_quantum = self.get_order_price_quantum(trading_pair, price)
        
        # 量化：(price // price_quantum) * price_quantum
        # 这确保价格是 price_quantum 的整数倍
        # 例如：price=100.456, quantum=0.01
        #      100.456 // 0.01 = 10045
        #      10045 * 0.01 = 100.45
        quantized_price = (price // price_quantum) * price_quantum
        
        return quantized_price

    async def _update_trading_rules(self):
        exchange_info = await self._api_get(path_url=self.trading_rules_request_path)
        trading_rules_list = await self._format_trading_rules(exchange_info)
        self._trading_rules.clear()
        for trading_rule in trading_rules_list:
            self._trading_rules[trading_rule.trading_pair] = trading_rule
        self._initialize_trading_pair_symbols_from_exchange_info(exchange_info=exchange_info)

    # 初始化获取交易对信息
    async def _initialize_trading_pair_symbol_map(self):
        try:
            exchange_info = await self._api_get(path_url=CONSTANTS.EXCHANGE_INFO_URL)

            self._initialize_trading_pair_symbols_from_exchange_info(exchange_info=exchange_info)
        except Exception:
            self.logger().exception("There was an error requesting symbols info.")

    
    def _create_order_book_data_source(self):
        return CaishenPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )
    
    def _create_user_stream_data_source(self):
        # 保留原有方法，返回 UserStreamDataSource（即使暂时不使用 WebSocket）
        return CaishenPerpetualUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )
    
    def _is_user_stream_initialized(self):
        # WebSocket 暂时未实现，总是返回 True，让连接器通过 REST API 轮询更新
        # 保留原有的 _create_user_stream_data_source 方法，待后续实现
        return True
    
    def _create_user_stream_tracker(self):
        # WebSocket 暂时未实现，返回 None
        # 保留原有的 _create_user_stream_data_source 方法，待后续实现
        return None
    
    def _create_user_stream_tracker_task(self):
        # WebSocket 暂时未实现，返回 None
        # 保留原有的 _create_user_stream_data_source 方法，待后续实现
        return None
    
    async def _user_stream_event_listener(self):
        # WebSocket 暂时未实现，此方法为空实现
        # 所有更新都通过 REST API 轮询完成（_status_polling_loop_fetch_updates）
        # 保留原有的 _create_user_stream_data_source 方法，待后续实现
        while True:
            await self._sleep(60.0)  # 保持任务运行但不做任何事

    async def _status_polling_loop_fetch_updates(self):
        await safe_gather(
            self._update_trade_history(),
            self._update_order_status(),
            self._update_balances(),
            self._update_positions(),
        )

    async def _update_order_status(self):
        await self._update_orders()

    async def _update_lost_orders_status(self):
        await self._update_lost_orders()

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 position_action: PositionAction,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = is_maker or False
        fee = build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )
        return fee

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        # TODO: fixme
        pass


    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        action_type = "CANCEL_ORDER"
        form_data = {
            "order_id": order_id,
        }

        cancel_result = await self.authenticator.make_and_submit_tx(self.api_key, self.secret_key, action_type, form_data)
        
        """
        Cancel result 结构：
        {
            "error": {
                "code": "NO_ERROR",  // 无错误时为 "NO_ERROR"
                "code_text": "string",
                "message": "string"
            },
            "block_result": {
                "number": "string"  // 区块号，大于0表示成功
            },
            "raw": "string"
        }
        """
        
        try:
            # 如果返回的是字符串，先解析为 JSON
            if isinstance(cancel_result, str):
                cancel_result = json.loads(cancel_result)
            
            # 正常情况下 error 和 block_result 只有一个有值
            error_info = cancel_result.get("error")
            block_result = cancel_result.get("block_result")
            
            # 情况1: 有错误（error 有值，block_result 为空）
            if error_info and not block_result:
                error_code = error_info.get("code", "")
                error_message = error_info.get("message", "")
                
                self.logger().warning(
                    f"撤单失败 - Order ID: {order_id}, "
                    f"Error Code: {error_code}, Message: {error_message}"
                )
                
                # 如果是订单不存在的错误，标记订单为未找到
                if "not found" in error_message.lower() or "does not exist" in error_message.lower():
                    self.logger().debug(f"订单 {order_id} 不存在，无需撤单")
                    await self._order_tracker.process_order_not_found(order_id)
                
                raise IOError(f"撤单失败: {error_code} - {error_message}")
            
            # 情况2: 成功（block_result 有值，error 为空）
            if block_result and not error_info:
                block_number = block_result.get("number", "0")
                try:
                    block_num = int(block_number)
                    if block_num > 0:
                        self.logger().info(
                            f"撤单成功 - Order ID: {order_id}, Block Number: {block_number}"
                        )
                        return True
                    else:
                        self.logger().warning(
                            f"撤单返回无效的区块号 - Order ID: {order_id}, Block Number: {block_number}"
                        )
                        return False
                except (ValueError, TypeError):
                    self.logger().error(
                        f"无法解析区块号 - Order ID: {order_id}, Block Number: {block_number}"
                    )
                    return False
            
            # 情况3: 异常情况（两者都有值或都没有值）
            self.logger().error(
                f"撤单结果异常 - Order ID: {order_id}, "
                f"error 和 block_result 状态异常, "
                f"error: {error_info}, block_result: {block_result}, "
                f"Result: {cancel_result}"
            )
            await self._order_tracker.process_order_not_found(order_id)
            return False
                
        except json.JSONDecodeError as e:
            self.logger().error(
                f"撤单结果 JSON 解析失败 - Order ID: {order_id}, Error: {e}, "
                f"Result: {cancel_result}"
            )
            await self._order_tracker.process_order_not_found(order_id)
            return False
            
        except (KeyError, TypeError) as e:
            self.logger().error(
                f"撤单结果格式错误 - Order ID: {order_id}, Error: {e}, "
                f"Result: {cancel_result}"
            )
            await self._order_tracker.process_order_not_found(order_id)
            return False


    async def _place_order(self, order_id: str, trading_pair: str, amount: Decimal, 
                           trade_type: TradeType, order_type: OrderType, price: Decimal,
                           position_action: PositionAction = PositionAction.NIL, **kwargs) -> Tuple[str, float]:
        """
        下单方法
        
        使用存储的 base_token 和 quote_token ID 来构建订单请求
        """
        # 获取 base_token 和 quote_token ID（整数）
        base_token_id = self.get_base_token_id(trading_pair)
        quote_token_id = self.get_quote_token_id(trading_pair)
        
        side = 0 if trade_type is TradeType.BUY else 1
        
        if order_type is OrderType.MARKET:
            order_type_int = 1  
        elif order_type is OrderType.LIMIT:
            order_type_int = 0  
        elif order_type is OrderType.LIMIT_MAKER:
            order_type_int = 1  
        else:
            order_type_int = 1  # 默认值
        
        # 设置 time_in_force（根据订单类型）
        # 需要根据实际 API 文档确认
        if order_type is OrderType.MARKET:
            time_in_force = "IOC"  # Immediate or Cancel
        elif order_type is OrderType.LIMIT_MAKER:
            time_in_force = "GTC"  # Good Till Cancel
        else:
            time_in_force = "GTC"  # Good Till Cancel
        
        action_type = "PLACE_ORDER"
        form_data = {
            "base_token": base_token_id,
            "quote_token": quote_token_id,
            "side": side,
            "type": order_type_int,
            "time_in_force": time_in_force,
            "size": str(amount),
            "reduce_only": position_action == PositionAction.CLOSE
        }
        if price is not None and order_type.is_limit_type():
            form_data['price'] = str(price)

        order_result = await self.authenticator.make_and_submit_tx(self.api_key, self.secret_key, action_type, form_data)
        # TODO: 解析订单结果并返回 (exchange_order_id, timestamp)
        # 需要根据实际 API 响应格式解析
        raise NotImplementedError("Order result parsing not implemented yet")
    
    async def _update_trade_history(self):
        """
        通过 REST API 获取账户交易历史（成交记录）
        
        这个方法通过 REST API 轮询获取交易历史，不是通过 WebSocket。
        在 _status_polling_loop_fetch_updates() 中定期调用。
        
        API 端点: GET /v1/trades/by-account
        请求参数: account (账户地址), cursor (分页游标), limit (限制数量)
        
        预期响应格式:
        {
            "code": 0,
            "msg": "",
            "data": {
                "trades": [
                    {
                        "trade_id": "12345",
                        "order_id": "67890",
                        "symbol": "ETH-USDC",
                        "side": "buy",  // 或 "sell"
                        "price": "3000.5",
                        "size": "1.5",
                        "fee": "0.001",
                        "timestamp": 1234567890000,  // 毫秒时间戳
                        "position_action": "open"  // 或 "close"
                    }
                ],
                "cursor": 100,
                "has_more": false
            }
        }
        """
        orders = list(self._order_tracker.all_fillable_orders.values())
        all_fillable_orders = self._order_tracker.all_fillable_orders_by_exchange_order_id
        
        # 只有在有待成交订单时才查询交易历史
        if len(orders) == 0:
            return
        
        try:
            # 调用 REST API 获取交易历史
            # 注意：根据实际 API 文档调整参数和端点
            response = await self._api_get(
                path_url=CONSTANTS.ACCOUNT_TRADE_LIST_URL,
                params={
                    "account": self.api_key,
                    "cursor": 0,  # 分页游标，可以根据需要调整
                    "limit": 100  # 每次获取的交易数量
                },
                is_auth_required=True
            )
            
            # 检查 API 响应状态
            if response.get("code") != 0:
                self.logger().warning(
                    f"获取交易历史失败: {response.get('msg')}"
                )
                return
            
            # 提取交易列表
            data = response.get("data", {})
            trades = data.get("trades", [])
            
            # 处理每条交易记录
            for trade_fill in trades:
                self._process_trade_rs_event_message(
                    order_fill=trade_fill,
                    all_fillable_order=all_fillable_orders
                )
                
        except asyncio.CancelledError:
            raise
        except Exception as request_error:
            self.logger().warning(
                f"Failed to fetch trade updates. Error: {request_error}",
                exc_info=request_error,
            )

    def _process_trade_rs_event_message(self, order_fill: Dict[str, Any], all_fillable_order):
        """
        处理从 REST API 返回的单条交易记录
        
        参数:
            order_fill: 单条交易记录，格式根据 Caishen API 响应调整
            all_fillable_order: 所有可成交订单的字典，key 为 exchange_order_id
        
        预期 order_fill 格式:
        {
            "trade_id": "12345",           # 交易ID
            "order_id": "67890",           # 订单ID（交易所订单ID）
            "symbol": "ETH-USDC",          # 交易对
            "side": "buy",                 # 买卖方向
            "price": "3000.5",             # 成交价格
            "size": "1.5",                 # 成交数量
            "fee": "0.001",                # 手续费
            "timestamp": 1234567890000,    # 时间戳（毫秒）
            "position_action": "open"      # 开仓/平仓
        }
        """
        # 从交易记录中获取订单ID（交易所订单ID）
        exchange_order_id = str(order_fill.get("order_id", ""))
        
        # 查找对应的订单
        fillable_order = all_fillable_order.get(exchange_order_id)
        
        # 如果找不到对应的订单，尝试通过其他方式查找
        if fillable_order is None:
            # 可以尝试通过 trade_id 或其他字段查找
            # 或者记录日志并跳过
            self.logger().debug(
                f"找不到对应的订单，跳过交易记录: {order_fill}"
            )
            return
        
        # 获取手续费资产（通常是 quote asset）
        fee_asset = fillable_order.quote_asset
        
        # 确定是开仓还是平仓
        position_action_str = order_fill.get("position_action", "open").lower()
        position_action = PositionAction.OPEN if position_action_str == "open" else PositionAction.CLOSE
        
        # 构建手续费对象
        fee = TradeFeeBase.new_perpetual_fee(
            fee_schema=self.trade_fee_schema(),
            position_action=position_action,
            percent_token=fee_asset,
            flat_fees=[TokenAmount(
                amount=Decimal(str(order_fill.get("fee", "0"))),
                token=fee_asset
            )]
        )
        
        # 获取时间戳（转换为秒）
        timestamp_ms = order_fill.get("timestamp", 0)
        fill_timestamp = timestamp_ms / 1000.0 if timestamp_ms > 0 else time.time()
        
        # 创建交易更新对象
        trade_update = TradeUpdate(
            trade_id=str(order_fill.get("trade_id", "")),
            client_order_id=fillable_order.client_order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=fillable_order.trading_pair,
            fee=fee,
            fill_base_amount=Decimal(str(order_fill.get("size", "0"))),
            fill_quote_amount=Decimal(str(order_fill.get("price", "0"))) * Decimal(str(order_fill.get("size", "0"))),
            fill_price=Decimal(str(order_fill.get("price", "0"))),
            fill_timestamp=fill_timestamp,
        )
        
        # 处理交易更新
        self._order_tracker.process_trade_update(trade_update)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        # Use _update_trade_history instead
        pass

    async def _handle_update_error_for_active_order(self, order: InFlightOrder, error: Exception):
        try:
            raise error
        except (asyncio.TimeoutError, KeyError):
            self.logger().debug(
                f"Tracked order {order.client_order_id} does not have an exchange id. "
                f"Attempting fetch in next polling interval."
            )
            await self._order_tracker.process_order_not_found(order.client_order_id)
        except asyncio.CancelledError:
            raise
        except Exception as request_error:
            self.logger().warning(
                f"Error fetching status update for the active order {order.client_order_id}: {request_error}.",
            )
            self.logger().debug(
                f"Order {order.client_order_id} not found counter: {self._order_tracker._order_not_found_records.get(order.client_order_id, 0)}")
            await self._order_tracker.process_order_not_found(order.client_order_id)

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        """
        通过查询所有未关闭订单来获取指定订单的状态
        
        Caishen 没有查询单个订单状态的接口，所以需要：
        1. 调用 GET /v1/orders/open 获取所有未关闭的订单
        2. 在返回的订单列表中匹配 order_id（exchange_order_id）
        3. 如果找到订单，返回其状态；如果找不到，说明订单已关闭或不存在
        
        响应格式:
        {
            "code": 0,
            "msg": "",
            "data": {
                "open_orders": [
                    {
                        "order_id": "string",
                        "status": 0,  # 整数状态
                        "on_chain_created_at": "string",  # 时间戳（可能是字符串或整数）
                        ...
                    }
                ]
            }
        }
        """
        # 只使用 exchange_order_id，不使用 client_order_id
        if not tracked_order.exchange_order_id:
            try:
                exchange_order_id = await tracked_order.get_exchange_order_id()
            except asyncio.TimeoutError:
                exchange_order_id = None
        else:
            exchange_order_id = tracked_order.exchange_order_id
        
        # 如果没有 exchange_order_id，无法查询
        if not exchange_order_id:
            raise ValueError(f"Missing exchange_order_id for order {tracked_order.client_order_id}")
        
        # 调用 GET 接口查询所有未关闭的订单
        response = await self._api_get(
            path_url=CONSTANTS.ORDER_OPEN_URL,
            params={"account": self.api_key},
            is_auth_required=True
        )
        
        # 检查 API 响应状态
        if response.get("code") != 0:
            error_msg = response.get("msg", "Unknown error")
            self.logger().warning(
                f"查询未关闭订单列表失败: {error_msg}"
            )
            raise IOError(f"Failed to fetch open orders: {error_msg}")
        
        # 提取订单列表
        data = response.get("data", {})
        open_orders = data.get("open_orders", [])
        
        # 在订单列表中查找匹配的订单
        matched_order = None
        exchange_order_id_str = str(exchange_order_id)
        
        for order in open_orders:
            order_id = str(order.get("order_id", ""))
            if order_id == exchange_order_id_str:
                matched_order = order
                break
        
        # 如果找不到订单，说明订单已关闭或不存在
        if matched_order is None:
            # 订单不在未关闭订单列表中，说明已关闭或不存在
            # 抛出异常，让调用方处理（可能会标记为已取消或已成交）
            raise IOError(
                f"Order {exchange_order_id} not found in open orders. "
                f"It may have been filled, canceled, or does not exist."
            )
        
        # 解析订单状态
        order_status = matched_order.get("status")
        current_state = CONSTANTS.get_order_state(order_status)
        
        # 获取时间戳
        # on_chain_created_at 可能是字符串或整数（毫秒时间戳）
        timestamp_str = matched_order.get("on_chain_created_at", "0")
        try:
            if isinstance(timestamp_str, str):
                # 如果是字符串，尝试转换为整数
                timestamp_ms = int(timestamp_str) if timestamp_str else 0
            else:
                timestamp_ms = int(timestamp_str) if timestamp_str else 0
        except (ValueError, TypeError):
            timestamp_ms = 0
        
        # 转换为秒（如果时间戳是毫秒）
        update_timestamp = timestamp_ms / 1000.0 if timestamp_ms > 0 else time.time()
        
        # 创建订单更新对象
        _order_update: OrderUpdate = OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=update_timestamp,
            new_state=current_state,
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id_str,
        )
        
        return _order_update

    async def _iter_user_event_queue(self) -> AsyncIterable[Dict[str, any]]:
        while True:
            try:
                yield await self._user_stream_tracker.user_stream.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    "Unknown error. Retrying after 1 seconds.",
                    exc_info=True,
                    app_warning_msg="Could not fetch user events from Hyperliquid. Check API key and network connection.",
                )
                await self._sleep(1.0)

    async def _user_stream_event_listener(self):
        """
        Listens to messages from _user_stream_tracker.user_stream queue.
        Traders, Orders, and Balance updates from the WS.
        """
        user_channels = [
            CONSTANTS.USER_ORDERS_ENDPOINT_NAME,
            CONSTANTS.USEREVENT_ENDPOINT_NAME,
        ]
        async for event_message in self._iter_user_event_queue():
            try:
                if isinstance(event_message, dict):
                    channel: str = event_message.get("channel", None)
                    results = event_message.get("data", None)
                elif event_message is asyncio.CancelledError:
                    raise asyncio.CancelledError
                else:
                    raise Exception(event_message)
                if channel not in user_channels:
                    self.logger().error(
                        f"Unexpected message in user stream: {event_message}.", exc_info=True)
                    continue
                if channel == CONSTANTS.USER_ORDERS_ENDPOINT_NAME:
                    for order_msg in results:
                        self._process_order_message(order_msg)
                elif channel == CONSTANTS.USEREVENT_ENDPOINT_NAME:
                    if "fills" in results:
                        for trade_msg in results["fills"]:
                            await self._process_trade_message(trade_msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(
                    "Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    async def _process_trade_message(self, trade: Dict[str, Any], client_order_id: Optional[str] = None):
        """
        Updates in-flight order and trigger order filled event for trade message received. Triggers order completed
        event if the total executed amount equals to the specified order amount.
        Example Trade:
        """
        exchange_order_id = str(trade.get("oid", ""))
        tracked_order = self._order_tracker.all_fillable_orders_by_exchange_order_id.get(exchange_order_id)

        if tracked_order is None:
            all_orders = self._order_tracker.all_fillable_orders
            for k, v in all_orders.items():
                await v.get_exchange_order_id()
            _cli_tracked_orders = [o for o in all_orders.values() if exchange_order_id == o.exchange_order_id]
            if not _cli_tracked_orders:
                self.logger().debug(f"Ignoring trade message with id {client_order_id}: not in in_flight_orders.")
                return
            tracked_order = _cli_tracked_orders[0]
        trading_pair_base_coin = tracked_order.base_asset
        if trade["coin"] == trading_pair_base_coin:
            position_action = PositionAction.OPEN if trade["dir"].split(" ")[0] == "Open" else PositionAction.CLOSE
            fee_asset = tracked_order.quote_asset
            fee = TradeFeeBase.new_perpetual_fee(
                fee_schema=self.trade_fee_schema(),
                position_action=position_action,
                percent_token=fee_asset,
                flat_fees=[TokenAmount(amount=Decimal(trade["fee"]), token=fee_asset)]
            )
            trade_update: TradeUpdate = TradeUpdate(
                trade_id=str(trade["tid"]),
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=str(trade["oid"]),
                trading_pair=tracked_order.trading_pair,
                fill_timestamp=trade["time"] * 1e-3,
                fill_price=Decimal(trade["px"]),
                fill_base_amount=Decimal(trade["sz"]),
                fill_quote_amount=Decimal(trade["px"]) * Decimal(trade["sz"]),
                fee=fee,
            )
            self._order_tracker.process_trade_update(trade_update)

    def _process_order_message(self, order_msg: Dict[str, Any]):
        """
        Updates in-flight order and triggers cancelation or failure event if needed.

        :param order_msg: The order response from either REST or web socket API (they are of the same format)

        Example Order:
        """
        client_order_id = str(order_msg["order"].get("cloid", ""))
        tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
        if not tracked_order:
            self.logger().debug(f"Ignoring order message with id {client_order_id}: not in in_flight_orders.")
            return
        current_state = order_msg["status"]
        tracked_order.update_exchange_order_id(str(order_msg["order"]["oid"]))
        order_update: OrderUpdate = OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=order_msg["statusTimestamp"] * 1e-3,
            new_state=CONSTANTS.ORDER_STATE[current_state],
            client_order_id=order_msg["order"]["cloid"],
            exchange_order_id=str(order_msg["order"]["oid"]),
        )
        self._order_tracker.process_order_update(order_update=order_update)

    async def _format_trading_rules(self, exchange_info_dict: Dict) -> List[TradingRule]:
        """
        从交易所返回的信息中初始化每个交易对的 TradingRule 对象
        
        Parameters
        ----------
        exchange_info_dict:
            交易规则字典，来自交易所的响应
            
        API 返回格式：
        {
            "code": 0,
            "msg": "",
            "data": {
                "symbols": [
                    {
                        "symbol": "ETH-USDC",
                        "tick_size": "0.01",       // 价格最小变动单位
                        "lot_size": "0.01",        // 数量最小变动单位
                        "min_size": "0.01",        // 最小订单数量
                        "funding_interval_hours": 8,
                        "status": 1
                    }
                ]
            }
        }
        """
        trading_rules = []
        
        # 检查 API 响应状态
        if exchange_info_dict.get("code") != 0:
            self.logger().warning(f"获取交易规则失败: {exchange_info_dict.get('msg')}")
            return trading_rules
        
        data = exchange_info_dict.get("data", {})
        symbols = data.get("symbols", [])
        
        for symbol_info in symbols:
            try:
                # 检查交易对状态是否可用 (status=1 表示可用)
                if symbol_info.get("status") != 1:
                    continue
                
                # 获取交易对符号
                exchange_symbol = symbol_info["symbol"]  # "ETH-USDC"
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=exchange_symbol)
                
                # 价格最小变动单位 (tick_size)
                # 例如: "0.01" 表示价格只能是 0.01 的倍数
                min_price_increment = Decimal(str(symbol_info.get("tick_size", "0.01")))
                
                # 数量最小变动单位 (lot_size)
                # 例如: "0.01" 表示数量只能是 0.01 的倍数
                min_base_amount_increment = Decimal(str(symbol_info.get("lot_size", "0.01")))
                
                # 最小订单数量 (min_size)
                # 例如: "0.01" 表示最小下单数量为 0.01
                min_order_size = Decimal(str(symbol_info.get("min_size", "0.01")))
                
                # 保证金代币（永续合约使用的结算货币）
                collateral_token = CONSTANTS.CURRENCY  # "USDC"
                
                # 保存资金费率结算周期（用于 FundingInfo 计算）
                funding_interval_hours = symbol_info.get("funding_interval_hours", 8)
                self._funding_interval_hours[trading_pair] = funding_interval_hours
                
                # 创建交易规则对象
                trading_rule = TradingRule(
                    trading_pair=trading_pair,
                    min_order_size=min_order_size,                           # 最小订单数量
                    min_price_increment=min_price_increment,                 # 价格最小变动
                    min_base_amount_increment=min_base_amount_increment,     # 数量最小变动
                    buy_order_collateral_token=collateral_token,             # 买单保证金代币
                    sell_order_collateral_token=collateral_token,            # 卖单保证金代币
                )
                
                trading_rules.append(trading_rule)
                
            except Exception as e:
                self.logger().error(
                    f"解析交易规则时出错 - Symbol: {symbol_info.get('symbol', 'UNKNOWN')}, "
                    f"Error: {e}",
                    exc_info=True
                )
        
        return trading_rules

    # 初始化交易对符号映射
    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict):
        """
        从交易所返回的信息中初始化交易对符号映射
        
        API 返回格式：
        {
            "code": 0,
            "msg": "",
            "data": {
                "symbols": [
                    {
                        "symbol": "ETH-USDC",
                        "base_token": 102,
                        "base_token_symbol": "ETH",
                        "quote_token": 101,
                        "quote_token_symbol": "USDC",
                        "tick_size": "0.01",
                        "lot_size": "0.01",
                        "min_size": "0.01",
                        "max_number_of_order_each_account": 50,
                        "max_number_of_order_each_match": 1000,
                        "funding_interval_hours": 8,
                        "funding_rate_bound": "0.03",
                        "price_limit_bound": "0.1",
                        "default_leverage": 20,
                        "status": 1,
                        "open_at": 1761091200000,
                        "close_at": 0,
                    }
                ]
            }
        }
        """
        mapping = bidict()
        
        # 从 API 响应中提取 symbols 列表
        if exchange_info.get("code") != 0:
            self.logger().warning(f"获取交易对信息失败: {exchange_info.get('msg')}")
            self._set_trading_pair_symbol_map(mapping)
            return
        
        data = exchange_info.get("data", {})
        symbols = data.get("symbols", [])
        
        # 遍历每个交易对信息并建立映射
        for symbol_info in symbols:
            try:
                # 检查交易对状态是否可用 (status=1 表示可用)
                if symbol_info.get("status") != 1:
                    continue
                
                exchange_symbol = symbol_info["symbol"]  # e.g., "ETH-USDC"
                
                # base_token 和 quote_token 是整数 token ID（用于 API 调用）
                base_token_id = symbol_info.get("base_token")  # e.g., 102
                quote_token_id = symbol_info.get("quote_token")  # e.g., 101
                
                # base_token_symbol 和 quote_token_symbol 是字符串 symbol（用于创建 trading_pair）
                base_token_symbol = exchange_symbol.split("-")[0]
                quote_token_symbol = exchange_symbol.split("-")[1]
                
                # 使用 hummingbot 的标准格式创建交易对 (e.g., "ETH-USDC")
                trading_pair = combine_to_hb_trading_pair(base_token_symbol, quote_token_symbol)
                
                # 建立交易所符号到 hummingbot 交易对的映射
                mapping[exchange_symbol] = trading_pair
                
                # 保存资金费率结算周期（小时）
                # 不同交易对的结算周期可能不同：ETH-USDC=8小时, BTC-USDC=1小时, SOL-USDC=4小时
                funding_interval_hours = symbol_info.get("funding_interval_hours", 8)  # 默认8小时
                self._funding_interval_hours[trading_pair] = funding_interval_hours
                
                # 保存 base_token 和 quote_token（整数 token ID），用于后续 API 调用（下单、调整杠杆等）
                if base_token_id is not None:
                    self._base_token_ids[trading_pair] = int(base_token_id)
                if quote_token_id is not None:
                    self._quote_token_ids[trading_pair] = int(quote_token_id)
                
            except Exception as exception:
                self.logger().error(
                    f"解析交易对信息时出错 ({exception}). Symbol: {symbol_info}"
                )
        
        self._set_trading_pair_symbol_map(mapping)


    async def _get_last_traded_price(self, trading_pair: str) -> float:
        """
        获取指定交易对的最新成交价格
        
        API 响应格式：
        {
            "code": 0,
            "msg": "",
            "data": {
                "symbol": "BTC-USDC",
                "price": "0",          # 最新成交价（可能为0）
                "mark_price": "90759.2",   # 标记价格
                "index_price": "92779.3",  # 指数价格
                ...
            }
        }
        """
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        
        response = await self._api_get(
            path_url=CONSTANTS.TICKER_PRICE_CHANGE_URL,
            params={"symbol": exchange_symbol}
        )
        
        # 检查 API 返回的状态码
        if response.get("code") != 0:
            self.logger().warning(f"Error fetching ticker for {trading_pair}: {response.get('msg')}")
            return 0.0
        
        data = response.get("data", {})
        
        return float(data.get("price", "0"))

    def _resolve_trading_pair_symbols_duplicate(self, mapping: bidict, new_exchange_symbol: str, base: str, quote: str):
        """Resolves name conflicts provoked by futures contracts.

        If the expected BASEQUOTE combination matches one of the exchange symbols, it is the one taken, otherwise,
        the trading pair is removed from the map and an error is logged.
        """
        expected_exchange_symbol = f"{base}{quote}"
        trading_pair = combine_to_hb_trading_pair(base, quote)
        current_exchange_symbol = mapping.inverse[trading_pair]
        if current_exchange_symbol == expected_exchange_symbol:
            pass
        elif new_exchange_symbol == expected_exchange_symbol:
            mapping.pop(current_exchange_symbol)
            mapping[new_exchange_symbol] = trading_pair
        else:
            self.logger().error(
                f"Could not resolve the exchange symbols {new_exchange_symbol} and {current_exchange_symbol}")
            mapping.pop(current_exchange_symbol)

    async def _update_balances(self):
        """
        更新余额
        
        API 返回格式：
        {
            "code": 0,
            "msg": "",
            "data": {
                "balance": {
                    "token_id": 101,
                    "wallet": "9147554.37199109",  // 钱包总余额
                    "isolated_position_frozen": "2.5888",  // 逐仓持仓冻结
                    "isolated_order_frozen": "0",  // 逐仓订单冻结
                    "cross_order_frozen": "0",  // 全仓订单冻结
                    "symbol": "USDC"
                },
                "fee_tier": {
                    "taker": "0.00045",
                    "maker": "0.00015"
                }
            }
        }
        """
        account_info = await self._api_get(
            path_url=CONSTANTS.ACCOUNT_INFO_URL,
            params={"account": self.api_key}
        )
        
        # 检查 API 响应状态
        if account_info.get("code") != 0:
            self.logger().warning(f"获取余额信息失败: {account_info.get('msg')}")
            return
        
        data = account_info.get("data", {})
        balance_info = data.get("balance", {})
        
        quote = CONSTANTS.CURRENCY
        
        # 总余额 = 钱包余额
        total_balance = Decimal(str(balance_info.get("wallet", "0")))
        
        # 可用余额 = 钱包余额 - 所有冻结资金
        isolated_position_frozen = Decimal(str(balance_info.get("isolated_position_frozen", "0")))
        isolated_order_frozen = Decimal(str(balance_info.get("isolated_order_frozen", "0")))
        cross_order_frozen = Decimal(str(balance_info.get("cross_order_frozen", "0")))
        
        available_balance = total_balance - isolated_position_frozen - isolated_order_frozen - cross_order_frozen
        
        # 更新账户余额
        self._account_balances[quote] = total_balance
        self._account_available_balances[quote] = available_balance
    
    async def _update_positions(self):
        """更新持仓"""
        """
        API 返回格式：
        {
            "code": 0,
            "msg": "",
            "data": {
                "open_positions": [
                {
                    "position_id": 251,
                    "account": "0x394ab7622054a7c8faf3bfdffd77c408ac74c2ca",
                    "mode": 0,
                    "side": 1,
                    "base_token": 102,
                    "quote_token": 101,
                    "symbol": "ETH-USDC",
                    "entry_price": "3044.11850586",
                    "size": "4.22",
                    "realized_pnl": "52.9131052",
                    "liquidation_price": "2362213.06",
                    "taker_fee_rate": "0.00045",
                    "funding_fee": "0",
                    "risk_limits_id": 101,
                    "on_chain_created_at": 1764901193301,
                    "liquidation_order": false
                }
                ]
            }
        }
        """
        response = await self._api_get(path_url=CONSTANTS.POSITION_INFORMATION_URL,params={"account": self.api_key})
        if response.get("code") != 0:
            self.logger().warning(f"获取持仓信息失败: {response.get('msg')}")
            return
        data = response.get("data", {})
        positions = data.get("open_positions", [])
        for position in positions:
            ex_trading_pair = position.get("symbol")
            hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(ex_trading_pair)

            position_side = PositionSide.LONG if position.get("side") == 0 else PositionSide.SHORT
            # unrealized_pnl = Decimal(position.get("unrealizedPnl")) fixme
            entry_price = Decimal(position.get("entry_price"))
            amount = Decimal(position.get("size", 0))
            # leverage = Decimal(position.get("leverage").get("value")) fixme
            pos_key = self._perpetual_trading.position_key(hb_trading_pair, position_side)
            if amount != 0:
                _position = Position(
                    trading_pair=hb_trading_pair,
                    position_side=position_side,
                    unrealized_pnl=unrealized_pnl,
                    entry_price=entry_price,
                    amount=amount,
                    leverage=leverage
                )
                self._perpetual_trading.set_position(pos_key, _position)
            else:
                self._perpetual_trading.remove_position(pos_key)
        if not positions:
            keys = list(self._perpetual_trading.account_positions.keys())
            for key in keys:
                self._perpetual_trading.remove_position(key)


    async def _get_position_mode(self) -> Optional[PositionMode]:
        return PositionMode.ONEWAY

    async def _trading_pair_position_mode_set(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        msg = ""
        success = True
        initial_mode = await self._get_position_mode()
        if initial_mode != mode:
            msg = "caishen_perpetual only supports the ONEWAY position mode."
            success = False
        return success, msg
    
    async def _set_trading_pair_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        """
        设置交易对的杠杆
        
        使用存储的 base_token 和 quote_token ID 来构建请求
        """
        # 获取 base_token 和 quote_token ID（整数）
        base_token_id = self.get_base_token_id(trading_pair)
        quote_token_id = self.get_quote_token_id(trading_pair)
        
        action_type = "SET_POSITION_LEVERAGE"
        form_data = {
            "base_token": base_token_id,
            "quote_token": quote_token_id,
            "leverage": leverage,
        }
        
        try:
            response = self.authenticator.make_and_submit_tx(self.api_key, self.secret_key, action_type, form_data)
            # 如果返回的是字符串，先解析为 JSON
            if isinstance(response, str):
                set_result = json.loads(response)
            
            # 正常情况下 error 和 block_result 只有一个有值
            error_info = set_result.get("error")
            if not error_info:
                return True, ""
            else:
                msg = "设置杠杆失败"
                return False,msg
        except json.JSONDecodeError as e:
            msg = "设置杠杆结果 JSON 解析失败"
            return False,msg
            
        except (KeyError, TypeError) as e:
            msg = "设置杠杆结果格式错误"
            return False,msg
        

    
    async def _fetch_last_fee_payment(self, trading_pair: str) -> Tuple[float, Decimal, Decimal]:
        """
        获取最近一次资金费率支付记录
        
        Returns:
            Tuple[timestamp, funding_rate, payment]:
            - timestamp: 支付时间戳（秒）
            - funding_rate: 资金费率
            - payment: 支付金额（正数=收入，负数=支出）
        """
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
        
        # 计算时间范围：从上次资金费率结算时间到现在
        start_ms = self._last_funding_time(trading_pair)  # 毫秒时间戳
        end_ms = int(time.time() * 1000)                  # 当前时间（毫秒）
        
        # 1. 获取账户资金费率支付历史
        account_funding_info_response = await self._api_get(
            path_url=CONSTANTS.ACCOUNT_FUNDING_HISTORY_URL,
            params={"account": self.api_key, "cursor": 0, "limit": 100}
        )
        
        if account_funding_info_response.get("code") != 0:
            self.logger().warning(
                f"获取账户资金费率支付历史失败: {account_funding_info_response.get('msg')}"
            )
            return 0, Decimal("-1"), Decimal("-1")
        
        # 2. 从账户支付历史中筛选匹配的记录
        account_data = account_funding_info_response.get("data", {})
        history = account_data.get("history", [])
        
        # 按 symbol 和 start 时间过滤
        matching_payments = []
        for record in history:
            record_symbol = record.get("symbol")
            record_time_str = record.get("time", "0")
            
            # 检查 symbol 是否匹配
            if record_symbol != exchange_symbol:
                continue
            
            # 检查时间是否 >= start（转为毫秒比较）
            try:
                record_time_ms = int(record_time_str)
                if record_time_ms >= start_ms:
                    matching_payments.append(record)
            except (ValueError, TypeError):
                continue
        
        # 如果没有匹配的支付记录
        if not matching_payments:
            return 0, Decimal("-1"), Decimal("-1")
        
        # 按时间降序排序，取最新的支付记录
        latest_payment = sorted(
            matching_payments,
            key=lambda x: int(x.get("time", "0")),
            reverse=True
        )[0]
        
        # 提取 payment 和 timestamp
        payment = Decimal(str(latest_payment.get("funding_fee", "0")))
        payment_time_ms = int(latest_payment.get("time", "0"))
        payment_timestamp = payment_time_ms / 1000  # 转为秒
        
        # 如果 payment 为 0，返回无效标记
        if payment == Decimal("0"):
            return 0, Decimal("-1"), Decimal("-1")
        
        # 3. 获取资金费率历史（用于获取对应的 funding_rate）
        funding_info_response = await self._api_get(
            path_url=CONSTANTS.FUNDING_HISTORY_URL,
            params={
                "symbol": exchange_symbol,
                "start_time": start_ms,  # 毫秒时间戳
                "end_time": end_ms       # 毫秒时间戳
            }
        )
        
        if funding_info_response.get("code") != 0:
            self.logger().warning(
                f"获取资金费率历史失败: {funding_info_response.get('msg')}"
            )
            # 即使获取 funding_rate 失败，也返回 payment 数据
            return payment_timestamp, Decimal("0"), payment
        
        # 4. 从资金费率历史中筛选时间范围内的记录
        funding_data = funding_info_response.get("data", {})
        funding_histories = funding_data.get("funding_histories", [])
        
        # 筛选时间在 [start, end] 范围内的记录
        matching_rates = []
        for rate_record in funding_histories:
            rate_time = rate_record.get("time", 0)
            if start_ms <= rate_time <= end_ms:
                matching_rates.append(rate_record)
        
        # 如果没有匹配的费率记录，使用默认值
        if not matching_rates:
            self.logger().debug(
                f"未找到时间范围内的资金费率记录，使用默认值 0"
            )
            funding_rate = Decimal("0")
        else:
            # 按时间降序排序，取最新的（上一个）funding_rate
            latest_rate_record = sorted(
                matching_rates,
                key=lambda x: x.get("time", 0),
                reverse=True
            )[0]
            funding_rate = Decimal(str(latest_rate_record.get("funding_rate", "0")))
        
        return payment_timestamp, funding_rate, payment

    
    def _last_funding_time(self, trading_pair: str) -> int:
        """
        计算指定交易对的上次资金费率结算时间戳（毫秒）
        
        不同交易对的结算周期不同：
        - ETH-USDC: 每 8 小时结算一次
        - BTC-USDC: 每 1 小时结算一次
        - SOL-USDC: 每 4 小时结算一次
        """
        # 从配置中获取该交易对的资金费率结算周期（小时）
        funding_interval_hours = self._funding_interval_hours.get(trading_pair, 8)
        
        # 计算当前时间所在周期的开始时间（即上次结算时间）
        # 例如：当前时间 10:30，结算周期 8 小时
        #      当前周期开始时间 = 8:00（这就是上次结算时间）
        current_time = time.time()
        interval_seconds = funding_interval_hours * 3600
        
        # 找到当前周期的开始时间
        # (current_time // interval_seconds) 得到当前是第几个周期
        # * interval_seconds 得到当前周期的开始时间
        last_funding_time_sec = int((current_time // interval_seconds) * interval_seconds)
        
        # 转为毫秒时间戳
        return int(last_funding_time_sec * 1000)