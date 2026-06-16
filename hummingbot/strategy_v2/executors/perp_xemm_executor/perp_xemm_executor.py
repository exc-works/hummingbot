import asyncio
import logging
import time
from decimal import Decimal
from typing import Dict, Optional

from hummingbot.connector.connector_base import ConnectorBase, Union
from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, PositionAction, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import PerpetualOrderCandidate
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
from hummingbot.strategy_v2.executors.perp_xemm_executor.data_types import PerpXEMMExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder


class PerpXEMMExecutor(ExecutorBase):
    """
    Perpetual XEMM executor: places a maker limit order on exchange A (perp), then immediately
    hedges with a market taker order on exchange B (perp) when the maker is filled.

    Key differences from the spot XEMMExecutor:
    - Orders use PositionAction.OPEN (entry) / PositionAction.CLOSE (exit)
    - Both connectors must be perpetual (name contains '_perpetual')
    - Funding rate is pre-deducted from the maker target price
    - Pre-funding-window logic: tighten/loosen thresholds near settlement time
    - Hard failure path: if taker hedge exceeds max_retries, the maker leg is
      immediately emergency-closed at market price to eliminate naked exposure
    """

    _logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    @staticmethod
    def _are_tokens_interchangeable(first_token: str, second_token: str) -> bool:
        interchangeable_tokens = [
            {"WETH", "ETH"},
            {"WBTC", "BTC"},
            {"WBNB", "BNB"},
            {"WPOL", "POL"},
            {"WAVAX", "AVAX"},
            {"WONE", "ONE"},
            {"USDC", "USDC.E"},
            {"USOL", "SOL"},
            {"UETH", "ETH"},
            {"UBTC", "BTC"},
        ]
        if first_token == second_token:
            return True
        if any({first_token, second_token} <= pair for pair in interchangeable_tokens):
            return True
        # treat all USD-stablecoins as interchangeable
        return "USD" in first_token and "USD" in second_token

    def is_arbitrage_valid(self, pair1: str, pair2: str) -> bool:
        base1, _ = split_hb_trading_pair(pair1)
        base2, _ = split_hb_trading_pair(pair2)
        return self._are_tokens_interchangeable(base1, base2)

    @staticmethod
    def _is_perp_connector(connector_name: str) -> bool:
        return "_perpetual" in connector_name.lower()

    def __init__(
        self,
        strategy: StrategyV2Base,
        config: PerpXEMMExecutorConfig,
        update_interval: float = 1.0,
        max_retries: int = 10,
    ):
        # --- Perp connector guard ---
        if not self._is_perp_connector(config.buying_market.connector_name):
            raise ValueError(
                f"PerpXEMMExecutor requires a perpetual connector for buying_market, "
                f"got '{config.buying_market.connector_name}'. "
                f"Use a connector whose name contains '_perpetual'."
            )
        if not self._is_perp_connector(config.selling_market.connector_name):
            raise ValueError(
                f"PerpXEMMExecutor requires a perpetual connector for selling_market, "
                f"got '{config.selling_market.connector_name}'. "
                f"Use a connector whose name contains '_perpetual'."
            )
        if not self.is_arbitrage_valid(
            config.buying_market.trading_pair, config.selling_market.trading_pair
        ):
            raise Exception("PerpXEMM is not valid since the trading pairs are not interchangeable.")

        self.config: PerpXEMMExecutorConfig = config
        self.rate_oracle = RateOracle.get_instance()

        # Resolve maker / taker sides
        if config.maker_side == TradeType.BUY:
            self.maker_connector = config.buying_market.connector_name
            self.maker_trading_pair = config.buying_market.trading_pair
            self.maker_order_side = TradeType.BUY
            self.taker_connector = config.selling_market.connector_name
            self.taker_trading_pair = config.selling_market.trading_pair
            self.taker_order_side = TradeType.SELL
            self._maker_leverage = config.maker_leverage
            self._taker_leverage = config.taker_leverage
        else:
            self.maker_connector = config.selling_market.connector_name
            self.maker_trading_pair = config.selling_market.trading_pair
            self.maker_order_side = TradeType.SELL
            self.taker_connector = config.buying_market.connector_name
            self.taker_trading_pair = config.buying_market.trading_pair
            self.taker_order_side = TradeType.BUY
            # When maker is SELL the selling_market fields map to config.taker_leverage
            self._maker_leverage = config.taker_leverage
            self._taker_leverage = config.maker_leverage

        # Quote conversion setup
        _, maker_quote = split_hb_trading_pair(self.maker_trading_pair)
        _, taker_quote = split_hb_trading_pair(self.taker_trading_pair)
        self.quote_conversion_pair = f"{taker_quote}-{maker_quote}"

        # Pricing state
        self._taker_result_price = Decimal("1")
        self._maker_target_price = Decimal("1")
        self._tx_cost = Decimal("0")
        self._tx_cost_pct = Decimal("0")
        self._funding_buffer_pct = Decimal("0")
        self._current_trade_profitability = Decimal("0")

        # Effective min/max profitability (may be widened near funding settlement)
        self._effective_min_profitability: Decimal = config.min_profitability
        self._effective_max_profitability: Decimal = config.max_profitability

        # Order tracking
        self.maker_order: Optional[TrackedOrder] = None
        self.taker_order: Optional[TrackedOrder] = None
        self.taker_orders = []
        self._maker_orders_by_id: Dict[str, TrackedOrder] = {}
        self._taker_order_ids: set = set()
        self._failed_taker_ids: set = set()
        self._seen_trade_ids: set = set()

        # Filled-amount bookkeeping (dual-source max to prevent double counting)
        self._maker_fills_sum = Decimal("0")
        self._maker_filled_floor = Decimal("0")
        self._maker_filled_base = Decimal("0")
        self._submitted_hedge_base = Decimal("0")
        self._taker_amounts: Dict[str, Decimal] = {}

        # State flags
        self._hedging = False
        self._emergency_close_triggered = False
        self.failed_orders = []

        # Taker market order support check
        taker_conn = strategy.connectors[self.taker_connector]
        if not self.is_amm_connector(exchange=self.taker_connector):
            if OrderType.MARKET not in taker_conn.supported_order_types():
                raise ValueError(f"{self.taker_connector} does not support market orders.")

        super().__init__(
            strategy=strategy,
            connectors=[config.buying_market.connector_name, config.selling_market.connector_name],
            config=config,
            update_interval=update_interval,
            max_retries=max_retries,
        )

    # -----------------------------------------------------------------------
    # Balance validation
    # -----------------------------------------------------------------------

    async def validate_sufficient_balance(self):
        """
        Check that both legs have enough free margin to open positions.
        For isolated margin: required margin = nominal / leverage * (1 + margin_buffer_pct).
        Uses PerpetualOrderCandidate to respect the exchange's budget checker.
        """
        mid_price = self.get_price(
            self.maker_connector, self.maker_trading_pair, price_type=PriceType.MidPrice
        )

        # Maker leg: limit (POST_ONLY) order on perp
        maker_candidate = PerpetualOrderCandidate(
            trading_pair=self.maker_trading_pair,
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=self.maker_order_side,
            amount=self.config.order_amount,
            price=mid_price,
            leverage=Decimal(str(self._maker_leverage)),
            position_close=False,
        )
        # Taker leg: market order on perp
        taker_candidate = PerpetualOrderCandidate(
            trading_pair=self.taker_trading_pair,
            is_maker=False,
            order_type=OrderType.MARKET,
            order_side=self.taker_order_side,
            amount=self.config.order_amount,
            price=mid_price,
            leverage=Decimal(str(self._taker_leverage)),
            position_close=False,
        )

        maker_adj = self.adjust_order_candidates(self.maker_connector, [maker_candidate])[0]
        taker_adj = self.adjust_order_candidates(self.taker_connector, [taker_candidate])[0]

        # Additional buffer check: available free margin must be >= nominal/leverage * (1 + buffer)
        buffer = self.config.margin_buffer_pct
        nominal = mid_price * self.config.order_amount
        maker_required = nominal / Decimal(str(self._maker_leverage)) * (Decimal("1") + buffer)
        taker_required = nominal / Decimal(str(self._taker_leverage)) * (Decimal("1") + buffer)

        maker_conn = self.connectors[self.maker_connector]
        taker_conn = self.connectors[self.taker_connector]
        _, maker_quote = split_hb_trading_pair(self.maker_trading_pair)
        _, taker_quote = split_hb_trading_pair(self.taker_trading_pair)
        maker_available = maker_conn.get_available_balance(maker_quote)
        taker_available = taker_conn.get_available_balance(taker_quote)

        insufficient = False
        if maker_adj.amount == Decimal("0") or maker_available < maker_required:
            self.logger().error(
                f"Insufficient maker margin: need {maker_required:.4f} {maker_quote} "
                f"(incl. {buffer*100:.0f}% buffer), have {maker_available:.4f}."
            )
            insufficient = True
        if taker_adj.amount == Decimal("0") or taker_available < taker_required:
            self.logger().error(
                f"Insufficient taker margin: need {taker_required:.4f} {taker_quote} "
                f"(incl. {buffer*100:.0f}% buffer), have {taker_available:.4f}."
            )
            insufficient = True

        if insufficient:
            self.close_type = CloseType.INSUFFICIENT_BALANCE
            self.stop()

    # -----------------------------------------------------------------------
    # Main control loop
    # -----------------------------------------------------------------------

    async def control_task(self):
        if self.status == RunnableStatus.RUNNING:
            await self.update_prices_and_tx_costs()
            if self.status != RunnableStatus.RUNNING:
                return
            await self.update_funding_buffer()
            await self.control_maker_order()
        elif self.status == RunnableStatus.SHUTTING_DOWN:
            await self.control_shutdown_process()

    async def control_maker_order(self):
        if self.maker_order is None:
            await self.create_maker_order()
        else:
            await self.control_update_maker_order()

    # -----------------------------------------------------------------------
    # Pricing: tx costs + funding buffer
    # -----------------------------------------------------------------------

    async def update_prices_and_tx_costs(self):
        self._taker_result_price = await self.get_resulting_price_for_amount(
            connector=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            is_buy=self.taker_order_side == TradeType.BUY,
            order_amount=self.config.order_amount,
        )
        await self.update_tx_costs()
        self._recompute_maker_target_price()

    def _recompute_maker_target_price(self):
        """
        maker_price = taker_price / (1 ∓ target_profitability ∓ tx_cost_pct ∓ funding_buffer_pct)

        For maker BUY (taker SELL): maker buys cheap, taker sells at higher price.
            maker_price = taker_price / (1 + target + tx_cost + funding_buffer)
        For maker SELL (taker BUY): maker sells high, taker buys at lower price.
            maker_price = taker_price / (1 - target - tx_cost - funding_buffer)
        """
        total_deduction = (
            self.config.target_profitability + self._tx_cost_pct + self._funding_buffer_pct
        )
        if self.taker_order_side == TradeType.BUY:
            # Maker is SELL
            self._maker_target_price = self._taker_result_price / (Decimal("1") - total_deduction)
        else:
            # Maker is BUY
            self._maker_target_price = self._taker_result_price / (Decimal("1") + total_deduction)

    async def update_tx_costs(self):
        base, _ = split_hb_trading_pair(self.config.buying_market.trading_pair)
        base_without_wrapped = base[1:] if base.startswith("W") else base

        taker_fee_task = asyncio.create_task(
            self.get_tx_cost_in_asset(
                exchange=self.taker_connector,
                trading_pair=self.taker_trading_pair,
                order_type=OrderType.MARKET,
                is_buy=self.taker_order_side == TradeType.BUY,
                order_amount=self.config.order_amount,
                asset=base_without_wrapped,
            )
        )
        maker_fee_task = asyncio.create_task(
            self.get_tx_cost_in_asset(
                exchange=self.maker_connector,
                trading_pair=self.maker_trading_pair,
                order_type=OrderType.LIMIT,
                is_buy=self.maker_order_side == TradeType.BUY,
                order_amount=self.config.order_amount,
                asset=base_without_wrapped,
            )
        )
        taker_fee, maker_fee = await asyncio.gather(taker_fee_task, maker_fee_task)
        self._tx_cost = taker_fee + maker_fee
        self._tx_cost_pct = (
            self._tx_cost / self.config.order_amount
            if self.config.order_amount > Decimal("0")
            else Decimal("0")
        )

    async def get_tx_cost_in_asset(
        self,
        exchange: str,
        trading_pair: str,
        is_buy: bool,
        order_amount: Decimal,
        asset: str,
        order_type: OrderType = OrderType.MARKET,
    ) -> Decimal:
        connector = self.connectors[exchange]
        if self.is_amm_connector(exchange=exchange):
            gas_cost = connector.network_transaction_fee
            conversion_price = RateOracle.get_instance().get_pair_rate(f"{asset}-{gas_cost.token}")
            if conversion_price is None:
                self.logger().warning(f"Could not get conversion rate for {asset}-{gas_cost.token}")
                return Decimal("0")
            return gas_cost.amount / conversion_price
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

    # -----------------------------------------------------------------------
    # Funding rate logic
    # -----------------------------------------------------------------------

    async def update_funding_buffer(self):
        """
        Compute funding_buffer_pct as a conservative pre-deduction in the maker target price.

        Buffer = max(0, |rate_maker_per_s| + |rate_taker_per_s|) * min(maker_interval, taker_interval)

        This represents a worst-case single funding period cost (both legs pay).
        The buffer is then also used to adjust effective min/max profitability.

        Additionally, applies pre-funding-window logic:
        - If within pre_funding_window_s of a settlement and net funding is negative
          (i.e. the held position would have to pay), tighten min_profitability by the
          net funding amount so the strategy only enters if spread covers the upcoming payment.
        - If net funding is positive (we would receive), keep/loosen thresholds.
        """
        try:
            maker_conn = self.connectors[self.maker_connector]
            taker_conn = self.connectors[self.taker_connector]

            maker_info = maker_conn.get_funding_info(self.maker_trading_pair)
            taker_info = taker_conn.get_funding_info(self.taker_trading_pair)

            maker_rate: Decimal = maker_info.rate  # rate for the funding period
            taker_rate: Decimal = taker_info.rate

            maker_interval = self.config.maker_funding_interval_s
            taker_interval = self.config.taker_funding_interval_s

            # Rates normalised to per-second
            maker_rate_per_s = maker_rate / Decimal(str(maker_interval))
            taker_rate_per_s = taker_rate / Decimal(str(taker_interval))

            # Worst-case: both legs pay their full funding in the shortest period
            worst_funding_per_s = abs(maker_rate_per_s) + abs(taker_rate_per_s)
            min_interval = min(maker_interval, taker_interval)
            self._funding_buffer_pct = worst_funding_per_s * Decimal(str(min_interval))

            # Recompute maker target price after updating buffer
            self._recompute_maker_target_price()

            # --- Pre-funding window logic ---
            now = time.time()
            maker_next_settlement = maker_info.next_funding_utc_timestamp
            taker_next_settlement = taker_info.next_funding_utc_timestamp
            pre_window = self.config.pre_funding_window_s

            maker_near = (maker_next_settlement - now) < pre_window
            taker_near = (taker_next_settlement - now) < pre_window

            if maker_near or taker_near:
                self._apply_pre_funding_window_logic(maker_rate, taker_rate)
            else:
                # Outside window: restore configured profitability thresholds
                self._effective_min_profitability = self.config.min_profitability
                self._effective_max_profitability = self.config.max_profitability

        except Exception as exc:
            # Funding info may not be available at startup; log and continue with zero buffer
            self.logger().warning(
                f"Could not fetch funding info for {self.maker_trading_pair}/{self.taker_trading_pair}: {exc}. "
                f"funding_buffer_pct set to 0."
            )
            self._funding_buffer_pct = Decimal("0")

    def _apply_pre_funding_window_logic(self, maker_rate: Decimal, taker_rate: Decimal):
        """
        Near settlement, adjust effective profitability thresholds based on net funding direction.

        For a maker-BUY position:
          maker pays funding if maker_rate > 0, receives if < 0
          taker (SHORT) pays if taker_rate < 0, receives if > 0
          net_funding_cost = maker_rate + (-taker_rate) = maker_rate - taker_rate

        For a maker-SELL position:
          maker (SHORT) pays funding if maker_rate < 0, receives if > 0
          taker (LONG) pays if taker_rate > 0, receives if < 0
          net_funding_cost = (-maker_rate) + taker_rate = taker_rate - maker_rate
        """
        if self.maker_order_side == TradeType.BUY:
            net_funding_cost = maker_rate - taker_rate
        else:
            net_funding_cost = taker_rate - maker_rate

        if net_funding_cost > Decimal("0"):
            # Net cost: tighten minimum profitability so entry only happens if spread
            # covers the upcoming funding payment
            extra = net_funding_cost
            self._effective_min_profitability = self.config.min_profitability + extra
            self._effective_max_profitability = self.config.max_profitability + extra
            self.logger().info(
                f"Pre-funding window: net funding cost {net_funding_cost:.6f}. "
                f"Tightening min_profitability to {self._effective_min_profitability:.6f}."
            )
        else:
            # Net receipt: relax thresholds (allow existing positions to ride through settlement)
            self._effective_min_profitability = self.config.min_profitability + net_funding_cost
            self._effective_max_profitability = self.config.max_profitability
            self.logger().info(
                f"Pre-funding window: net funding receipt {-net_funding_cost:.6f}. "
                f"Relaxing min_profitability to {self._effective_min_profitability:.6f}."
            )

    # -----------------------------------------------------------------------
    # Order creation and management
    # -----------------------------------------------------------------------

    async def create_maker_order(self):
        order_id = self.place_order(
            connector_name=self.maker_connector,
            trading_pair=self.maker_trading_pair,
            order_type=OrderType.LIMIT,
            side=self.maker_order_side,
            amount=self.config.order_amount,
            price=self._maker_target_price,
            position_action=PositionAction.OPEN,
        )
        self.maker_order = TrackedOrder(order_id=order_id)
        self._maker_orders_by_id[order_id] = self.maker_order
        self.logger().info(
            f"Created maker order {order_id} at price {self._maker_target_price} "
            f"(leverage={self._maker_leverage}x, funding_buffer={self._funding_buffer_pct:.6f})."
        )

    async def control_update_maker_order(self):
        if self.maker_order is None or self.maker_order.is_done:
            return
        await self.update_current_trade_profitability()
        if self.maker_order is None or self.maker_order.is_done:
            return

        net_profitability = self._current_trade_profitability - self._tx_cost_pct
        if net_profitability < self._effective_min_profitability:
            self.logger().info(
                f"Order {self.maker_order.order_id} net profitability {net_profitability:.6f} "
                f"< effective_min {self._effective_min_profitability:.6f}. Cancelling."
            )
            self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
            self._handle_maker_cancel()
        elif net_profitability > self._effective_max_profitability:
            self.logger().info(
                f"Order {self.maker_order.order_id} net profitability {net_profitability:.6f} "
                f"> effective_max {self._effective_max_profitability:.6f}. Cancelling."
            )
            self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
            self._handle_maker_cancel()

    async def control_shutdown_process(self):
        """Monitor taker hedge completion; stop when done or trigger emergency close."""
        # If maker is still open (shouldn't be in SHUTTING_DOWN, but be safe), cancel it
        maker_open = bool(self.maker_order and self.maker_order.order and self.maker_order.order.is_open)
        if not maker_open:
            self._hedge_pending()

        takers_done = all(
            (t.is_done or t.order_id in self._failed_taker_ids) for t in self.taker_orders
        ) if self.taker_orders else True
        fully_hedged = self._actual_hedged_base() >= self._maker_filled_base

        if not maker_open and fully_hedged and takers_done:
            if self._maker_filled_base > Decimal("0") and self.taker_orders:
                self.close_type = CloseType.COMPLETED
            self.logger().info("Maker filled amount fully hedged, executor terminated.")
            self.stop()

    # -----------------------------------------------------------------------
    # Order placement helpers (perp: OPEN/CLOSE)
    # -----------------------------------------------------------------------

    def place_taker_order(self, amount: Decimal):
        """Place a market hedge order on the taker leg with PositionAction.OPEN."""
        taker_order_id = self.place_order(
            connector_name=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            order_type=OrderType.MARKET,
            side=self.taker_order_side,
            amount=amount,
            position_action=PositionAction.OPEN,
        )
        tracked = TrackedOrder(order_id=taker_order_id)
        self.taker_orders.append(tracked)
        self.taker_order = tracked
        self._taker_order_ids.add(taker_order_id)
        self._taker_amounts[taker_order_id] = amount
        self.logger().info(
            f"Placed taker hedge order {taker_order_id} for {amount} "
            f"(leverage={self._taker_leverage}x)."
        )

    def _place_emergency_close_order(self, amount: Decimal):
        """
        Emergency: close the naked maker leg position at market price.
        Uses PositionAction.CLOSE with the opposite side to flatten the position.
        """
        emergency_side = TradeType.SELL if self.maker_order_side == TradeType.BUY else TradeType.BUY
        order_id = self.place_order(
            connector_name=self.maker_connector,
            trading_pair=self.maker_trading_pair,
            order_type=OrderType.MARKET,
            side=emergency_side,
            amount=amount,
            position_action=PositionAction.CLOSE,
        )
        self.logger().critical(
            f"EMERGENCY CLOSE: placed market order {order_id} to close {amount} of naked "
            f"maker position on {self.maker_connector} {self.maker_trading_pair}."
        )
        return order_id

    # -----------------------------------------------------------------------
    # Internal state helpers
    # -----------------------------------------------------------------------

    def _handle_maker_cancel(self):
        filled = self.maker_order.executed_amount_base if self.maker_order else Decimal("0")
        if filled > Decimal("0") or self._maker_filled_base > Decimal("0") or self._hedging:
            self._maker_filled_floor = max(self._maker_filled_floor, filled)
            self._recompute_maker_filled()
            self._enter_hedging()
            self._hedge_pending()
        else:
            self.maker_order = None

    def _recompute_maker_filled(self):
        self._maker_filled_base = max(self._maker_fills_sum, self._maker_filled_floor)

    def _actual_hedged_base(self) -> Decimal:
        return sum((t.executed_amount_base for t in self.taker_orders), Decimal("0"))

    def _unhedged_base(self) -> Decimal:
        """Amount of maker fill that is not yet covered by any taker fill."""
        return max(self._maker_filled_base - self._actual_hedged_base(), Decimal("0"))

    def _enter_hedging(self):
        if self._hedging:
            return
        self._hedging = True
        if self.maker_order and self.maker_order.order and self.maker_order.order.is_open:
            self.logger().info(
                f"Cancelling remaining maker order {self.maker_order.order_id} before hedging."
            )
            self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
        self._status = RunnableStatus.SHUTTING_DOWN

    def _hedge_pending(self):
        """Submit taker orders for any unsubmitted maker fill."""
        unhedged = self._maker_filled_base - self._submitted_hedge_base
        if unhedged > Decimal("0"):
            self.place_taker_order(unhedged)
            self._submitted_hedge_base += unhedged

    def _trigger_emergency_close(self):
        """
        Called when taker hedge exhausts max_retries.
        Closes the naked maker position at market and stops the executor.
        """
        if self._emergency_close_triggered:
            return
        self._emergency_close_triggered = True

        unhedged = self._unhedged_base()
        if unhedged > Decimal("0"):
            self._place_emergency_close_order(unhedged)
        else:
            self.logger().warning(
                "Emergency close triggered but unhedged amount is 0. "
                "Position may already be flat."
            )
        self.close_type = CloseType.FAILED
        self.logger().critical(
            f"Taker hedge exhausted {self._current_retries} retries. "
            f"Emergency close triggered. Executor stopped."
        )
        self.stop()

    # -----------------------------------------------------------------------
    # Profitability tracking
    # -----------------------------------------------------------------------

    async def update_current_trade_profitability(self):
        trade_profitability = Decimal("0")
        if self.maker_order and self.maker_order.order and self.maker_order.order.is_open:
            maker_price = self.maker_order.order.price
            try:
                conversion_rate = await self.get_quote_asset_conversion_rate()
                normalized_taker_price = self._taker_result_price * conversion_rate
                if self.maker_order_side == TradeType.BUY:
                    trade_profitability = (normalized_taker_price - maker_price) / maker_price
                else:
                    trade_profitability = (maker_price - normalized_taker_price) / maker_price
            except Exception as exc:
                self.logger().error(f"Error calculating trade profitability: {exc}")
                return Decimal("0")
        self._current_trade_profitability = trade_profitability
        return trade_profitability

    async def get_resulting_price_for_amount(self, connector: str, trading_pair: str, is_buy: bool,
                                             order_amount: Decimal) -> Decimal:
        return await self.connectors[connector].get_quote_price(trading_pair, is_buy, order_amount)

    async def get_quote_asset_conversion_rate(self) -> Decimal:
        try:
            rate = self.rate_oracle.get_pair_rate(self.quote_conversion_pair)
            if rate is None:
                raise ValueError(f"Could not fetch conversion rate for {self.quote_conversion_pair}")
            return rate
        except Exception as exc:
            self.logger().error(f"Error fetching conversion rate for {self.quote_conversion_pair}: {exc}")
            raise

    # -----------------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------------

    def process_order_created_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent],
    ):
        if event.order_id in self._maker_orders_by_id:
            self.logger().info(f"Maker order {event.order_id} created.")
        self._update_tracked_order_with_order_id(event.order_id)

    def _update_tracked_order_with_order_id(self, order_id: str):
        maker_tracked = self._maker_orders_by_id.get(order_id)
        if maker_tracked is not None:
            maker_tracked.order = self.get_in_flight_order(self.maker_connector, order_id)
            return
        for tracked in self.taker_orders:
            if tracked.order_id == order_id:
                tracked.order = self.get_in_flight_order(self.taker_connector, order_id)
                break

    def process_order_completed_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent],
    ):
        self._update_tracked_order_with_order_id(event.order_id)
        if event.order_id in self._maker_orders_by_id:
            self.logger().info(f"Maker order {event.order_id} completed. Reconciling filled amount.")
            self._maker_filled_floor = max(self._maker_filled_floor, event.base_asset_amount)
            self._recompute_maker_filled()
            self._enter_hedging()
            self._hedge_pending()

    def process_order_filled_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: OrderFilledEvent,
    ):
        exchange_trade_id = getattr(event, "exchange_trade_id", None)
        if exchange_trade_id:
            dedup_key = f"{event.order_id}:{exchange_trade_id}"
        else:
            dedup_key = f"{event.order_id}:{event.timestamp}:{event.price}:{event.amount}"

        self._update_tracked_order_with_order_id(event.order_id)
        is_maker = event.order_id in self._maker_orders_by_id

        # Floor update before potential dedup return (prevents under-hedging on fallback keys)
        if is_maker and not exchange_trade_id:
            maker_tracked = self._maker_orders_by_id.get(event.order_id)
            if maker_tracked is not None:
                self._maker_filled_floor = max(
                    self._maker_filled_floor, maker_tracked.executed_amount_base
                )
                self._recompute_maker_filled()

        if dedup_key in self._seen_trade_ids:
            if is_maker:
                self._enter_hedging()
                self._hedge_pending()
            return

        self._seen_trade_ids.add(dedup_key)
        if is_maker:
            self._maker_fills_sum += event.amount
            self._recompute_maker_filled()
            self._enter_hedging()
            self._hedge_pending()

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        if self.maker_order and self.maker_order.order_id == event.order_id:
            self.failed_orders.append(self.maker_order)
            self.maker_order = None
            self._current_retries += 1
        elif event.order_id in self._taker_order_ids:
            if event.order_id in self._failed_taker_ids:
                return
            self._update_tracked_order_with_order_id(event.order_id)
            original = self._taker_amounts.pop(event.order_id, self.config.order_amount)
            tracked = next((t for t in self.taker_orders if t.order_id == event.order_id), None)
            filled = tracked.executed_amount_base if tracked is not None else Decimal("0")
            unfilled = max(original - filled, Decimal("0"))
            self._failed_taker_ids.add(event.order_id)
            self._submitted_hedge_base -= unfilled
            self._current_retries += 1

            # Check if max retries exceeded → emergency close
            if self._current_retries >= self._max_retries:
                self.logger().critical(
                    f"Taker hedge failed {self._current_retries} times (max={self._max_retries}). "
                    f"Triggering emergency close of naked maker position."
                )
                self._trigger_emergency_close()
            else:
                self.logger().warning(
                    f"Taker hedge failed (retry {self._current_retries}/{self._max_retries}). "
                    f"Re-attempting hedge for {unfilled}."
                )
                self._hedge_pending()

    # -----------------------------------------------------------------------
    # Info and status
    # -----------------------------------------------------------------------

    def get_custom_info(self) -> Dict:
        return {
            "side": self.config.maker_side,
            "maker_connector": self.maker_connector,
            "maker_trading_pair": self.maker_trading_pair,
            "maker_leverage": self._maker_leverage,
            "taker_connector": self.taker_connector,
            "taker_trading_pair": self.taker_trading_pair,
            "taker_leverage": self._taker_leverage,
            "min_profitability": self.config.min_profitability,
            "effective_min_profitability": self._effective_min_profitability,
            "target_profitability_pct": self.config.target_profitability,
            "max_profitability": self.config.max_profitability,
            "trade_profitability": self._current_trade_profitability,
            "tx_cost_pct": self._tx_cost_pct,
            "funding_buffer_pct": self._funding_buffer_pct,
            "net_profitability": self._current_trade_profitability - self._tx_cost_pct,
            "taker_price": self._taker_result_price,
            "maker_target_price": self._maker_target_price,
            "order_amount": self.config.order_amount,
            "emergency_close_triggered": self._emergency_close_triggered,
        }

    def early_stop(self, keep_position: bool = False):
        if self.maker_order and self.maker_order.order and self.maker_order.order.is_open:
            self.logger().info(f"Cancelling maker order {self.maker_order.order_id}.")
            self._strategy.cancel(
                self.maker_connector, self.maker_trading_pair, self.maker_order.order_id
            )
        self.close_type = CloseType.POSITION_HOLD if keep_position else CloseType.EARLY_STOP
        self.stop()

    def get_cum_fees_quote(self) -> Decimal:
        if not self.is_closed:
            return Decimal("0")
        maker_fee = sum((m.cum_fees_quote for m in self._maker_orders_by_id.values()), Decimal("0"))
        taker_fee = sum((t.cum_fees_quote for t in self.taker_orders), Decimal("0"))
        return maker_fee + taker_fee

    def get_net_pnl_quote(self) -> Decimal:
        filled_makers = [
            m for m in self._maker_orders_by_id.values() if m.executed_amount_base > Decimal("0")
        ]
        if not self.is_closed or not filled_makers or not self.taker_orders:
            return Decimal("0")
        takers_done = all(
            (t.is_done or t.order_id in self._failed_taker_ids) for t in self.taker_orders
        )
        makers_done = all(m.is_done for m in filled_makers)
        if not (makers_done and takers_done):
            return Decimal("0")
        maker_pnl = sum(
            (m.executed_amount_base * m.average_executed_price for m in filled_makers), Decimal("0")
        )
        taker_pnl = sum(
            (t.executed_amount_base * t.average_executed_price for t in self.taker_orders), Decimal("0")
        )
        return taker_pnl - maker_pnl - self.get_cum_fees_quote()

    def get_net_pnl_pct(self) -> Decimal:
        pnl_quote = self.get_net_pnl_quote()
        return (
            pnl_quote / self.config.order_amount
            if self.config.order_amount > Decimal("0")
            else Decimal("0")
        )

    def to_format_status(self) -> str:
        return (
            f"Maker Side: {self.maker_order_side}\n"
            f"-------------------------------------------------------\n"
            f"  Maker : {self.maker_connector} {self.maker_trading_pair} "
            f"(x{self._maker_leverage})\n"
            f"  Taker : {self.taker_connector} {self.taker_trading_pair} "
            f"(x{self._taker_leverage})\n"
            f"  Target profitability : {self.config.target_profitability * 100:.3f}%  "
            f"| tx_cost : {self._tx_cost_pct * 100:.3f}%  "
            f"| funding_buffer : {self._funding_buffer_pct * 100:.4f}%\n"
            f"  Effective min prof   : {self._effective_min_profitability * 100:.3f}%  "
            f"| Current net : {(self._current_trade_profitability - self._tx_cost_pct) * 100:.3f}%\n"
            f"  Taker price : {self._taker_result_price:.4f}  "
            f"| Maker target : {self._maker_target_price:.4f}\n"
            f"  Order amount : {self.config.order_amount}  "
            f"| Emergency close : {self._emergency_close_triggered}\n"
            f"-------------------------------------------------------\n"
        )
