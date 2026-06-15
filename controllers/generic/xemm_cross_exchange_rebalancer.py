"""
Cross-exchange inventory rebalancer for XEMM (V2).

Inspired by cross_exchange_mining.check_balance(): periodically compare base holdings
on maker/taker exchanges and send a market order on the side that corrects drift.

Run alongside xemm_multiple_levels via v2_with_controllers (second controllers_config entry).
"""
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from pydantic import Field, field_validator

from hummingbot.core.data_type.common import PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction
from hummingbot.strategy_v2.executors.xemm_executor.risk import to_decimal

REBALANCE_LEVEL_ID = "cross_exchange_rebalance"


class XEMMCrossExchangeRebalancerConfig(ControllerConfigBase):
    controller_name: str = "xemm_cross_exchange_rebalancer"
    candles_config: List[CandlesConfig] = []

    maker_connector: str = Field(
        default="okx",
        json_schema_extra={"prompt": "Maker connector (e.g. okx): ", "prompt_on_new": True},
    )
    maker_trading_pair: str = Field(
        default="ETH-USDT",
        json_schema_extra={"prompt": "Maker trading pair: ", "prompt_on_new": True},
    )
    taker_connector: str = Field(
        default="hyperliquid",
        json_schema_extra={"prompt": "Taker connector (e.g. hyperliquid): ", "prompt_on_new": True},
    )
    taker_trading_pair: str = Field(
        default="UETH-USDC",
        json_schema_extra={"prompt": "Taker trading pair: ", "prompt_on_new": True},
    )

    target_base_per_exchange: Decimal = Field(
        default=Decimal("0.05"),
        json_schema_extra={
            "prompt": "Target base amount on EACH exchange (in maker base units, e.g. ETH): ",
            "prompt_on_new": True,
        },
    )
    drift_threshold_base: Decimal = Field(
        default=Decimal("0.005"),
        json_schema_extra={
            "prompt": "Min deviation from target before rebalancing (maker base units): ",
            "prompt_on_new": True,
        },
    )
    min_rebalance_amount_base: Decimal = Field(
        default=Decimal("0.001"),
        json_schema_extra={
            "prompt": "Minimum rebalance order size (maker base units): ",
            "prompt_on_new": True,
        },
    )
    rebalance_interval: float = Field(
        default=60.0,
        json_schema_extra={
            "prompt": "Seconds between rebalance checks (cooldown after each order): ",
            "prompt_on_new": True,
        },
    )
    base_conversion_rate: Decimal = Field(
        default=Decimal("1"),
        description="Multiply taker base balance by this to express it in maker base units (e.g. UETH->ETH).",
        json_schema_extra={
            "prompt": "Taker base to maker base conversion (1 if 1:1): ",
            "prompt_on_new": True,
        },
    )
    use_oracle_base_conversion: bool = Field(
        default=False,
        json_schema_extra={
            "prompt": "Use rate oracle for taker_base-maker_base instead of fixed rate? (True/False): ",
            "prompt_on_new": True,
        },
    )
    pause_when_xemm_hedging: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Pause rebalancing while XEMM has unhedged or pending hedge exposure? (True/False): ",
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )
    max_xemm_unhedged_base: Decimal = Field(
        default=Decimal("0"),
        json_schema_extra={
            "prompt": "Max XEMM unhedged base before pausing rebalancer (0 pauses on any unhedged base): ",
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )

    def update_markets(self, markets: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
        for connector, pair in (
            (self.maker_connector, self.maker_trading_pair),
            (self.taker_connector, self.taker_trading_pair),
        ):
            if connector not in markets:
                markets[connector] = set()
            markets[connector].add(pair)
        return markets

    @field_validator("use_oracle_base_conversion", mode="before")
    @classmethod
    def parse_bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "y")
        return bool(v)

    @field_validator("pause_when_xemm_hedging", mode="before")
    @classmethod
    def parse_pause_bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "y")
        return bool(v)


