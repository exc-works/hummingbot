"""
Perp XEMM Multiple Levels Controller

A perpetual-market version of xemm_multiple_levels that:
- Requires both maker and taker connectors to be perpetual (_perpetual in name)
- Sets leverage + position_mode once per leg in __init__, with retry in update_processed_data
- Supports multi-level (multi-tier) buy/sell grids
- Applies a controller-level margin gate before creating any new executor
- Optionally warns when margin mode is not ISOLATED (read-only check)
"""
import time
from decimal import Decimal
from typing import Dict, List, Optional, Set

import pandas as pd
from pydantic import Field, field_validator

from hummingbot.client.ui.interface_utils import format_df_for_printout
from hummingbot.core.data_type.common import PositionMode, PositionSide, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.perp_xemm_executor.data_types import (
    DEFAULT_FUNDING_PAYMENT_INTERVAL_S,
    DEFAULT_PRE_FUNDING_WINDOW_S,
    PerpXEMMExecutorConfig,
)
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction


class PerpXEMMMultipleLevelsConfig(ControllerConfigBase):
    """
    Configuration for the Perp XEMM multiple-levels controller.

    Example YAML:
        controller_name: perp_xemm_multiple_levels
        maker_connector: caishen_perpetual
        maker_trading_pair: ETH-USDT
        taker_connector: binance_perpetual
        taker_trading_pair: ETH-USDT
        maker_leverage: 2
        taker_leverage: 2
        position_mode: ONEWAY
        buy_levels_targets_amount: "0.003,10-0.006,20-0.009,30"
        sell_levels_targets_amount: "0.003,10-0.006,20-0.009,30"
        min_profitability: 0.002
        max_profitability: 0.012
        max_executors_imbalance: 1
        total_amount_quote: 300
        margin_buffer_pct: 1.0
        maker_funding_interval_s: 28800
        taker_funding_interval_s: 28800
        pre_funding_window_s: 600
    """

    controller_name: str = "perp_xemm_multiple_levels"

    maker_connector: str = Field(
        default="caishen_perpetual",
        json_schema_extra={"prompt": "Enter the maker perpetual connector: ", "prompt_on_new": True},
    )
    maker_trading_pair: str = Field(
        default="ETH-USDT",
        json_schema_extra={"prompt": "Enter the maker trading pair: ", "prompt_on_new": True},
    )
    taker_connector: str = Field(
        default="binance_perpetual",
        json_schema_extra={"prompt": "Enter the taker perpetual connector: ", "prompt_on_new": True},
    )
    taker_trading_pair: str = Field(
        default="ETH-USDT",
        json_schema_extra={"prompt": "Enter the taker trading pair: ", "prompt_on_new": True},
    )

    # Per-leg leverage (default 1 = conservative, safest for isolated margin)
    maker_leverage: int = Field(
        default=1,
        json_schema_extra={"prompt": "Enter the maker leverage (default 1): ", "prompt_on_new": True},
    )
    taker_leverage: int = Field(
        default=1,
        json_schema_extra={"prompt": "Enter the taker leverage (default 1): ", "prompt_on_new": True},
    )

    # Position mode for both legs (ONEWAY recommended for simplicity)
    position_mode: PositionMode = Field(
        default=PositionMode.ONEWAY,
        json_schema_extra={"prompt": "Enter the position mode (ONEWAY/HEDGE): ", "prompt_on_new": True},
    )

    # Multi-level grid: format "target_pct,amount-target_pct,amount-..."
    buy_levels_targets_amount: List[List[Decimal]] = Field(
        default="0.003,10-0.006,20-0.009,30",
        json_schema_extra={
            "prompt": (
                "Enter buy levels (target_profitability,amount pairs separated by '-'): "
            ),
            "prompt_on_new": True,
        },
    )
    sell_levels_targets_amount: List[List[Decimal]] = Field(
        default="0.003,10-0.006,20-0.009,30",
        json_schema_extra={
            "prompt": (
                "Enter sell levels (target_profitability,amount pairs separated by '-'): "
            ),
            "prompt_on_new": True,
        },
    )

    min_profitability: Decimal = Field(
        default=Decimal("0.002"),
        json_schema_extra={"prompt": "Enter the minimum profitability: ", "prompt_on_new": True},
    )
    max_profitability: Decimal = Field(
        default=Decimal("0.012"),
        json_schema_extra={"prompt": "Enter the maximum profitability: ", "prompt_on_new": True},
    )
    max_executors_imbalance: int = Field(
        default=1,
        json_schema_extra={"prompt": "Enter the maximum executors imbalance: ", "prompt_on_new": True},
    )

    # Margin buffer: required free margin = nominal/leverage * (1 + margin_buffer_pct)
    # For isolated margin, recommend >= 1.0 (2x the minimum)
    margin_buffer_pct: Decimal = Field(
        default=Decimal("1.0"),
        json_schema_extra={"prompt": "Enter the margin buffer pct (>=1.0 for isolated): ", "prompt_on_new": True},
    )

    # Funding-related parameters
    maker_funding_interval_s: int = Field(
        default=DEFAULT_FUNDING_PAYMENT_INTERVAL_S,
        json_schema_extra={"prompt": "Enter maker funding interval in seconds (28800=8h): ", "prompt_on_new": True},
    )
    taker_funding_interval_s: int = Field(
        default=DEFAULT_FUNDING_PAYMENT_INTERVAL_S,
        json_schema_extra={"prompt": "Enter taker funding interval in seconds (28800=8h): ", "prompt_on_new": True},
    )
    pre_funding_window_s: int = Field(
        default=DEFAULT_PRE_FUNDING_WINDOW_S,
        json_schema_extra={
            "prompt": "Enter pre-funding window in seconds (600=10min before settlement): ",
            "prompt_on_new": True,
        },
    )

    # Inventory skew: read exchange positions and bias quoting toward flattening maker leg
    inventory_skew_enabled: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Enable inventory skew from exchange positions (true/false): ",
            "prompt_on_new": True,
        },
    )
    inventory_skew_band_quote: Decimal = Field(
        default=Decimal("50"),
        json_schema_extra={
            "prompt": "Dead band in quote before inventory skew applies (e.g. 50 USDC): ",
            "prompt_on_new": True,
        },
    )

    @field_validator("buy_levels_targets_amount", "sell_levels_targets_amount", mode="before")
    @classmethod
    def validate_levels_targets_amount(cls, v):
        if isinstance(v, str):
            v = [list(map(Decimal, x.split(","))) for x in v.split("-")]
        return v

    @field_validator("maker_connector", "taker_connector", mode="before")
    @classmethod
    def validate_perpetual_connector(cls, v):
        if "_perpetual" not in str(v).lower():
            raise ValueError(
                f"PerpXEMMMultipleLevels requires perpetual connectors "
                f"(name must contain '_perpetual'), got '{v}'."
            )
        return v

    def update_markets(self, markets: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
        for connector, pair in [
            (self.maker_connector, self.maker_trading_pair),
            (self.taker_connector, self.taker_trading_pair),
        ]:
            if connector not in markets:
                markets[connector] = set()
            markets[connector].add(pair)
        return markets


class PerpXEMMMultipleLevels(ControllerBase):
    """
    Perpetual XEMM multi-level controller.

    Sets leverage + position mode once in __init__, retries in update_processed_data
    until both legs acknowledge the settings.  Before creating any new executor it
    validates that both legs have enough free margin (controller-level margin gate).
    """

    # Interval between leverage/position-mode retry attempts (seconds)
    _LEVERAGE_RETRY_INTERVAL_S: float = 10.0

    def __init__(self, config: PerpXEMMMultipleLevelsConfig, *args, **kwargs):
        self.config = config
        self.buy_levels_targets_amount = config.buy_levels_targets_amount
        self.sell_levels_targets_amount = config.sell_levels_targets_amount

        # Leverage/position-mode readiness tracking
        self._maker_leverage_ready: bool = False
        self._taker_leverage_ready: bool = False
        self._last_leverage_attempt: float = 0.0
        self._leverage_setup_dispatched_at: Dict[str, float] = {}
        self._leverage_setup_in_progress: Set[str] = set()
        self._LEVERAGE_CONFIRM_WAIT_S: float = 3.0
        self._startup_positions_logged: bool = False
        self._last_imbalance_log_key: Optional[tuple] = None

        super().__init__(config, *args, **kwargs)

    # -----------------------------------------------------------------------
    # Leverage / position-mode setup with retry
    # -----------------------------------------------------------------------

    async def _setup_leverage_and_position_mode(self):
        """
        Apply set_leverage + set_position_mode on both perp connectors.
        Uses sequential async setup to avoid Caishen on-chain tx races.
        """
        now = time.time()
        if now - self._last_leverage_attempt < self._LEVERAGE_RETRY_INTERVAL_S:
            return
        self._last_leverage_attempt = now

        for connector_name, trading_pair, leverage, is_maker in [
            (self.config.maker_connector, self.config.maker_trading_pair, self.config.maker_leverage, True),
            (self.config.taker_connector, self.config.taker_trading_pair, self.config.taker_leverage, False),
        ]:
            already_ready = self._maker_leverage_ready if is_maker else self._taker_leverage_ready
            if already_ready or connector_name in self._leverage_setup_in_progress:
                continue
            try:
                connector = self.market_data_provider.get_connector(connector_name)
                if not connector.ready:
                    self.logger().warning(
                        f"[Perp XEMM] {connector_name} not ready yet; "
                        f"will retry leverage/position-mode setup."
                    )
                    continue

                self._leverage_setup_in_progress.add(connector_name)
                self._leverage_setup_dispatched_at[connector_name] = now
                self.logger().info(
                    f"[Perp XEMM] Applying leverage/position-mode for "
                    f"{connector_name} {trading_pair} "
                    f"(position_mode={self.config.position_mode.name}, leverage={leverage}x)."
                )

                if hasattr(connector, "configure_trading_pair_settings"):
                    await connector.configure_trading_pair_settings(
                        trading_pair, leverage, self.config.position_mode
                    )
                else:
                    connector.set_position_mode(self.config.position_mode)
                    connector.set_leverage(trading_pair, leverage)

            except Exception as exc:
                self.logger().error(
                    f"[Perp XEMM] Failed to apply leverage/position_mode on {connector_name}: {exc}. "
                    f"Will retry."
                )
                self._leverage_setup_dispatched_at.pop(connector_name, None)
                self._last_leverage_attempt = 0.0
            finally:
                self._leverage_setup_in_progress.discard(connector_name)

    async def _confirm_leverage_setup(self):
        """Mark a leg ready only after exchange-side settings match the config."""
        for connector_name, trading_pair, leverage, is_maker in [
            (self.config.maker_connector, self.config.maker_trading_pair, self.config.maker_leverage, True),
            (self.config.taker_connector, self.config.taker_trading_pair, self.config.taker_leverage, False),
        ]:
            if is_maker and self._maker_leverage_ready:
                continue
            if not is_maker and self._taker_leverage_ready:
                continue

            dispatched_at = self._leverage_setup_dispatched_at.get(connector_name)
            if dispatched_at is None:
                continue

            elapsed = time.time() - dispatched_at
            if elapsed < self._LEVERAGE_CONFIRM_WAIT_S:
                continue

            try:
                connector = self.market_data_provider.get_connector(connector_name)
            except Exception:
                continue

            if hasattr(connector, "get_exchange_isolated_leverage"):
                exchange_lev = await connector.get_exchange_isolated_leverage(trading_pair)
                leverage_ok = exchange_lev == leverage
            else:
                exchange_lev = None
                leverage_ok = connector.get_leverage(trading_pair) == leverage

            mode_ok = connector.position_mode == self.config.position_mode

            if leverage_ok and mode_ok:
                self.logger().info(
                    f"[Perp XEMM] {connector_name} {trading_pair}: "
                    f"position_mode={self.config.position_mode.name}, leverage={leverage}x confirmed."
                )
                if is_maker:
                    self._maker_leverage_ready = True
                else:
                    self._taker_leverage_ready = True
                self._warn_if_not_isolated(connector, connector_name, trading_pair)
            elif elapsed > 60:
                self.logger().warning(
                    f"[Perp XEMM] {connector_name} {trading_pair} setup not confirmed after 60s "
                    f"(exchange_lev={exchange_lev}, target={leverage}, "
                    f"local_lev={connector.get_leverage(trading_pair)}, mode_ok={mode_ok}); will retry."
                )
                self._leverage_setup_dispatched_at.pop(connector_name, None)
                self._last_leverage_attempt = 0.0

    @staticmethod
    def _warn_if_not_isolated(connector, connector_name: str, trading_pair: str):
        """Read current margin mode and warn if not ISOLATED (read-only, no connector change)."""
        try:
            # Not all connectors expose get_margin_mode; guard with hasattr
            if hasattr(connector, "get_margin_mode"):
                margin_mode = connector.get_margin_mode(trading_pair)
                if margin_mode is not None and str(margin_mode).upper() != "ISOLATED":
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[Perp XEMM] {connector_name} {trading_pair} margin mode is "
                        f"'{margin_mode}', not ISOLATED. "
                        f"For isolated risk control, set ISOLATED manually on the exchange "
                        f"before starting the bot. The strategy will NOT change this setting."
                    )
        except Exception:
            pass  # Best-effort only; connector may not support margin mode query

    @property
    def _leverage_ready(self) -> bool:
        return self._maker_leverage_ready and self._taker_leverage_ready

    # -----------------------------------------------------------------------
    # Controller lifecycle
    # -----------------------------------------------------------------------

    async def update_processed_data(self):
        """Retry leverage/position-mode setup until both legs confirm ready."""
        if not self._leverage_ready:
            await self._setup_leverage_and_position_mode()
            await self._confirm_leverage_setup()
        elif not self._startup_positions_logged:
            self._log_startup_positions()

    # -----------------------------------------------------------------------
    # Exchange position / inventory skew
    # -----------------------------------------------------------------------

    @staticmethod
    def _signed_position_base(connector, trading_pair: str) -> Decimal:
        """Signed base position: long > 0, short < 0, flat = 0."""
        perpetual_trading = getattr(connector, "_perpetual_trading", None)
        if perpetual_trading is None:
            return Decimal("0")
        position = perpetual_trading.get_position(trading_pair)
        if position is None or position.amount == 0:
            return Decimal("0")
        if position.position_side == PositionSide.SHORT:
            return -position.amount
        return position.amount

    def _read_leg_positions(self) -> tuple[Decimal, Decimal]:
        maker_conn = self.market_data_provider.get_connector(self.config.maker_connector)
        taker_conn = self.market_data_provider.get_connector(self.config.taker_connector)
        maker_base = self._signed_position_base(maker_conn, self.config.maker_trading_pair)
        taker_base = self._signed_position_base(taker_conn, self.config.taker_trading_pair)
        return maker_base, taker_base

    def _log_startup_positions(self):
        """One-time snapshot after connectors are ready."""
        try:
            maker_base, taker_base = self._read_leg_positions()
            mid = self.market_data_provider.get_price_by_type(
                self.config.maker_connector,
                self.config.maker_trading_pair,
                PriceType.MidPrice,
            )
            mid = mid if mid is not None and mid > 0 else Decimal("0")
            drift_base = maker_base + taker_base
            self.logger().info(
                f"[Perp XEMM] Startup positions — "
                f"maker {self.config.maker_connector} {self.config.maker_trading_pair}: "
                f"{maker_base} base"
                f"{f' (~{maker_base * mid:.2f} quote)' if mid > 0 else ''}; "
                f"taker {self.config.taker_connector} {self.config.taker_trading_pair}: "
                f"{taker_base} base"
                f"{f' (~{taker_base * mid:.2f} quote)' if mid > 0 else ''}; "
                f"cross-leg drift (maker+taker): {drift_base} base."
            )
            if mid > 0 and abs(drift_base * mid) > self.config.inventory_skew_band_quote:
                self.logger().warning(
                    f"[Perp XEMM] Cross-leg drift {drift_base} base "
                    f"(~{drift_base * mid:.2f} quote) exceeds band "
                    f"{self.config.inventory_skew_band_quote}; check hedge alignment."
                )
        except Exception as exc:
            self.logger().warning(f"[Perp XEMM] Could not read startup positions: {exc}.")
        finally:
            self._startup_positions_logged = True

    def _inventory_skew_adjustment(
        self,
        mid_price: Decimal,
        per_level_quote: Decimal,
    ) -> tuple[int, Decimal, Decimal]:
        """
        Derive inventory bias (in executor-count units) and buy/sell size scales
        from maker-leg net position. Short maker → favor BUY / shrink SELL.
        """
        if not self.config.inventory_skew_enabled or per_level_quote <= 0:
            return 0, Decimal("1"), Decimal("1")

        maker_base, _ = self._read_leg_positions()
        maker_quote = maker_base * mid_price
        band = self.config.inventory_skew_band_quote
        if abs(maker_quote) <= band:
            return 0, Decimal("1"), Decimal("1")

        level_units = min(
            int(abs(maker_quote) / per_level_quote),
            self.config.max_executors_imbalance,
        )
        level_units = max(level_units, 1)
        scale = min(Decimal("2"), Decimal("1") + abs(maker_quote) / band)
        min_scale = Decimal("0.25")

        if maker_quote < 0:
            return -level_units, scale, max(min_scale, Decimal("2") - scale)
        return level_units, max(min_scale, Decimal("2") - scale), scale

    def _log_imbalance_if_changed(
        self,
        fill_imbalance: int,
        inventory_bias: int,
        effective_imbalance: int,
        buy_scale: Decimal,
        sell_scale: Decimal,
    ):
        """Log imbalance state only when it changes, not every control tick."""
        key = (
            fill_imbalance,
            inventory_bias,
            effective_imbalance,
            buy_scale,
            sell_scale,
        )
        if key == self._last_imbalance_log_key:
            return
        self._last_imbalance_log_key = key
        if fill_imbalance == 0 and inventory_bias == 0:
            self.logger().info("[Perp XEMM] Imbalance neutral (no fill skew, flat inventory).")
            return
        self.logger().info(
            f"[Perp XEMM] Imbalance: fill_delta={fill_imbalance} "
            f"inventory_bias={inventory_bias} effective={effective_imbalance} "
            f"(max=±{self.config.max_executors_imbalance}, "
            f"buy_scale={buy_scale:.2f}, sell_scale={sell_scale:.2f})."
        )

    # -----------------------------------------------------------------------
    # Margin gate helper
    # -----------------------------------------------------------------------

    def _count_filled_executors(self, maker_side: TradeType) -> int:
        """
        Count executors with maker fills on the given side.
        Includes in-flight hedges (not yet TERMINATED) so imbalance reacts before
        the executor lifecycle fully completes.
        """
        return len([
            e for e in self.executors_info
            if e.config.maker_side == maker_side and e.filled_amount_quote > Decimal("0")
        ])

    def _has_sufficient_margin(self, nominal_quote: Decimal) -> bool:
        """
        Controller-level margin gate.
        Check that both legs have enough free margin for a new executor of size nominal_quote.

        Required margin per leg = nominal_quote / leverage * (1 + margin_buffer_pct)
        This prevents N simultaneous executors from cumulatively exhausting isolated margin.
        """
        buffer = self.config.margin_buffer_pct

        try:
            maker_conn = self.market_data_provider.get_connector(self.config.maker_connector)
            taker_conn = self.market_data_provider.get_connector(self.config.taker_connector)
            _, maker_quote = self.config.maker_trading_pair.split("-")
            _, taker_quote = self.config.taker_trading_pair.split("-")

            maker_available = maker_conn.get_available_balance(maker_quote)
            taker_available = taker_conn.get_available_balance(taker_quote)

            maker_required = (
                nominal_quote / Decimal(str(self.config.maker_leverage)) * (Decimal("1") + buffer)
            )
            taker_required = (
                nominal_quote / Decimal(str(self.config.taker_leverage)) * (Decimal("1") + buffer)
            )

            if maker_available < maker_required:
                self.logger().warning(
                    f"[Margin Gate] Maker margin insufficient: "
                    f"need {maker_required:.2f} {maker_quote}, "
                    f"have {maker_available:.2f}. "
                    f"Skipping new executor."
                )
                return False
            if taker_available < taker_required:
                self.logger().warning(
                    f"[Margin Gate] Taker margin insufficient: "
                    f"need {taker_required:.2f} {taker_quote}, "
                    f"have {taker_available:.2f}. "
                    f"Skipping new executor."
                )
                return False
            return True

        except Exception as exc:
            self.logger().error(f"[Margin Gate] Error checking margin: {exc}. Allowing executor creation.")
            # Fail-open to avoid blocking legitimate trading; executor's own balance check will catch it
            return True

    # -----------------------------------------------------------------------
    # Executor creation
    # -----------------------------------------------------------------------

    def _build_executor_config(
        self,
        maker_side: TradeType,
        target_profitability: Decimal,
        order_amount_base: Decimal,
    ) -> PerpXEMMExecutorConfig:
        """Build a PerpXEMMExecutorConfig for a single level."""
        min_profitability = target_profitability - self.config.min_profitability
        max_profitability = target_profitability + self.config.max_profitability

        if maker_side == TradeType.BUY:
            buying_market = ConnectorPair(
                connector_name=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
            )
            selling_market = ConnectorPair(
                connector_name=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
            )
        else:
            # maker SELL: buying_market is taker side
            buying_market = ConnectorPair(
                connector_name=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair,
            )
            selling_market = ConnectorPair(
                connector_name=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair,
            )

        return PerpXEMMExecutorConfig(
            controller_id=self.config.id,
            timestamp=self.market_data_provider.time(),
            buying_market=buying_market,
            selling_market=selling_market,
            maker_side=maker_side,
            order_amount=order_amount_base,
            min_profitability=min_profitability,
            target_profitability=target_profitability,
            max_profitability=max_profitability,
            # Perpetual-specific fields
            maker_leverage=self.config.maker_leverage,
            taker_leverage=self.config.taker_leverage,
            position_mode=self.config.position_mode,
            maker_funding_interval_s=self.config.maker_funding_interval_s,
            taker_funding_interval_s=self.config.taker_funding_interval_s,
            pre_funding_window_s=self.config.pre_funding_window_s,
            margin_buffer_pct=self.config.margin_buffer_pct,
        )

    def determine_executor_actions(self) -> List[ExecutorAction]:
        executor_actions: List[ExecutorAction] = []

        # Do not create executors until leverage setup is confirmed
        if not self._leverage_ready:
            self.logger().info(
                "[Perp XEMM] Waiting for leverage/position-mode setup to complete …"
            )
            return executor_actions

        mid_price: Optional[Decimal] = self.market_data_provider.get_price_by_type(
            self.config.maker_connector,
            self.config.maker_trading_pair,
            PriceType.MidPrice,
        )
        if mid_price is None or mid_price.is_nan() or mid_price <= 0:
            self.logger().warning(
                f"[Perp XEMM] Mid price unavailable for "
                f"{self.config.maker_trading_pair} on {self.config.maker_connector}; skipping."
            )
            return executor_actions

        active_buy_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda e: not e.is_done and e.config.maker_side == TradeType.BUY,
        )
        active_sell_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda e: not e.is_done and e.config.maker_side == TradeType.SELL,
        )
        filled_buy_count = self._count_filled_executors(TradeType.BUY)
        filled_sell_count = self._count_filled_executors(TradeType.SELL)
        fill_imbalance = filled_buy_count - filled_sell_count

        total_buy_amount = sum(amt for _, amt in self.buy_levels_targets_amount) or Decimal("1")
        total_sell_amount = sum(amt for _, amt in self.sell_levels_targets_amount) or Decimal("1")

        buy_side_quote = self.config.total_amount_quote * Decimal("0.5")
        sell_side_quote = self.config.total_amount_quote * Decimal("0.5")
        avg_level_quote = buy_side_quote / total_buy_amount

        inventory_bias, buy_scale, sell_scale = self._inventory_skew_adjustment(
            mid_price=mid_price,
            per_level_quote=avg_level_quote,
        )
        effective_imbalance = fill_imbalance + inventory_bias

        self._log_imbalance_if_changed(
            fill_imbalance, inventory_bias, effective_imbalance, buy_scale, sell_scale
        )

        # --- Buy levels ---
        for target_profitability, level_amount in self.buy_levels_targets_amount:
            has_active = any(
                e.config.target_profitability == target_profitability
                for e in active_buy_executors
            )
            if has_active or effective_imbalance >= self.config.max_executors_imbalance:
                continue

            proportional_quote = (level_amount / total_buy_amount) * buy_side_quote * buy_scale
            order_amount_base = proportional_quote / mid_price

            # Controller-level margin gate
            if not self._has_sufficient_margin(proportional_quote):
                continue

            config = self._build_executor_config(
                maker_side=TradeType.BUY,
                target_profitability=target_profitability,
                order_amount_base=order_amount_base,
            )
            executor_actions.append(
                CreateExecutorAction(executor_config=config, controller_id=self.config.id)
            )

        # --- Sell levels ---
        for target_profitability, level_amount in self.sell_levels_targets_amount:
            has_active = any(
                e.config.target_profitability == target_profitability
                for e in active_sell_executors
            )
            if has_active or effective_imbalance <= -self.config.max_executors_imbalance:
                continue

            proportional_quote = (level_amount / total_sell_amount) * sell_side_quote * sell_scale
            order_amount_base = proportional_quote / mid_price

            # Controller-level margin gate
            if not self._has_sufficient_margin(proportional_quote):
                continue

            config = self._build_executor_config(
                maker_side=TradeType.SELL,
                target_profitability=target_profitability,
                order_amount_base=order_amount_base,
            )
            executor_actions.append(
                CreateExecutorAction(executor_config=config, controller_id=self.config.id)
            )

        if executor_actions:
            self.logger().info(
                f"[Perp XEMM] Proposing {len(executor_actions)} executor(s) for "
                f"{self.config.maker_trading_pair} (mid={mid_price:.4f}, "
                f"maker_lev={self.config.maker_leverage}x, taker_lev={self.config.taker_leverage}x)."
            )
        return executor_actions

    def to_format_status(self) -> List[str]:
        lev_status = (
            f"Leverage ready: maker={'✓' if self._maker_leverage_ready else '✗'} "
            f"taker={'✓' if self._taker_leverage_ready else '✗'} | "
            f"maker_lev={self.config.maker_leverage}x taker_lev={self.config.taker_leverage}x | "
            f"position_mode={self.config.position_mode.name}"
        )
        rows = [lev_status]
        if self._leverage_ready:
            try:
                maker_base, taker_base = self._read_leg_positions()
                rows.append(
                    f"Positions: maker={maker_base} base | taker={taker_base} base | "
                    f"drift={maker_base + taker_base} base"
                )
            except Exception:
                pass
        if self.executors_info:
            all_executors_df = pd.DataFrame(e.custom_info for e in self.executors_info)
            rows.append(format_df_for_printout(all_executors_df, table_format="psql"))
        return rows
