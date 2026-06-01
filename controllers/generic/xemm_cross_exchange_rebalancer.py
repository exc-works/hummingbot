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


class XEMMCrossExchangeRebalancer(ControllerBase):
    def __init__(self, config: XEMMCrossExchangeRebalancerConfig, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)
        self._next_check_ts = 0.0
        self._last_rebalance_ts = 0.0
        self.initialize_rate_sources()

    def initialize_rate_sources(self):
        maker_base, maker_quote = self.config.maker_trading_pair.split("-")
        taker_base, taker_quote = self.config.taker_trading_pair.split("-")
        pairs = [
            ConnectorPair(self.config.maker_connector, self.config.maker_trading_pair),
            ConnectorPair(self.config.taker_connector, self.config.taker_trading_pair),
        ]
        if maker_quote != taker_quote:
            pairs.append(ConnectorPair(self.config.maker_connector, f"{taker_quote}-{maker_quote}"))
        if maker_base != taker_base:
            pairs.append(ConnectorPair(self.config.maker_connector, f"{taker_base}-{maker_base}"))
        self.market_data_provider.initialize_rate_sources(pairs)

    async def update_processed_data(self):
        maker_base, maker_quote = self.config.maker_trading_pair.split("-")
        taker_base, taker_quote = self.config.taker_trading_pair.split("-")

        maker_connector = self.market_data_provider.get_connector(self.config.maker_connector)
        taker_connector = self.market_data_provider.get_connector(self.config.taker_connector)

        maker_base_bal = Decimal(str(maker_connector.get_balance(maker_base)))
        taker_base_bal = Decimal(str(taker_connector.get_balance(taker_base)))
        maker_quote_avail = Decimal(str(maker_connector.get_available_balance(maker_quote)))
        taker_quote_avail = Decimal(str(taker_connector.get_available_balance(taker_quote)))

        base_rate = self._taker_to_maker_base_rate(maker_base, taker_base)
        taker_base_in_maker_units = taker_base_bal * base_rate

        maker_mid = self.market_data_provider.get_price_by_type(
            self.config.maker_connector, self.config.maker_trading_pair, PriceType.MidPrice
        )
        taker_mid = self.market_data_provider.get_price_by_type(
            self.config.taker_connector, self.config.taker_trading_pair, PriceType.MidPrice
        )

        maker_quote_in_base = maker_quote_avail / maker_mid if maker_mid > 0 else Decimal("0")
        taker_quote_in_base = (taker_quote_avail / taker_mid) * base_rate if taker_mid > 0 else Decimal("0")

        target = self.config.target_base_per_exchange
        maker_diff = maker_base_bal - target
        taker_diff = taker_base_in_maker_units - target

        self.processed_data = {
            "maker_base_bal": maker_base_bal,
            "taker_base_bal": taker_base_bal,
            "taker_base_in_maker_units": taker_base_in_maker_units,
            "maker_diff": maker_diff,
            "taker_diff": taker_diff,
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

        plan = self._plan_rebalance()
        if plan is None:
            self._next_check_ts = now + self.config.rebalance_interval
            return []

        connector, trading_pair, side, amount_base_maker_units = plan
        if side == TradeType.BUY:
            mid = self.processed_data["maker_mid"] if connector == self.config.maker_connector else self.processed_data["taker_mid"]
        else:
            mid = self.processed_data["maker_mid"] if connector == self.config.maker_connector else self.processed_data["taker_mid"]

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

    def _plan_rebalance(self) -> Optional[Tuple[str, str, TradeType, Decimal]]:
        drift = self.config.drift_threshold_base
        min_amt = self.config.min_rebalance_amount_base
        maker_diff = self.processed_data["maker_diff"]
        taker_diff = self.processed_data["taker_diff"]

        # Typical one-sided XEMM drift: maker short, taker long -> sell taker first.
        if taker_diff > drift and maker_diff < -drift:
            sell_taker = self._sell_plan(
                excess_maker_units=taker_diff,
                available_base=self.processed_data["taker_base_bal"],
                base_rate=self.processed_data["base_rate"],
                connector=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
                min_amt=min_amt,
            )
            buy_maker = self._buy_plan(
                deficit_maker_units=-maker_diff,
                quote_in_base=self.processed_data["maker_quote_in_base"],
                connector=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
                min_amt=min_amt,
            )
            return self._pick_cheaper(sell_taker, buy_maker)

        if maker_diff > drift and taker_diff < -drift:
            sell_maker = self._sell_plan(
                excess_maker_units=maker_diff,
                available_base=self.processed_data["maker_base_bal"],
                base_rate=Decimal("1"),
                connector=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
                min_amt=min_amt,
            )
            buy_taker = self._buy_plan(
                deficit_maker_units=-taker_diff,
                quote_in_base=self.processed_data["taker_quote_in_base"],
                connector=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
                min_amt=min_amt,
            )
            return self._pick_cheaper(sell_maker, buy_taker)

        if maker_diff < -drift:
            return self._buy_plan(
                deficit_maker_units=-maker_diff,
                quote_in_base=self.processed_data["maker_quote_in_base"],
                connector=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
                min_amt=min_amt,
            )

        if taker_diff < -drift:
            return self._buy_plan(
                deficit_maker_units=-taker_diff,
                quote_in_base=self.processed_data["taker_quote_in_base"],
                connector=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
                min_amt=min_amt,
            )

        if maker_diff > drift:
            return self._sell_plan(
                excess_maker_units=maker_diff,
                available_base=self.processed_data["maker_base_bal"],
                base_rate=Decimal("1"),
                connector=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
                min_amt=min_amt,
            )

        if taker_diff > drift:
            return self._sell_plan(
                excess_maker_units=taker_diff,
                available_base=self.processed_data["taker_base_bal"],
                base_rate=self.processed_data["base_rate"],
                connector=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
                min_amt=min_amt,
            )

        return None

    def _buy_plan(
        self,
        deficit_maker_units: Decimal,
        quote_in_base: Decimal,
        connector: str,
        trading_pair: str,
        min_amt: Decimal,
    ) -> Optional[Tuple[str, str, TradeType, Decimal]]:
        amount = min(deficit_maker_units, quote_in_base)
        if amount < min_amt:
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
    ) -> Optional[Tuple[str, str, TradeType, Decimal]]:
        max_base = available_base if base_rate == Decimal("1") else available_base
        amount_maker = min(excess_maker_units, max_base * base_rate)
        if amount_maker < min_amt:
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
            # Prefer higher sell / lower buy in maker units
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
        return [
            "Cross-exchange rebalancer",
            f"  Maker base: {d['maker_base_bal']} (diff {d['maker_diff']:+.6f})",
            f"  Taker base: {d['taker_base_bal']} / {d['taker_base_in_maker_units']} maker-units (diff {d['taker_diff']:+.6f})",
            f"  Target each: {self.config.target_base_per_exchange} | next check in "
            f"{max(0, self._next_check_ts - self.market_data_provider.time()):.0f}s",
        ]
