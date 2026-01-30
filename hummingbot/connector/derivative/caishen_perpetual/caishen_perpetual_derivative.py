# your_dex_perpetual_derivative.py

import asyncio
import decimal
import base64
import json
import time
from decimal import Decimal
from typing import Any, AsyncIterable, Dict, List, Optional, Tuple

from bidict import bidict
from eth_utils import to_bytes

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
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather
from hummingbot.connector.derivative.caishen_perpetual.deepproto.schema import tx_pb2


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
            self.logger().error(
                f"Base token ID not found for trading pair: {trading_pair}. "
                f"Available trading pairs: {list(self._base_token_ids.keys())}. "
                f"Total loaded pairs: {len(self._base_token_ids)}"
            )
            raise KeyError(f"Base token ID not found for trading pair: {trading_pair}")
        token_id = self._base_token_ids[trading_pair]
        self.logger().debug(f"Found base_token_id={token_id} for trading_pair={trading_pair}")
        return token_id
    
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
            self.logger().error(
                f"Quote token ID not found for trading pair: {trading_pair}. "
                f"Available trading pairs: {list(self._quote_token_ids.keys())}. "
                f"Total loaded pairs: {len(self._quote_token_ids)}"
            )
            raise KeyError(f"Quote token ID not found for trading pair: {trading_pair}")
        token_id = self._quote_token_ids[trading_pair]
        self.logger().debug(f"Found quote_token_id={token_id} for trading_pair={trading_pair}")
        return token_id
    
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
        """
        检查撤单异常是否表示订单不存在（错误码1139）
        """
        if isinstance(cancelation_exception, IOError):
            error_str = str(cancelation_exception)
            # 检查错误码1139（ORDER_NOT_FOUND）
            if "1139" in error_str or "撤单失败: 1139" in error_str:
                return True
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
        """
        重写 _api_request 方法，使用 web_utils.get_rest_api_limit_id_for_endpoint 获取正确的 limit_id
        这符合其他 connector 的模式（如 bybit_perpetual, okx_perpetual）
        """
        # 如果没有提供 limit_id，使用 web_utils 函数获取
        if limit_id is None:
            limit_id = web_utils.get_rest_api_limit_id_for_endpoint(
                endpoint=path_url,
                trading_pair=trading_pair,
            )
        
        # 调用父类方法
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

    async def _update_trading_rules(self):
        self.logger().debug(f"开始更新交易规则，请求路径: {self.trading_rules_request_path}")
        exchange_info = await self._api_get(path_url=self.trading_rules_request_path)
        self.logger().debug(f"获取到交易规则响应: code={exchange_info.get('code')}, symbols数量={len(exchange_info.get('data', {}).get('symbols', []))}")
        trading_rules_list = await self._format_trading_rules(exchange_info)
        self._trading_rules.clear()
        for trading_rule in trading_rules_list:
            self._trading_rules[trading_rule.trading_pair] = trading_rule
        self.logger().debug(f"已加载 {len(self._trading_rules)} 个交易规则")
        self._initialize_trading_pair_symbols_from_exchange_info(exchange_info=exchange_info)
        self.logger().debug(f"交易规则更新完成，已加载 {len(self._base_token_ids)} 个交易对的 token IDs")

    # 初始化获取交易对信息
    async def _initialize_trading_pair_symbol_map(self):
        try:
            self.logger().debug(f"开始初始化交易对符号映射，请求路径: {CONSTANTS.EXCHANGE_INFO_URL}")
            exchange_info = await self._api_get(path_url=CONSTANTS.EXCHANGE_INFO_URL)
            self.logger().debug(f"获取到交易对信息响应: code={exchange_info.get('code')}, symbols数量={len(exchange_info.get('data', {}).get('symbols', []))}")
            self._initialize_trading_pair_symbols_from_exchange_info(exchange_info=exchange_info)
            self.logger().debug(f"交易对符号映射初始化完成，已加载 {len(self._base_token_ids)} 个交易对")
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
        self.logger().warning(f"开始撤单 - Order ID: {order_id}, Exchange Order ID: {tracked_order.exchange_order_id}")
        # 获取 exchange_order_id（整数类型），caishen 撤单 API 需要使用 exchange_order_id 而不是 client_order_id
        exchange_order_id = tracked_order.exchange_order_id
        if not exchange_order_id:
            # 如果 exchange_order_id 还没有设置，尝试等待获取
            try:
                exchange_order_id = await tracked_order.get_exchange_order_id()
            except Exception as e:
                self.logger().error(f"无法获取订单 {order_id} 的 exchange_order_id: {e}")
                raise IOError(f"无法获取订单 {order_id} 的 exchange_order_id，无法撤单")
        
        # 将 exchange_order_id 转换为整数（caishen API 要求整数类型）
        try:
            order_id_int = int(exchange_order_id)
        except (ValueError, TypeError) as e:
            self.logger().error(f"订单 {order_id} 的 exchange_order_id '{exchange_order_id}' 无法转换为整数: {e}")
            raise IOError(f"订单 {order_id} 的 exchange_order_id '{exchange_order_id}' 无效，无法撤单")
        
        form_data = {
            "order_id": order_id_int,  # 使用整数类型的 exchange_order_id
        }

        block_hash_bytes = await self.get_latest()
        encoded_message = await self.authenticator.make_tx(self.api_key,block_hash_bytes, action_type, form_data)
        cancel_result = await self.submit_tx(encoded_message)
        
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
            "raw_tx_result": "string"
        }
        """
        
        try:
            # 首先检查顶层 code 字段
            response_code = cancel_result.get("code", -1)
            response_msg = cancel_result.get("msg", "")
            
            # 情况1: API 返回错误（code != 0）
            if response_code != 0:
                msg = f"撤单失败: code={response_code}, msg={response_msg}"
                self.logger().warning(f"撤单失败 - Order ID: {order_id}, {msg}")
                
                # 如果错误码表示订单不存在（如 1139），调用 process_order_not_found
                # 参考 hyperliquid 的处理方式
                if response_code == 1139 or "not found" in response_msg.lower() or "does not exist" in response_msg.lower():
                    self.logger().debug(f"订单 {order_id} 不存在（错误码: {response_code}），无需撤单")
                    await self._order_tracker.process_order_not_found(order_id)
                    # 对于1139错误码，不抛出异常，直接返回True，避免记录error日志
                    return True
                
                return False
            
            # 情况2: API 返回成功（code == 0），解析 data.result
            data = cancel_result.get("data", {})
            result = data.get("result", {})
            
            # 正常情况下 error 和 block_result 只有一个有值
            error_info = result.get("error")
            block_result = result.get("block_result")
            
            # 情况1: 有错误（error 有值，block_result 为空）
            if error_info and not block_result:
                error_code = error_info.get("code", "")
                error_message = error_info.get("message", "")
                
                self.logger().warning(
                    f"撤单失败 - Order ID: {order_id}, "
                    f"Error Code: {error_code}, Message: {error_message}"
                )
                
                # 如果是订单不存在的错误（错误码 1139 或错误消息包含 not found/does not exist），标记订单为未找到
                # 参考 hyperliquid 的处理方式
                if (error_code == 1139 or 
                    "not found" in str(error_message).lower() or 
                    "does not exist" in str(error_message).lower()):
                    self.logger().debug(f"订单 {order_id} 不存在（错误码: {error_code}），无需撤单")
                    await self._order_tracker.process_order_not_found(order_id)
                    # 对于1139错误码，不抛出异常，直接返回True，避免记录error日志
                    return True
                
                raise IOError(f"撤单失败: {error_code} - {error_message}")
            
            # 情况2: 成功（block_result 有值，error 为空）
            if block_result and not error_info:
                block_number = block_result.get("number", "0")
                try:
                    block_num = int(block_number)
                    if block_num > 0:
                        self.logger().debug(
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
            order_type_int = 0  # 默认值
        
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

        block_hash_bytes = await self.get_latest()
        encoded_message = await self.authenticator.make_tx(self.api_key,block_hash_bytes, action_type, form_data)
        order_result = await self.submit_tx(encoded_message)
        
        # 打印 API 返回结果
        self.logger().debug(f"下单 API 响应 - Order ID: {order_id}, Response: {order_result}")

        # Safe response parsing with proper error handling
        try:
            # 首先检查顶层 code 字段
            response_code = order_result.get("code", -1)
            response_msg = order_result.get("msg", "")
            
            # 情况1: API 返回错误（code != 0）
            if response_code != 0:
                error_msg = f"code={response_code}, msg={response_msg}"
                self.logger().error(f"Error submitting order {order_id}: {error_msg}")
                raise IOError(f"Error submitting order {order_id}: {error_msg}")
            
            # 情况2: API 返回成功（code == 0），解析 data.raw 中的 protobuf 数据
            data = order_result.get("data", {})
            if not isinstance(data, dict):
                raise IOError(f"Error submitting order {order_id}: Invalid data format")
            
            # 检查 data.result.error 中的错误（在解析 protobuf 之前）
            result = data.get("result", {})
            if isinstance(result, dict):
                error_info = result.get("error")
                if isinstance(error_info, dict):
                    error_code = error_info.get("code")
                    error_code_text = error_info.get("code_text", "")
                    
                    # 处理1114-NO_POSITION_TO_REDUCE错误：仓位已经完全平仓，移除追踪的仓位
                    if error_code == 1114 or error_code_text == "NO_POSITION_TO_REDUCE":
                        self.logger().warning(
                            f"下单失败 - Order ID: {order_id}, "
                            f"Error Code: {error_code} ({error_code_text}), "
                            f"仓位已经完全平仓，移除追踪的仓位"
                        )
                        
                        # 如果是平仓操作，移除对应的仓位
                        if position_action == PositionAction.CLOSE:
                            # 根据 trade_type 确定仓位方向
                            # BUY 平 SHORT 仓位，SELL 平 LONG 仓位
                            if trade_type == TradeType.BUY:
                                position_side = PositionSide.SHORT
                            else:  # TradeType.SELL
                                position_side = PositionSide.LONG
                            
                            pos_key = self._perpetual_trading.position_key(trading_pair, position_side)
                            removed_position = self._perpetual_trading.remove_position(pos_key)
                            if removed_position:
                                self.logger().debug(
                                    f"已移除追踪的仓位 - Trading Pair: {trading_pair}, "
                                    f"Position Side: {position_side}, Position Key: {pos_key}"
                                )
                            else:
                                self.logger().debug(
                                    f"未找到要移除的仓位 - Trading Pair: {trading_pair}, "
                                    f"Position Side: {position_side}, Position Key: {pos_key}"
                                )
                        
                        error_message = error_info.get("message", error_code_text)
                        error_msg = f"{error_code} - {error_message}" if error_code else error_message
                        raise IOError(f"Error submitting order {order_id}: {error_msg}")
            
            raw_data_base64 = data.get("raw_tx_result", "")
            if not raw_data_base64:
                raise IOError(f"Error submitting order {order_id}: No raw data in response")
            
            # 解码 base64 并解析 protobuf
            try:
                raw_data_bytes = base64.b64decode(raw_data_base64)
                tx_result = tx_pb2.TxResult()
                tx_result.ParseFromString(raw_data_bytes)
            except Exception as e:
                raise IOError(f"Error submitting order {order_id}: Failed to parse protobuf raw data - {e}")
            
            # 检查是否有错误
            if tx_result.HasField("error"):
                error = tx_result.error
                try:
                    error_code = error.code if error.HasField("code") else None
                    error_message = error.message if error.HasField("message") else ""
                except ValueError:
                    # 如果 HasField 检查失败（字段没有 presence），尝试直接访问
                    error_code = getattr(error, "code", None)
                    error_message = getattr(error, "message", "")
                
                # 处理1114-NO_POSITION_TO_REDUCE错误：仓位已经完全平仓，移除追踪的仓位
                if error_code == 1114:
                    self.logger().warning(
                        f"下单失败 - Order ID: {order_id}, "
                        f"Error Code: {error_code} (NO_POSITION_TO_REDUCE), "
                        f"仓位已经完全平仓，移除追踪的仓位"
                    )
                    
                    # 如果是平仓操作，移除对应的仓位
                    if position_action == PositionAction.CLOSE:
                        # 根据 trade_type 确定仓位方向
                        # BUY 平 SHORT 仓位，SELL 平 LONG 仓位
                        if trade_type == TradeType.BUY:
                            position_side = PositionSide.SHORT
                        else:  # TradeType.SELL
                            position_side = PositionSide.LONG
                        
                        pos_key = self._perpetual_trading.position_key(trading_pair, position_side)
                        removed_position = self._perpetual_trading.remove_position(pos_key)
                        if removed_position:
                            self.logger().debug(
                                f"已移除追踪的仓位 - Trading Pair: {trading_pair}, "
                                f"Position Side: {position_side}, Position Key: {pos_key}"
                            )
                        else:
                            self.logger().debug(
                                f"未找到要移除的仓位 - Trading Pair: {trading_pair}, "
                                f"Position Side: {position_side}, Position Key: {pos_key}"
                            )
                
                error_msg = f"{error_code} - {error_message}" if error_code else error_message
                self.logger().error(f"Error submitting order {order_id}: {error_msg}")
                raise IOError(f"Error submitting order {order_id}: {error_msg}")
            
            # 检查是否有 PlaceOrderResult
            # 尝试使用 HasField 检查，如果字段不存在则使用 hasattr 作为后备
            has_place_order_result = False
            place_order_result = None
            
            try:
                if tx_result.HasField("place_order_result"):
                    has_place_order_result = True
                    place_order_result = tx_result.place_order_result
            except (ValueError, AttributeError):
                # 字段可能不存在，尝试直接访问属性
                try:
                    if hasattr(tx_result, "place_order_result"):
                        place_order_result = tx_result.place_order_result
                        if place_order_result is not None:
                            has_place_order_result = True
                except Exception:
                    pass
            
            if has_place_order_result and place_order_result is not None:
                # 提取订单ID
                order_id_from_result = None
                try:
                    order_id_from_result = place_order_result.id if hasattr(place_order_result, "id") else None
                except Exception as e:
                    self.logger().warning(f"提取 PlaceOrderResult.id 时出错: {e}")
                
                if not order_id_from_result:
                    raise IOError(f"Error submitting order {order_id}: No order ID in PlaceOrderResult")
                
                # 提取其他订单信息用于日志
                try:
                    size = place_order_result.size if hasattr(place_order_result, "size") else None
                    filled = place_order_result.filled if hasattr(place_order_result, "filled") else None
                    price = place_order_result.price if hasattr(place_order_result, "price") else None
                    filled_price = place_order_result.filled_price if hasattr(place_order_result, "filled_price") else None
                    client_order_id = place_order_result.client_order_id if hasattr(place_order_result, "client_order_id") else None
                    status = place_order_result.status if hasattr(place_order_result, "status") else None
                    
                    self.logger().debug(
                        f"下单成功 - Order ID: {order_id}, "
                        f"Result Order ID: {order_id_from_result}, "
                        f"Size: {size}, Filled: {filled}, "
                        f"Price: {price}, Filled Price: {filled_price}, "
                        f"Client Order ID: {client_order_id}, Status: {status}"
                    )
                except Exception as e:
                    self.logger().warning(f"提取 PlaceOrderResult 字段时出错: {e}")
                    self.logger().debug(f"下单成功 - Order ID: {order_id}, Result Order ID: {order_id_from_result}")
                
                # 返回订单ID和时间戳（参考 hyperliquid 的实现）
                return (str(order_id_from_result), time.time())
            
            # 检查是否有 block_result（兼容旧格式）
            if tx_result.HasField("block_result"):
                block_result = tx_result.block_result
                block_number = block_result.number if block_result.HasField("number") else 0
                
                if block_number > 0:
                    self.logger().debug(
                        f"下单成功 - Order ID: {order_id}, Block Number: {block_number}"
                    )
                    # 如果没有 place_order_result，使用传入的 order_id 作为返回值
                    return (order_id, time.time())
                else:
                    raise IOError(f"Error submitting order {order_id}: Invalid block number: {block_number}")
            
            # 异常情况：既没有 place_order_result 也没有 block_result 和 error
            raise IOError(f"Error submitting order {order_id}: TxResult has no place_order_result, block_result, or error")
                
        except IOError:
            # 重新抛出 IOError（下单失败的错误）
            raise
        except (KeyError, TypeError, AttributeError) as e:
            error_msg = f"Failed to parse response - {e}"
            self.logger().error(f"Error parsing order result for {order_id}: {error_msg}, result: {order_result}")
            raise IOError(f"Error submitting order {order_id}: {error_msg}")
        except Exception as e:
            error_msg = f"Unexpected error - {e}"
            self.logger().error(f"Error submitting order {order_id}: {error_msg}, result: {order_result}")
            raise IOError(f"Error submitting order {order_id}: {error_msg}")
    
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
            # 调用 REST API 获取交易历史（symbol 使用交易所格式）
            hb_trading_pair = self.trading_pairs[0]
            exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=hb_trading_pair)
            to_ts_ms = int(time.time() * 1000)
            from_ts_ms = to_ts_ms - 24 * 3600 * 1000
            response = await self._api_get(
                path_url=CONSTANTS.ACCOUNT_TRADE_LIST_URL,
                params={
                    "account": self.api_key,
                    "symbol": exchange_symbol,
                    "cursor": "",  # 分页游标，传空字符串
                    "limit": 100,
                    "from": from_ts_ms,
                    "to": to_ts_ms,
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
            self.logger().debug(
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
        """
        WebSocket 暂时未实现，此方法为空实现。
        如果 _user_stream_tracker 为 None（WebSocket 被禁用），直接返回，不 yield 任何值。
        """
        # WebSocket 被禁用时，_user_stream_tracker 为 None
        if self._user_stream_tracker is None:
            # 直接返回，结束 generator（不 yield 任何值）
            # 由于 _user_stream_event_listener 已经被重写为空实现，这个方法理论上不会被调用
            # 但为了安全起见，我们在这里处理 None 的情况
            return
        
        # 如果 WebSocket 已启用，使用正常的实现
        while True:
            try:
                yield await self._user_stream_tracker.user_stream.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    "Unknown error. Retrying after 1 seconds.",
                    exc_info=True,
                    app_warning_msg="Could not fetch user events from Caishen Perpetual. Check API key and network connection.",
                )
                await self._sleep(1.0)

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
        self.logger().debug(f"开始初始化交易对符号映射，共 {len(symbols)} 个交易对")
        
        loaded_count = 0
        skipped_count = 0
        error_count = 0
        
        # 遍历每个交易对信息并建立映射
        for symbol_info in symbols:
            try:
                exchange_symbol = symbol_info.get("symbol", "UNKNOWN")
                status = symbol_info.get("status")
                
                # 检查交易对状态是否可用 (status=1 表示可用)
                if status != 1:
                    self.logger().debug(f"跳过不可用的交易对: {exchange_symbol}, status={status}")
                    skipped_count += 1
                    continue
                
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
                else:
                    self.logger().warning(f"交易对 {trading_pair} ({exchange_symbol}) 的 base_token_id 为 None")
                    
                if quote_token_id is not None:
                    self._quote_token_ids[trading_pair] = int(quote_token_id)
                else:
                    self.logger().warning(f"交易对 {trading_pair} ({exchange_symbol}) 的 quote_token_id 为 None")
                
                self.logger().debug(
                    f"已加载交易对: {trading_pair} ({exchange_symbol}), "
                    f"base_token_id={base_token_id}, quote_token_id={quote_token_id}, "
                    f"funding_interval={funding_interval_hours}h"
                )
                loaded_count += 1
                
            except Exception as exception:
                error_count += 1
                self.logger().error(
                    f"解析交易对信息时出错 ({exception}). Symbol: {symbol_info}",
                    exc_info=True
                )
        
        self._set_trading_pair_symbol_map(mapping)
        self.logger().debug(
            f"交易对符号映射初始化完成: 成功加载 {loaded_count} 个, "
            f"跳过 {skipped_count} 个, 错误 {error_count} 个. "
            f"已存储的 token IDs: base_token_ids={self._base_token_ids}, "
            f"quote_token_ids={self._quote_token_ids}"
        )


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
        try:
            wallet_str = str(balance_info.get("wallet", "0") or "0")
            total_balance = Decimal(wallet_str)
            if total_balance.is_nan() or total_balance.is_infinite():
                total_balance = Decimal("0")
        except (ValueError, TypeError, decimal.InvalidOperation):
            self.logger().warning(f"无法解析钱包余额: {balance_info.get('wallet')}, 使用 0")
            total_balance = Decimal("0")
        
        # 可用余额 = 钱包余额 - 所有冻结资金
        try:
            isolated_position_frozen = Decimal(str(balance_info.get("isolated_position_frozen", "0") or "0"))
            if isolated_position_frozen.is_nan() or isolated_position_frozen.is_infinite():
                isolated_position_frozen = Decimal("0")
        except (ValueError, TypeError, decimal.InvalidOperation):
            isolated_position_frozen = Decimal("0")
        
        try:
            isolated_order_frozen = Decimal(str(balance_info.get("isolated_order_frozen", "0") or "0"))
            if isolated_order_frozen.is_nan() or isolated_order_frozen.is_infinite():
                isolated_order_frozen = Decimal("0")
        except (ValueError, TypeError, decimal.InvalidOperation):
            isolated_order_frozen = Decimal("0")
        
        try:
            cross_order_frozen = Decimal(str(balance_info.get("cross_order_frozen", "0") or "0"))
            if cross_order_frozen.is_nan() or cross_order_frozen.is_infinite():
                cross_order_frozen = Decimal("0")
        except (ValueError, TypeError, decimal.InvalidOperation):
            cross_order_frozen = Decimal("0")
        
        # 计算可用余额，确保不会是负数或 NaN
        available_balance = total_balance - isolated_position_frozen - isolated_order_frozen - cross_order_frozen
        if available_balance < Decimal("0"):
            available_balance = Decimal("0")
        if available_balance.is_nan() or available_balance.is_infinite():
            available_balance = Decimal("0")
        
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
            unrealized_pnl = Decimal(position.get("unrealized_pnl")) 
            entry_price = Decimal(position.get("entry_price"))
            size = Decimal(position.get("size", 0))
            # 根据持仓方向设置 amount 的正负号：LONG 为正数，SHORT 为负数
            # 参考 okx_perpetual 的处理方式
            amount = size * (Decimal("-1.0") if position_side == PositionSide.SHORT else Decimal("1.0"))
            leverage = Decimal(position.get("leverage")) 
            pos_key = self._perpetual_trading.position_key(hb_trading_pair, position_side)
            if abs(amount) > 0:
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
        self.logger().debug(f"开始设置杠杆: trading_pair={trading_pair}, leverage={leverage}")
        self.logger().debug(
            f"当前已加载的交易对数量: base_token_ids={len(self._base_token_ids)}, "
            f"quote_token_ids={len(self._quote_token_ids)}, "
            f"已加载的交易对: {list(self._base_token_ids.keys())}"
        )
        
        # 获取 base_token 和 quote_token ID（整数）
        try:
            base_token_id = self.get_base_token_id(trading_pair)
            quote_token_id = self.get_quote_token_id(trading_pair)
            self.logger().debug(f"获取到 token IDs: base_token_id={base_token_id}, quote_token_id={quote_token_id}")
        except KeyError as e:
            self.logger().error(
                f"设置杠杆失败: 无法获取交易对的 token IDs. "
                f"这可能是因为交易对信息尚未加载完成。错误: {e}"
            )
            return False, str(e)
        
        action_type = "SET_POSITION_LEVERAGE"
        form_data = {
            "base_token": base_token_id,
            "quote_token": quote_token_id,
            "leverage": leverage,
        }
        
        try:
            block_hash_bytes = await self.get_latest()
            encoded_message = await self.authenticator.make_tx(self.api_key,block_hash_bytes, action_type, form_data)
            set_result = await self.submit_tx(encoded_message)
            self.logger().debug(f"设置杠杆 API 响应: {set_result}")
            
            # 解析 API 响应结构: {'code': 0, 'msg': '', 'data': {'result': {'block_result': {...}}}}
            # 首先检查顶层 code 字段
            response_code = set_result.get("code", -1)
            response_msg = set_result.get("msg", "")
            
            # 情况1: API 返回错误（code != 0）
            if response_code != 0:
                msg = f"设置杠杆失败: code={response_code}, msg={response_msg}"
                self.logger().warning(f"设置杠杆失败 - Trading Pair: {trading_pair}, Leverage: {leverage}, {msg}")
                return False, msg
            
            # 情况2: API 返回成功（code == 0），解析 data.result
            data = set_result.get("data", {})
            result = data.get("result", {})
            
            # 检查是否有 error
            error_info = result.get("error")
            block_result = result.get("block_result")
            
            # 情况2.1: 有错误信息
            if error_info:
                error_code = error_info.get("code", "")
                error_message = error_info.get("message", "")
                msg = f"设置杠杆失败: {error_code} - {error_message}"
                self.logger().warning(f"设置杠杆失败 - Trading Pair: {trading_pair}, Leverage: {leverage}, Error: {msg}")
                return False, msg
            
            # 情况2.2: 成功，有 block_result
            if block_result:
                block_number = block_result.get("number", "0")
                try:
                    block_num = int(block_number)
                    if block_num > 0:
                        self.logger().debug(
                            f"设置杠杆成功 - Trading Pair: {trading_pair}, Leverage: {leverage}, Block Number: {block_number}"
                        )
                        return True, ""
                    else:
                        msg = f"设置杠杆返回无效的区块号: {block_number}"
                        self.logger().warning(f"设置杠杆失败 - Trading Pair: {trading_pair}, Leverage: {leverage}, {msg}")
                        return False, msg
                except (ValueError, TypeError):
                    msg = f"无法解析区块号: {block_number}"
                    self.logger().error(f"设置杠杆失败 - Trading Pair: {trading_pair}, Leverage: {leverage}, {msg}")
                    return False, msg
            
            # 情况2.3: 异常情况（既没有 error 也没有 block_result）
            msg = f"设置杠杆结果异常: code={response_code}, data.result 中既没有 error 也没有 block_result, result={result}"
            self.logger().error(f"设置杠杆失败 - Trading Pair: {trading_pair}, Leverage: {leverage}, {msg}")
            return False, msg
            
        except json.JSONDecodeError as e:
            msg = f"设置杠杆结果 JSON 解析失败: {e}"
            self.logger().error(f"设置杠杆失败 - Trading Pair: {trading_pair}, Leverage: {leverage}, {msg}")
            return False, msg
            
        except (KeyError, TypeError) as e:
            msg = f"设置杠杆结果格式错误: {e}"
            self.logger().error(f"设置杠杆失败 - Trading Pair: {trading_pair}, Leverage: {leverage}, {msg}")
            return False, msg
        

    
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

    async def submit_tx(self,message_bytes,print_flag=True):
        # 将 bytes 转换为 base64
        message_base64 = base64.b64encode(message_bytes).decode("utf-8")
        payload={
            "message":message_base64,
            "async":False,
            "include_raw_tx_result":True
        }

        result = await self._api_post(
            path_url = CONSTANTS.SUBMIT_TX_URL,
            data=payload,
            is_auth_required=False)
            
        return result

    async def get_latest(self,print_flag=True):
        '''
        获取最新的区块
        :param print_flag:
        :return:
        '''
        result = await self._api_get(
            path_url = CONSTANTS.GET_LATEST_BLOCK_URL,
            is_auth_required=False)
        block_hash = result['data']['block']['hash']
        block_hash_bytes = to_bytes(hexstr=block_hash)
        return block_hash_bytes
    
    def parse_tx_result_raw_data(self, raw_data_base64: str) -> Dict[str, Any]:
        """
        解析 TxResult 的 raw 数据（base64 编码的 protobuf 数据）
        
        Args:
            raw_data_base64: base64 编码的 protobuf 原始数据
            
        Returns:
            包含解析结果的字典，格式：
            {
                "has_error": bool,
                "error": {...} or None,
                "has_block_result": bool,
                "block_result": {...} or None,
                "has_place_order_result": bool,
                "place_order_result": {...} or None,
                "which_extra": str or None,  # extra oneof 中的字段名
                "order_id": int or None,  # 从 place_order_result.id 提取的订单ID
            }
        """
        result = {
            "has_error": False,
            "error": None,
            "has_block_result": False,
            "block_result": None,
            "has_place_order_result": False,
            "place_order_result": None,
            "which_extra": None,
            "order_id": None,
        }
        
        try:
            # 解码 base64
            raw_data_bytes = base64.b64decode(raw_data_base64)
            
            # 解析 protobuf
            tx_result = tx_pb2.TxResult()
            tx_result.ParseFromString(raw_data_bytes)
            
            # 检查 error (result oneof)
            try:
                if tx_result.HasField("error"):
                    result["has_error"] = True
                    error = tx_result.error
                    error_code = getattr(error, "code", "")
                    error_message = getattr(error, "message", "")
                    result["error"] = {
                        "code": error_code,
                        "message": error_message
                    }
            except (ValueError, AttributeError):
                pass
            
            # 检查 block_result (result oneof)
            try:
                if tx_result.HasField("block_result"):
                    result["has_block_result"] = True
                    block_result = tx_result.block_result
                    block_number = getattr(block_result, "number", 0)
                    result["block_result"] = {
                        "number": block_number
                    }
            except (ValueError, AttributeError):
                pass
            
            # 检查 extra oneof 中的字段
            try:
                which_extra = tx_result.WhichOneof("extra")
                result["which_extra"] = which_extra
                
                if which_extra == "place_order_result":
                    result["has_place_order_result"] = True
                    place_order_result = tx_result.place_order_result
                    
                    # 提取订单ID
                    order_id = getattr(place_order_result, "id", None)
                    result["order_id"] = order_id if order_id and order_id > 0 else None
                    
                    # 提取其他字段
                    size = getattr(place_order_result, "size", None)
                    filled = getattr(place_order_result, "filled", None)
                    price = getattr(place_order_result, "price", None)
                    filled_price = getattr(place_order_result, "filled_price", None)
                    client_order_id = getattr(place_order_result, "client_order_id", None)
                    status = getattr(place_order_result, "status", None)
                    
                    result["place_order_result"] = {
                        "id": result["order_id"],
                        "size": size,
                        "filled": filled,
                        "price": price,
                        "filled_price": filled_price,
                        "client_order_id": client_order_id,
                        "status": status,
                    }
                elif which_extra == "cancel_liquidation_orders_result":
                    cancel_result = tx_result.cancel_liquidation_orders_result
                    ids = list(getattr(cancel_result, "ids", []))
                    exited_liquidation_mode = getattr(cancel_result, "exited_liquidation_mode", False)
                    result["cancel_liquidation_orders_result"] = {
                        "ids": ids,
                        "exited_liquidation_mode": exited_liquidation_mode
                    }
            except (ValueError, AttributeError):
                pass
        
        except Exception as e:
            self.logger().error(f"解析 raw 数据时出错: {e}")
            raise
        
        return result
    
    def test_parse_tx_result_raw_data(self):
        """
        测试解析 raw 数据的方法
        
        使用日志中的实际数据进行测试
        """
        # 测试数据: 设置杠杆的响应（只有 block_result）
        test_raw = "CiBf4waO/cqUYd0WQujtWCbOQV94L73Ote5ybHymZorDDxoFCIf4igI="
        
        print("\n" + "=" * 60)
        print("测试解析 TxResult raw 数据")
        print("=" * 60)
        print(f"\n测试数据 (base64): {test_raw}")
        
        try:
            result = self.parse_tx_result_raw_data(test_raw)
            
            print("\n解析结果:")
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            
            # 验证解析结果
            print("\n验证结果:")
            assert result["has_block_result"] == True, "应该有 block_result"
            assert result["block_result"] is not None, "block_result 不应该为 None"
            assert result["block_result"]["number"] > 0, "block_number 应该大于 0"
            print(f"✅ block_result 解析正确: block_number = {result['block_result']['number']}")
            
            assert result["has_error"] == False, "不应该有 error"
            print("✅ 没有 error")
            
            if result["has_place_order_result"]:
                assert result["order_id"] is not None, "如果有 place_order_result，应该有 order_id"
                print(f"✅ place_order_result 解析正确: order_id = {result['order_id']}")
            else:
                print("ℹ️  没有 place_order_result（这是正常的，因为这是设置杠杆的响应）")
            
            print("\n" + "=" * 60)
            print("✅ 所有测试通过！")
            print("=" * 60)
            return True
            
        except AssertionError as e:
            print(f"\n❌ 测试失败: {e}")
            return False
        except Exception as e:
            print(f"\n❌ 测试过程中发生错误: {e}")
            import traceback
            traceback.print_exc()
            return False