class XEMMCrossExchangeRebalancer(ControllerBase):
    def __init__(self, config: XEMMCrossExchangeRebalancerConfig, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)
        self._next_check_ts = 0.0
        self._last_rebalance_ts = 0.0
        self._last_skip_reason: Optional[str] = None
        self.initialize_rate_sources()

    def initialize_rate_sources(self):
        pairs = [
            ConnectorPair(connector_name=self.config.maker_connector, trading_pair=self.config.maker_trading_pair),
            ConnectorPair(connector_name=self.config.taker_connector, trading_pair=self.config.taker_trading_pair),
        ]
        self.market_data_provider.initialize_rate_sources(pairs)

    @staticmethod
    def _safe_connector_value(getter, default: Decimal = Decimal("0")) -> Decimal:
        try:
            value = to_decimal(getter())
            return value if value is not None else default
        except Exception:
            return default

    def _safe_mid_price(self, connector_name: str, trading_pair: str) -> Decimal:
        try:
            value = self.market_data_provider.get_price_by_type(connector_name, trading_pair, PriceType.MidPrice)
            return to_decimal(value) or Decimal("0")
        except Exception:
            return Decimal("0")

    async def update_processed_data(self):
        maker_base, maker_quote = self.config.maker_trading_pair.split("-")
        taker_base, taker_quote = self.config.taker_trading_pair.split("-")

        maker_connector = self.market_data_provider.get_connector(self.config.maker_connector)
        taker_connector = self.market_data_provider.get_connector(self.config.taker_connector)

        maker_base_bal = self._safe_connector_value(lambda: maker_connector.get_balance(maker_base))
        taker_base_bal = self._safe_connector_value(lambda: taker_connector.get_balance(taker_base))
        maker_base_avail = self._safe_connector_value(lambda: maker_connector.get_available_balance(maker_base))
        taker_base_avail = self._safe_connector_value(lambda: taker_connector.get_available_balance(taker_base))
        maker_quote_avail = self._safe_connector_value(lambda: maker_connector.get_available_balance(maker_quote))
        maker_quote_total = self._safe_connector_value(lambda: maker_connector.get_balance(maker_quote))
        taker_quote_avail = self._safe_connector_value(lambda: taker_connector.get_available_balance(taker_quote))

        base_rate = self._taker_to_maker_base_rate(maker_base, taker_base)
        taker_base_in_maker_units = taker_base_bal * base_rate

        maker_mid = self._safe_mid_price(self.config.maker_connector, self.config.maker_trading_pair)
        taker_mid = self._safe_mid_price(self.config.taker_connector, self.config.taker_trading_pair)

        maker_quote_for_buy = maker_quote_avail if maker_quote_avail > 0 else maker_quote_total
        maker_quote_in_base = maker_quote_for_buy / maker_mid if maker_mid > 0 else Decimal("0")
        taker_quote_in_base = (taker_quote_avail / taker_mid) * base_rate if taker_mid > 0 else Decimal("0")

        target = self.config.target_base_per_exchange
        maker_diff = maker_base_bal - target
        taker_diff = taker_base_in_maker_units - target

        self.processed_data = {
            "maker_base_bal": maker_base_bal,
            "maker_base_avail": maker_base_avail,
            "taker_base_bal": taker_base_bal,
            "taker_base_avail": taker_base_avail,
            "taker_base_in_maker_units": taker_base_in_maker_units,
            "maker_diff": maker_diff,
            "taker_diff": taker_diff,
            "maker_quote_avail": maker_quote_avail,
            "maker_quote_total": maker_quote_total,
            "maker_quote_in_base": maker_quote_in_base,
            "taker_quote_in_base": taker_quote_in_base,
            "maker_mid": maker_mid,
            "taker_mid": taker_mid,
            "base_rate": base_rate,
        }

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if self.config.manual_kill_switch:
            return []

        now = self.market_data_provider.time()
        if now < self._next_check_ts:
            return []

        active = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda e: e.is_active and e.custom_info.get("level_id") == REBALANCE_LEVEL_ID,
        )
        if active:
            return []

        xemm_block_reason = self._xemm_hedge_conflict_reason()
        if xemm_block_reason is not None:
            self._last_skip_reason = xemm_block_reason
            self._next_check_ts = now + self.config.rebalance_interval
            self.logger().info(f"Cross-exchange rebalance skipped: {self._last_skip_reason}")
            return []

        plan = self._plan_rebalance()
        if plan is None:
            self._next_check_ts = now + self.config.rebalance_interval
            if self._last_skip_reason:
                self.logger().info(f"Cross-exchange rebalance skipped: {self._last_skip_reason}")
            return []

        connector, trading_pair, side, amount_base_maker_units = plan
        mid = (
            self.processed_data["maker_mid"]
            if connector == self.config.maker_connector
            else self.processed_data["taker_mid"]
        )
        if mid <= Decimal("0"):
            self._last_skip_reason = f"mid price unavailable for {connector} {trading_pair}"
            self._next_check_ts = now + self.config.rebalance_interval
            self.logger().info(f"Cross-exchange rebalance skipped: {self._last_skip_reason}")
            return []

        if connector == self.config.taker_connector:
            base_rate = self.processed_data["base_rate"]
            amount = amount_base_maker_units / base_rate if base_rate > 0 else amount_base_maker_units
        else:
            amount = amount_base_maker_units

        order_config = OrderExecutorConfig(
            timestamp=now,
            connector_name=connector,
            trading_pair=trading_pair,
            execution_strategy=ExecutionStrategy.MARKET,
            side=side,
            amount=amount,
            price=mid,
            level_id=REBALANCE_LEVEL_ID,
        )
        self._last_rebalance_ts = now
        self._next_check_ts = now + self.config.rebalance_interval
        self.logger().info(
            f"Cross-exchange rebalance: {side.name} {amount} on {connector} {trading_pair} "
            f"(maker_diff={self.processed_data['maker_diff']}, taker_diff={self.processed_data['taker_diff']})"
        )
        return [CreateExecutorAction(controller_id=self.config.id, executor_config=order_config)]

    def _excess_beyond_band(self, diff: Decimal, drift: Decimal) -> Decimal:
        """Amount above target+d drift; rebalance stops at the band edge, not at target."""
        if diff > drift:
            return diff - drift
        return Decimal("0")

    def _deficit_beyond_band(self, diff: Decimal, drift: Decimal) -> Decimal:
        """Amount below target-d drift; rebalance stops at the band edge, not at target."""
        if diff < -drift:
            return -diff - drift
        return Decimal("0")

    def _plan_rebalance(self) -> Optional[Tuple[str, str, TradeType, Decimal]]:
        drift = self.config.drift_threshold_base
        min_amt = self.config.min_rebalance_amount_base
        maker_diff = self.processed_data["maker_diff"]
        taker_diff = self.processed_data["taker_diff"]
        self._last_skip_reason = None

        if taker_diff > drift and maker_diff <= -drift:
            sell_taker = self._sell_plan(
                excess_maker_units=self._excess_beyond_band(taker_diff, drift),
                available_base=self.processed_data["taker_base_avail"],
                base_rate=self.processed_data["base_rate"],
                connector=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
                min_amt=min_amt,
                side_label="taker",
            )
            buy_maker = self._buy_plan(
                deficit_maker_units=self._deficit_beyond_band(maker_diff, drift),
                quote_in_base=self.processed_data["maker_quote_in_base"],
                connector=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
                min_amt=min_amt,
                side_label="maker",
            )
            return self._pick_cheaper(sell_taker, buy_maker)

        if maker_diff > drift and taker_diff <= -drift:
            sell_maker = self._sell_plan(
                excess_maker_units=self._excess_beyond_band(maker_diff, drift),
                available_base=self.processed_data["maker_base_avail"],
                base_rate=Decimal("1"),
                connector=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
                min_amt=min_amt,
                side_label="maker",
            )
            buy_taker = self._buy_plan(
                deficit_maker_units=self._deficit_beyond_band(taker_diff, drift),
                quote_in_base=self.processed_data["taker_quote_in_base"],
                connector=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
                min_amt=min_amt,
                side_label="taker",
            )
            return self._pick_cheaper(sell_maker, buy_taker)

        if maker_diff <= -drift:
            maker_deficit = self._deficit_beyond_band(maker_diff, drift)
            buy_maker = self._buy_plan(
                deficit_maker_units=maker_deficit,
                quote_in_base=self.processed_data["maker_quote_in_base"],
                connector=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
                min_amt=min_amt,
                side_label="maker",
            )
            if taker_diff > min_amt:
                sell_taker = self._sell_plan(
                    excess_maker_units=min(maker_deficit, taker_diff),
                    available_base=self.processed_data["taker_base_avail"],
                    base_rate=self.processed_data["base_rate"],
                    connector=self.config.taker_connector,
                    trading_pair=self.config.taker_trading_pair,
                    min_amt=min_amt,
                    side_label="taker",
                )
                plan = self._pick_cheaper(sell_taker, buy_maker)
                if plan is not None:
                    return plan
            if buy_maker is not None:
                return buy_maker

        if taker_diff <= -drift:
            return self._buy_plan(
                deficit_maker_units=self._deficit_beyond_band(taker_diff, drift),
                quote_in_base=self.processed_data["taker_quote_in_base"],
                connector=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
                min_amt=min_amt,
                side_label="taker",
            )

        if maker_diff > drift:
            return self._sell_plan(
                excess_maker_units=self._excess_beyond_band(maker_diff, drift),
                available_base=self.processed_data["maker_base_avail"],
                base_rate=Decimal("1"),
                connector=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
                min_amt=min_amt,
                side_label="maker",
            )

        if taker_diff > drift:
            return self._sell_plan(
                excess_maker_units=self._excess_beyond_band(taker_diff, drift),
                available_base=self.processed_data["taker_base_avail"],
                base_rate=self.processed_data["base_rate"],
                connector=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
                min_amt=min_amt,
                side_label="taker",
            )

        self._last_skip_reason = (
            f"within drift (maker_diff={maker_diff}, taker_diff={taker_diff}, drift={drift})"
        )
        return None

    def _is_related_xemm_executor(self, executor) -> bool:
        config = executor.config
        if getattr(config, "type", None) != "xemm_executor":
            return False
        connector_pairs = {
            (config.buying_market.connector_name, config.buying_market.trading_pair),
            (config.selling_market.connector_name, config.selling_market.trading_pair),
        }
        rebalancer_pairs = {
            (self.config.maker_connector, self.config.maker_trading_pair),
            (self.config.taker_connector, self.config.taker_trading_pair),
        }
        return bool(connector_pairs & rebalancer_pairs)

    def _xemm_hedge_conflict_reason(self) -> Optional[str]:
        if not self.config.pause_when_xemm_hedging:
            return None

        threshold = self.config.max_xemm_unhedged_base
        executors = getattr(self, "all_executors_info", None) or self.executors_info
        for executor in executors:
            if executor.is_done or not self._is_related_xemm_executor(executor):
                continue
            info = executor.custom_info or {}
            maker_filled = to_decimal(info.get("maker_filled_base")) or Decimal("0")
            submitted_hedge = to_decimal(info.get("submitted_hedge_base")) or Decimal("0")
            actual_hedged = to_decimal(info.get("actual_hedged_base")) or Decimal("0")
            unhedged = to_decimal(info.get("unhedged_base")) or Decimal("0")
            hedge_block_reason = info.get("last_hedge_block_reason")

            allowed_unhedged = threshold if threshold > Decimal("0") else Decimal("0")
            if unhedged > allowed_unhedged:
                return f"related XEMM has unhedged base {unhedged} > {allowed_unhedged}"
            if submitted_hedge > actual_hedged:
                return (
                    f"related XEMM hedge pending "
                    f"(maker_filled={maker_filled}, submitted={submitted_hedge}, actual={actual_hedged})"
                )
            if hedge_block_reason:
                return f"related XEMM hedge blocked: {hedge_block_reason}"
        return None

    def _buy_plan(
        self,
        deficit_maker_units: Decimal,
        quote_in_base: Decimal,
        connector: str,
        trading_pair: str,
        min_amt: Decimal,
        side_label: str = "",
    ) -> Optional[Tuple[str, str, TradeType, Decimal]]:
        amount = min(deficit_maker_units, quote_in_base)
        if amount < min_amt:
            self._last_skip_reason = (
                f"{side_label} BUY too small or insufficient quote "
                f"(need {deficit_maker_units}, quote_in_base={quote_in_base}, min={min_amt})"
            )
            return None
        return connector, trading_pair, TradeType.BUY, amount

    def _sell_plan(
        self,
        excess_maker_units: Decimal,
        available_base: Decimal,
        base_rate: Decimal,
        connector: str,
        trading_pair: str,
        min_amt: Decimal,
        side_label: str = "",
    ) -> Optional[Tuple[str, str, TradeType, Decimal]]:
        max_base = available_base if base_rate == Decimal("1") else available_base
        amount_maker = min(excess_maker_units, max_base * base_rate)
        if amount_maker < min_amt:
            self._last_skip_reason = (
                f"{side_label} SELL too small or insufficient base "
                f"(excess={excess_maker_units}, available={available_base}, min={min_amt})"
            )
            return None
        return connector, trading_pair, TradeType.SELL, amount_maker

    def _pick_cheaper(
        self,
        plan_a: Optional[Tuple[str, str, TradeType, Decimal]],
        plan_b: Optional[Tuple[str, str, TradeType, Decimal]],
    ) -> Optional[Tuple[str, str, TradeType, Decimal]]:
        if plan_a and not plan_b:
            return plan_a
        if plan_b and not plan_a:
            return plan_b
        if not plan_a and not plan_b:
            return None
        _, _, side_a, _ = plan_a
        _, _, side_b, _ = plan_b
        maker_mid = self.processed_data["maker_mid"]
        taker_mid = self.processed_data["taker_mid"]
        base_rate = self.processed_data["base_rate"]
        taker_mid_in_maker = taker_mid * base_rate if base_rate > 0 else taker_mid

        if side_a == TradeType.SELL and side_b == TradeType.BUY:
            return plan_a if maker_mid >= taker_mid_in_maker else plan_b
        return plan_a if plan_a[3] >= plan_b[3] else plan_b

    def _taker_to_maker_base_rate(self, maker_base: str, taker_base: str) -> Decimal:
        if maker_base == taker_base:
            return Decimal("1")
        if self.config.use_oracle_base_conversion:
            rate = self.market_data_provider.get_rate(f"{taker_base}-{maker_base}")
            if rate and rate > 0:
                return Decimal(str(rate))
        return self.config.base_conversion_rate

    def to_format_status(self) -> List[str]:
        d = self.processed_data
        if not d:
            return ["Rebalancer: warming up..."]
        skip = f" | skip: {self._last_skip_reason}" if self._last_skip_reason else ""
        return [
            f"Cross-exchange rebalancer [{self.config.id}]",
            f"  Pair: {self.config.maker_trading_pair} / {self.config.taker_trading_pair}",
            f"  Maker base: {d['maker_base_bal']} avail {d['maker_base_avail']} (diff {d['maker_diff']:+.6f})",
            f"  Taker base: {d['taker_base_bal']} avail {d['taker_base_avail']} (diff {d['taker_diff']:+.6f})",
            f"  Quote buy power: {d['maker_quote_in_base']:.4f} base "
            f"(USDT avail/total {d.get('maker_quote_avail', '?')}/{d.get('maker_quote_total', '?')})",
            f"  Target: {self.config.target_base_per_exchange} | drift: {self.config.drift_threshold_base} | "
            f"next check {max(0, self._next_check_ts - self.market_data_provider.time()):.0f}s{skip}",
        ]
