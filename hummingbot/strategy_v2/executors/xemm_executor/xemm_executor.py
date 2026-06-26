import asyncio
import logging
from decimal import Decimal
from typing import Dict

from hummingbot.connector.connector_base import ConnectorBase, Union
from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)
from hummingbot.core.rate_oracle.rate_oracle import RateOracle
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.executors.xemm_executor.data_types import XEMMExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder
from hummingbot.strategy_v2.utils.xemm_sizing_price import resolve_xemm_sizing_price


class XEMMExecutor(ExecutorBase):
    _logger = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    @staticmethod
    def _are_tokens_interchangeable(first_token: str, second_token: str):
        interchangeable_tokens = [
            {"WETH", "ETH"},
            {"WBTC", "BTC"},
            {"WBNB", "BNB"},
            {"WPOL", "POL"},
            {"WAVAX", "AVAX"},
            {"WONE", "ONE"},
            {"USDC", "USDC.E"},
            {"WBTC", "BTC"},
            {"USOL", "SOL"},
            {"UETH", "ETH"},
            {"UBTC", "BTC"}
        ]
        same_token_condition = first_token == second_token
        tokens_interchangeable_condition = any(({first_token, second_token} <= interchangeable_pair
                                                for interchangeable_pair
                                                in interchangeable_tokens))
        # for now, we will consider all the stablecoins interchangeable
        stable_coins_condition = "USD" in first_token and "USD" in second_token
        return same_token_condition or tokens_interchangeable_condition or stable_coins_condition

    def is_arbitrage_valid(self, pair1, pair2):
        base_asset1, _ = split_hb_trading_pair(pair1)
        base_asset2, _ = split_hb_trading_pair(pair2)
        return self._are_tokens_interchangeable(base_asset1, base_asset2)

    def __init__(self, strategy: StrategyV2Base, config: XEMMExecutorConfig, update_interval: float = 1.0,
                 max_retries: int = 10):
        if not self.is_arbitrage_valid(pair1=config.buying_market.trading_pair,
                                       pair2=config.selling_market.trading_pair):
            raise Exception("XEMM is not valid since the trading pairs are not interchangeable.")
        self.config = config
        self.rate_oracle = RateOracle.get_instance()
        if config.maker_side == TradeType.BUY:
            self.maker_connector = config.buying_market.connector_name
            self.maker_trading_pair = config.buying_market.trading_pair
            self.maker_order_side = TradeType.BUY
            self.taker_connector = config.selling_market.connector_name
            self.taker_trading_pair = config.selling_market.trading_pair
            self.taker_order_side = TradeType.SELL
        else:
            self.maker_connector = config.selling_market.connector_name
            self.maker_trading_pair = config.selling_market.trading_pair
            self.maker_order_side = TradeType.SELL
            self.taker_connector = config.buying_market.connector_name
            self.taker_trading_pair = config.buying_market.trading_pair
            self.taker_order_side = TradeType.BUY

        # Set up quote conversion pair
        _, maker_quote = split_hb_trading_pair(self.maker_trading_pair)
        _, taker_quote = split_hb_trading_pair(self.taker_trading_pair)
        self.quote_conversion_pair = f"{taker_quote}-{maker_quote}"

        taker_connector = strategy.connectors[self.taker_connector]
        if not self.is_amm_connector(exchange=self.taker_connector):
            if OrderType.MARKET not in taker_connector.supported_order_types():
                raise ValueError(f"{self.taker_connector} does not support market orders.")
        self._taker_result_price = Decimal("1")
        self._maker_target_price = Decimal("1")
        self._tx_cost = Decimal("1")
        self._tx_cost_pct = Decimal("1")
        self._current_trade_profitability = Decimal("0")
        self.maker_order = None
        self.taker_order = None
        self.taker_orders = []
        # 保留所有曾创建的 maker TrackedOrder（按 order_id），即使撤单清空了 self.maker_order
        # 引用，迟到的成交仍能刷新 tracked 并正确计入 PnL/fee。
        self._maker_orders_by_id = {}
        self._taker_order_ids = set()
        self._failed_taker_ids = set()
        self._seen_trade_ids = set()
        # maker 成交量取两路来源的 max，避免 completed 与 fill 事件重复累加：
        #   _maker_fills_sum   —— OrderFilledEvent 按 trade_id 去重后的累计成交
        #   _maker_filled_floor —— completed 事件 / tracked 累计成交给出的总量下限
        self._maker_fills_sum = Decimal("0")
        self._maker_filled_floor = Decimal("0")
        self._maker_filled_base = Decimal("0")
        # 已『提交』的对冲量（下单去重用），区别于『实际成交』的对冲量
        self._submitted_hedge_base = Decimal("0")
        self._taker_amounts = {}
        self._hedging = False
        self.failed_orders = []
        super().__init__(strategy=strategy,
                         connectors=[config.buying_market.connector_name, config.selling_market.connector_name],
                         config=config, update_interval=update_interval, max_retries=max_retries)

    def _connector_get_price_by_type(self, connector_name: str, trading_pair: str, price_type: PriceType):
        return self.connectors[connector_name].get_price_by_type(trading_pair, price_type)

    async def validate_sufficient_balance(self):
        sizing_price, sizing_source = resolve_xemm_sizing_price(
            get_price_by_type=self._connector_get_price_by_type,
            maker_connector=self.maker_connector,
            maker_trading_pair=self.maker_trading_pair,
            taker_connector=self.taker_connector,
            taker_trading_pair=self.taker_trading_pair,
            require_maker_order_book=self.config.require_maker_order_book,
        )
        if sizing_price is None:
            mode = "maker order book" if self.config.require_maker_order_book else "taker reference price"
            self.logger().error(
                f"No {mode} for balance check on {self.maker_trading_pair} / "
                f"{self.taker_trading_pair}; stopping executor."
            )
            self.close_type = CloseType.FAILED
            self.stop()
            return
        if not self.config.require_maker_order_book:
            self.logger().debug(
                f"require_maker_order_book=false; using {sizing_source} ({sizing_price}) "
                f"for balance check on {self.maker_trading_pair}."
            )
        maker_order_candidate = OrderCandidate(
            trading_pair=self.maker_trading_pair,
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=self.maker_order_side,
            amount=self.config.order_amount,
            price=sizing_price,)
        taker_order_candidate = OrderCandidate(
            trading_pair=self.taker_trading_pair,
            is_maker=False,
            order_type=OrderType.MARKET,
            order_side=self.taker_order_side,
            amount=self.config.order_amount,
            price=sizing_price,)
        maker_adjusted_candidate = self.adjust_order_candidates(self.maker_connector, [maker_order_candidate])[0]
        taker_adjusted_candidate = self.adjust_order_candidates(self.taker_connector, [taker_order_candidate])[0]
        if maker_adjusted_candidate.amount == Decimal("0") or taker_adjusted_candidate.amount == Decimal("0"):
            self.close_type = CloseType.INSUFFICIENT_BALANCE
            self.logger().error("Not enough budget to open position.")
            self.stop()

    async def control_task(self):
        if self.status == RunnableStatus.RUNNING:
            await self.update_prices_and_tx_costs()
            if self.status != RunnableStatus.RUNNING:
                return
            await self.control_maker_order()
        elif self.status == RunnableStatus.SHUTTING_DOWN:
            await self.control_shutdown_process()

    async def control_maker_order(self):
        if self.maker_order is None:
            await self.create_maker_order()
        else:
            await self.control_update_maker_order()

    async def update_prices_and_tx_costs(self):
        self._taker_result_price = await self.get_resulting_price_for_amount(
            connector=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            is_buy=self.taker_order_side == TradeType.BUY,
            order_amount=self.config.order_amount)
        await self.update_tx_costs()
        if self.taker_order_side == TradeType.BUY:
            # Maker is SELL: profitability = (maker_price - taker_price) / maker_price
            # To achieve target: maker_price = taker_price / (1 - target_profitability - tx_cost_pct)
            self._maker_target_price = self._taker_result_price / (Decimal("1") - self.config.target_profitability - self._tx_cost_pct)
        else:
            # Maker is BUY: profitability = (taker_price - maker_price) / maker_price
            # To achieve target: maker_price = taker_price / (1 + target_profitability + tx_cost_pct)
            self._maker_target_price = self._taker_result_price / (Decimal("1") + self.config.target_profitability + self._tx_cost_pct)

    async def update_tx_costs(self):
        base, quote = split_hb_trading_pair(trading_pair=self.config.buying_market.trading_pair)
        base_without_wrapped = base[1:] if base.startswith("W") else base
        taker_fee_task = asyncio.create_task(self.get_tx_cost_in_asset(
            exchange=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            order_type=OrderType.MARKET,
            is_buy=self.taker_order_side == TradeType.BUY,
            order_amount=self.config.order_amount,
            asset=base_without_wrapped
        ))
        maker_fee_task = asyncio.create_task(self.get_tx_cost_in_asset(
            exchange=self.maker_connector,
            trading_pair=self.maker_trading_pair,
            order_type=OrderType.LIMIT,
            is_buy=self.maker_order_side == TradeType.BUY,
            order_amount=self.config.order_amount,
            asset=base_without_wrapped
        ))

        taker_fee, maker_fee = await asyncio.gather(taker_fee_task, maker_fee_task)
        self._tx_cost = taker_fee + maker_fee
        self._tx_cost_pct = self._tx_cost / self.config.order_amount

    async def get_tx_cost_in_asset(self, exchange: str, trading_pair: str, is_buy: bool, order_amount: Decimal,
                                   asset: str, order_type: OrderType = OrderType.MARKET):
        connector = self.connectors[exchange]
        if self.is_amm_connector(exchange=exchange):
            gas_cost = connector.network_transaction_fee
            conversion_price = RateOracle.get_instance().get_pair_rate(f"{asset}-{gas_cost.token}")
            if conversion_price is None:
                self.logger().warning(f"Could not get conversion rate for {asset}-{gas_cost.token}")
                return Decimal("0")
            return gas_cost.amount / conversion_price
        else:
            fee = connector.get_fee(
                base_currency=asset,
                quote_currency=trading_pair.split("-")[1],
                order_type=order_type,
                order_side=TradeType.BUY if is_buy else TradeType.SELL,
                amount=order_amount,
                price=self._taker_result_price,
                is_maker=order_type.is_limit_type(),
            )
            return fee.fee_amount_in_token(
                trading_pair=trading_pair,
                price=self._taker_result_price,
                order_amount=order_amount,
                token=asset,
            )

    async def get_resulting_price_for_amount(self, connector: str, trading_pair: str, is_buy: bool,
                                             order_amount: Decimal):
        return await self.connectors[connector].get_quote_price(trading_pair, is_buy, order_amount)

    async def create_maker_order(self):
        order_id = self.place_order(
            connector_name=self.maker_connector,
            trading_pair=self.maker_trading_pair,
            order_type=OrderType.LIMIT,
            side=self.maker_order_side,
            amount=self.config.order_amount,
            price=self._maker_target_price)
        self.maker_order = TrackedOrder(order_id=order_id)
        self._maker_orders_by_id[order_id] = self.maker_order
        self.logger().info(f"Created maker order {order_id} at price {self._maker_target_price}.")

    async def control_shutdown_process(self):
        maker_open = bool(self.maker_order and self.maker_order.order and self.maker_order.order.is_open)
        if not maker_open:
            # maker 已不在挂单，兜底补齐尚未『提交』的对冲量
            self._hedge_pending()
        takers_done = all(
            (t.is_done or t.order_id in self._failed_taker_ids) for t in self.taker_orders
        ) if self.taker_orders else True
        # 用『实际成交』量判断是否真正对冲完成，而非『已提交』量
        fully_hedged = self._actual_hedged_base() >= self._maker_filled_base
        if not maker_open and fully_hedged and takers_done:
            if self._maker_filled_base > Decimal("0") and self.taker_orders:
                self.close_type = CloseType.COMPLETED
            self.logger().info("Maker filled amount fully hedged, executor terminated.")
            self.stop()

    async def control_update_maker_order(self):
        if self.maker_order is None or self.maker_order.is_done:
            return
        await self.update_current_trade_profitability()
        if self.maker_order is None or self.maker_order.is_done:
            return
        net_profitability = self._current_trade_profitability - self._tx_cost_pct
        if net_profitability < self.config.min_profitability:
            self.logger().info(f"Order {self.maker_order.order_id} profitability {net_profitability} is below minimum profitability {self.config.min_profitability}. Cancelling order.")
            self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
            self._handle_maker_cancel()
        elif net_profitability > self.config.max_profitability:
            self.logger().info(f"Order {self.maker_order.order_id} profitability {net_profitability} is above maximum profitability {self.config.max_profitability}. Cancelling order.")
            self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
            self._handle_maker_cancel()

    def _handle_maker_cancel(self):
        """撤单后处理：若已有成交（或已进入对冲）则对冲已成交量并收尾，否则清空以便刷新重挂。"""
        filled = self.maker_order.executed_amount_base if self.maker_order else Decimal("0")
        if filled > Decimal("0") or self._maker_filled_base > Decimal("0") or self._hedging:
            # 以 tracked 累计成交作为下限并入 floor（与 fill 事件取 max，不累加）
            self._maker_filled_floor = max(self._maker_filled_floor, filled)
            self._recompute_maker_filled()
            self._enter_hedging()
            self._hedge_pending()
        else:
            self.maker_order = None

    def _recompute_maker_filled(self):
        self._maker_filled_base = max(self._maker_fills_sum, self._maker_filled_floor)

    def _actual_hedged_base(self) -> Decimal:
        """实际已对冲量 = 所有 taker 单累计成交量（含失败单已成交部分）。单一可信来源。"""
        return sum((t.executed_amount_base for t in self.taker_orders), Decimal("0"))

    def _enter_hedging(self):
        """标记进入对冲收尾：停止刷新挂单，取消仍在挂的 maker，转入 SHUTTING_DOWN。"""
        if self._hedging:
            return
        self._hedging = True
        if self.maker_order and self.maker_order.order and self.maker_order.order.is_open:
            self.logger().info(f"Cancelling remaining maker order {self.maker_order.order_id} before hedging.")
            self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
        self._status = RunnableStatus.SHUTTING_DOWN

    def _hedge_pending(self):
        """对 maker 已成交但尚未『提交』对冲的增量下 taker 单。
        以 _submitted_hedge_base 去重，避免对同一笔成交重复下单。"""
        unhedged = self._maker_filled_base - self._submitted_hedge_base
        if unhedged > Decimal("0"):
            self.place_taker_order(unhedged)
            self._submitted_hedge_base += unhedged

    async def update_current_trade_profitability(self):
        trade_profitability = Decimal("0")
        if self.maker_order and self.maker_order.order and self.maker_order.order.is_open:
            maker_price = self.maker_order.order.price
            # Get the conversion rate to normalize prices to the same quote asset
            try:
                conversion_rate = await self.get_quote_asset_conversion_rate()
                if self.maker_order_side == TradeType.BUY:
                    # If maker is buying, normalize taker (sell) price to maker quote asset
                    normalized_taker_price = self._taker_result_price * conversion_rate
                    trade_profitability = (normalized_taker_price - maker_price) / maker_price
                else:
                    # If maker is selling, normalize taker (buy) price to maker quote asset
                    normalized_taker_price = self._taker_result_price * conversion_rate
                    trade_profitability = (maker_price - normalized_taker_price) / maker_price
            except Exception as e:
                self.logger().error(f"Error calculating trade profitability: {e}")
                return Decimal("0")
        self._current_trade_profitability = trade_profitability
        return trade_profitability

    def process_order_created_event(self,
                                    event_tag: int,
                                    market: ConnectorBase,
                                    event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
        if event.order_id in self._maker_orders_by_id:
            self.logger().info(f"Maker order {event.order_id} created.")
        self._update_tracked_order_with_order_id(event.order_id)

    def _update_tracked_order_with_order_id(self, order_id: str):
        """用最新的 in-flight order 刷新对应的 TrackedOrder，保证 executed_amount_base
        等读数实时可靠。在 created/filled/completed 事件中都调用，避免 created 缺失或
        市价单瞬间成交时 tracked.order 仍为 None，导致 _actual_hedged_base() 误判为 0。
        通过 _maker_orders_by_id 查找，撤单清空 self.maker_order 后迟到的成交也能刷新。"""
        maker_tracked = self._maker_orders_by_id.get(order_id)
        if maker_tracked is not None:
            maker_tracked.order = self.get_in_flight_order(self.maker_connector, order_id)
            return
        for tracked in self.taker_orders:
            if tracked.order_id == order_id:
                tracked.order = self.get_in_flight_order(self.taker_connector, order_id)
                break

    def process_order_completed_event(self,
                                      event_tag: int,
                                      market: ConnectorBase,
                                      event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent]):
        self._update_tracked_order_with_order_id(event.order_id)
        if event.order_id in self._maker_orders_by_id:
            self.logger().info(f"Maker order {event.order_id} completed. Reconciling filled amount.")
            # completed 只抬升『下限』，与 fill 事件取 max，绝不累加，避免重复对冲
            self._maker_filled_floor = max(self._maker_filled_floor, event.base_asset_amount)
            self._recompute_maker_filled()
            self._enter_hedging()
            self._hedge_pending()

    def process_order_filled_event(self,
                                   event_tag: int,
                                   market: ConnectorBase,
                                   event: OrderFilledEvent):
        # 按 trade_id 去重，防止同一笔成交被重复计入（包括 completed 之后再到的 fill）。
        # exchange_trade_id 默认是空字符串 ""（非 None），必须用真值判断，否则所有无
        # trade_id 的成交会共用同一个 key 而被错误去重。并入 order_id 维度避免跨单碰撞。
        exchange_trade_id = getattr(event, "exchange_trade_id", None)
        if exchange_trade_id:
            dedup_key = f"{event.order_id}:{exchange_trade_id}"
        else:
            dedup_key = f"{event.order_id}:{event.timestamp}:{event.price}:{event.amount}"
        self._update_tracked_order_with_order_id(event.order_id)
        is_maker = event.order_id in self._maker_orders_by_id
        # P3 防御：无 exchange_trade_id 时 fallback key 有碰撞概率（同 ts/price/amount 两笔
        # 相同 fill）。必须在 dedup return 之前用 tracked 累计成交量抬升 floor，否则第二笔 fill
        # 命中 dedup 后直接 return，floor 永远停在第一笔的值，导致少对冲。
        if is_maker and not exchange_trade_id:
            maker_tracked = self._maker_orders_by_id.get(event.order_id)
            if maker_tracked is not None:
                self._maker_filled_floor = max(
                    self._maker_filled_floor, maker_tracked.executed_amount_base
                )
                self._recompute_maker_filled()
        if dedup_key in self._seen_trade_ids:
            # dedup 命中：fills_sum 不能再 +=，但 floor 已更新，需补充触发对冲
            if is_maker:
                self._enter_hedging()
                self._hedge_pending()
            return
        self._seen_trade_ids.add(dedup_key)
        # 只有 maker 成交需触发对冲；taker 成交计入实际对冲量（由 _actual_hedged_base 派生）。
        # 用 _maker_orders_by_id 匹配，避免撤单竞态下 maker_order 引用被清空而漏对冲。
        if is_maker:
            self._maker_fills_sum += event.amount
            self._recompute_maker_filled()
            self._enter_hedging()
            self._hedge_pending()

    def place_taker_order(self, amount: Decimal):
        taker_order_id = self.place_order(
            connector_name=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            order_type=OrderType.MARKET,
            side=self.taker_order_side,
            amount=amount)
        tracked = TrackedOrder(order_id=taker_order_id)
        self.taker_orders.append(tracked)
        self.taker_order = tracked
        self._taker_order_ids.add(taker_order_id)
        self._taker_amounts[taker_order_id] = amount
        self.logger().info(f"Placed taker hedge order {taker_order_id} for amount {amount}.")

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        if self.maker_order and self.maker_order.order_id == event.order_id:
            self.failed_orders.append(self.maker_order)
            self.maker_order = None
            self._current_retries += 1
        elif event.order_id in self._taker_order_ids:
            # 幂等保护：同一 failure 事件可能重复到达，已处理过的失败单直接返回，避免重复补单
            if event.order_id in self._failed_taker_ids:
                return
            # 刷新 tracked order，确保读取到真实成交量，避免 created/fill 事件乱序时把已部分
            # 成交量当成 0，进而按全量重试造成过度对冲。
            self._update_tracked_order_with_order_id(event.order_id)
            # taker 对冲失败：只补『未成交』部分，已成交部分仍计入 _actual_hedged_base 与 pnl。
            # 保留该 tracked 在 taker_orders 中（用于统计已成交部分），并标记为已完结。
            original = self._taker_amounts.pop(event.order_id, self.config.order_amount)
            tracked = next((t for t in self.taker_orders if t.order_id == event.order_id), None)
            filled = tracked.executed_amount_base if tracked is not None else Decimal("0")
            unfilled = max(original - filled, Decimal("0"))
            self._failed_taker_ids.add(event.order_id)
            # 回退未成交部分的『已提交』量，使 _hedge_pending 只补未成交的差额
            self._submitted_hedge_base -= unfilled
            self._current_retries += 1
            self._hedge_pending()

    def get_custom_info(self) -> Dict:
        # Since we can't make this method async, we'll skip the profitability calculation
        # The profitability will still be shown in the status message which is async
        return {
            "side": self.config.maker_side,
            "maker_connector": self.maker_connector,
            "maker_trading_pair": self.maker_trading_pair,
            "taker_connector": self.taker_connector,
            "taker_trading_pair": self.taker_trading_pair,
            "min_profitability": self.config.min_profitability,
            "target_profitability_pct": self.config.target_profitability,
            "max_profitability": self.config.max_profitability,
            "trade_profitability": self._current_trade_profitability,
            "tx_cost": self._tx_cost,
            "tx_cost_pct": self._tx_cost_pct,
            "taker_price": self._taker_result_price,
            "maker_target_price": self._maker_target_price,
            "net_profitability": self._current_trade_profitability - self._tx_cost_pct,
            "order_amount": self.config.order_amount,
        }

    def early_stop(self, keep_position: bool = False):
        if self.maker_order and self.maker_order.order and self.maker_order.order.is_open:
            self.logger().info(f"Cancelling maker order {self.maker_order.order_id}.")
            self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
        self.close_type = CloseType.POSITION_HOLD if keep_position else CloseType.EARLY_STOP
        self.stop()

    def get_cum_fees_quote(self) -> Decimal:
        if not self.is_closed:
            return Decimal("0")
        # 聚合所有 maker 单（含撤单后迟到成交的、已清空 self.maker_order 引用的）
        maker_fee = sum((m.cum_fees_quote for m in self._maker_orders_by_id.values()), Decimal("0"))
        taker_fee = sum((t.cum_fees_quote for t in self.taker_orders), Decimal("0"))
        return maker_fee + taker_fee

    def get_net_pnl_quote(self) -> Decimal:
        # 只统计真正有成交的 maker 单，避免 refresh 期间未成交即撤的空单干扰
        filled_makers = [m for m in self._maker_orders_by_id.values() if m.executed_amount_base > Decimal("0")]
        if not self.is_closed or not filled_makers or not self.taker_orders:
            return Decimal("0")
        # 与 shutdown 用同一套 done 判定：失败的 taker 也算完结（其已成交部分仍计入 PnL）
        takers_done = all(
            (t.is_done or t.order_id in self._failed_taker_ids) for t in self.taker_orders
        )
        makers_done = all(m.is_done for m in filled_makers)
        if not (makers_done and takers_done):
            return Decimal("0")
        maker_pnl = sum((m.executed_amount_base * m.average_executed_price for m in filled_makers), Decimal("0"))
        taker_pnl = sum((t.executed_amount_base * t.average_executed_price for t in self.taker_orders), Decimal("0"))
        return taker_pnl - maker_pnl - self.get_cum_fees_quote()

    def get_net_pnl_pct(self) -> Decimal:
        pnl_quote = self.get_net_pnl_quote()
        return pnl_quote / self.config.order_amount

    async def get_quote_asset_conversion_rate(self) -> Decimal:
        """
        Fetch the conversion rate between the quote assets of the buying and selling markets.
        Example: For M3M3/USDT and M3M3/USDC, fetch the USDC/USDT rate.
        """
        try:
            conversion_rate = self.rate_oracle.get_pair_rate(self.quote_conversion_pair)
            if conversion_rate is None:
                self.logger().error(f"Could not fetch conversion rate for {self.quote_conversion_pair}")
                raise ValueError(f"Could not fetch conversion rate for {self.quote_conversion_pair}")
            return conversion_rate
        except Exception as e:
            self.logger().error(f"Error fetching conversion rate for {self.quote_conversion_pair}: {e}")
            raise

    def to_format_status(self):
        return f"""
Maker Side: {self.maker_order_side}
-----------------------------------------------------------------------------------------------------------------------
    - Maker: {self.maker_connector} {self.maker_trading_pair} | Taker: {self.taker_connector} {self.taker_trading_pair}
    - Min profitability: {self.config.min_profitability * 100:.2f}% | Target profitability: {self.config.target_profitability * 100:.2f}% | Max profitability: {self.config.max_profitability * 100:.2f}% | Current profitability: {(self._current_trade_profitability - self._tx_cost_pct) * 100:.2f}%
    - Trade profitability: {self._current_trade_profitability * 100:.2f}% | Tx cost: {self._tx_cost_pct * 100:.2f}%
    - Taker result price: {self._taker_result_price:.3f} | Tx cost: {self._tx_cost:.3f} {self.maker_trading_pair.split('-')[-1]} | Order amount (Base): {self.config.order_amount:.2f}
-----------------------------------------------------------------------------------------------------------------------
"""
