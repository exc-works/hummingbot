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
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import PerpetualOrderCandidate
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.perp_xemm_executor.data_types import (
    DEFAULT_FUNDING_PAYMENT_INTERVAL_S,
    DEFAULT_PRE_FUNDING_WINDOW_S,
    PerpXEMMExecutorConfig,
)
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


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

    # Margin buffer: caps total side exposure at total_amount_quote/2 * (1 + margin_buffer_pct)
    # in margin terms; per-order checks use incremental nominal/leverage vs available.
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

    # Funding settlement control: compare combined net funding (bps) vs round-trip fee bps
    funding_settlement_control_enabled: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Enable funding settlement eval/flatten (true/false): ",
            "prompt_on_new": True,
        },
    )
    funding_settlement_eval_window_s: int = Field(
        default=300,
        json_schema_extra={
            "prompt": "Seconds before funding settlement to evaluate (e.g. 300=5min): ",
            "prompt_on_new": True,
        },
    )
    funding_flatten_cost_bps: Decimal = Field(
        default=Decimal("12"),
        json_schema_extra={
            "prompt": "Round-trip close+reopen cost in bps (default 12): ",
            "prompt_on_new": True,
        },
    )
    funding_post_settlement_resume_s: int = Field(
        default=30,
        json_schema_extra={
            "prompt": "Seconds after settlement before resuming new quotes: ",
            "prompt_on_new": True,
        },
    )

    # Flatten when exchange positions are unchanged for too long (resume quoting immediately)
    position_stale_control_enabled: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": "Enable stale-position flatten (true/false): ",
            "prompt_on_new": True,
        },
    )
    max_position_idle_s: int = Field(
        default=4 * 60 * 60,
        json_schema_extra={
            "prompt": "Seconds with unchanged positions before flatten (14400=4h): ",
            "prompt_on_new": True,
        },
    )
    position_stale_min_notional_quote: Decimal = Field(
        default=Decimal("50"),
        json_schema_extra={
            "prompt": "Min |leg| notional to apply stale flatten (0=any non-flat): ",
            "prompt_on_new": True,
        },
    )

    # After an executor at a level finishes (fill+hedge, cancel, or stop), wait before
    # spawning a replacement at the same side+target_profitability. Reduces churn and
    # Caishen 1139 cancel noise when orders fill instantly after manual flatten/restart.
    level_requote_cooldown_s: float = Field(
        default=10.0,
        ge=0,
        json_schema_extra={
            "prompt": "Seconds to wait before re-quoting a level after its executor ends (0=off): ",
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
        self._last_imbalance_log_key: Optional[int] = None
        self._funding_pause_until: float = 0.0
        self._funding_evaluated_settlement_ts: Optional[float] = None
        self._last_funding_control_log_key: Optional[tuple] = None
        self._last_position_snapshot: Optional[tuple[Decimal, Decimal]] = None
        self._last_position_change_ts: Optional[float] = None
        self._position_stale_flatten_dispatched: bool = False
        self._margin_gate_last_warn_key: Optional[tuple] = None
        self._margin_gate_last_warn_ts: float = 0.0
        self._level_cooldown_last_log_key: Optional[tuple] = None

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
    # Exchange position
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
        amount = position.amount
        # Caishen / Hyperliquid store signed amount (short < 0). Other connectors may
        # store unsigned amount with position_side — avoid double-negating the former.
        if amount < 0:
            return amount
        if position.position_side == PositionSide.SHORT:
            return -abs(amount)
        return abs(amount)

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
            maker_side = "flat"
            if maker_base > 0:
                maker_side = "long"
            elif maker_base < 0:
                maker_side = "short"
            self.logger().info(
                f"[Perp XEMM] Startup positions — "
                f"maker {self.config.maker_connector} {self.config.maker_trading_pair}: "
                f"{maker_base} base ({maker_side})"
                f"{f' (~{maker_base * mid:.2f} quote)' if mid > 0 else ''}; "
                f"taker {self.config.taker_connector} {self.config.taker_trading_pair}: "
                f"{taker_base} base"
                f"{f' (~{taker_base * mid:.2f} quote)' if mid > 0 else ''}; "
                f"cross-leg drift (maker+taker): {drift_base} base."
            )
        except Exception as exc:
            self.logger().warning(f"[Perp XEMM] Could not read startup positions: {exc}.")
        finally:
            self._startup_positions_logged = True

    def _log_imbalance_if_changed(self, fill_imbalance: int):
        """Log fill imbalance only when it changes, not every control tick."""
        if fill_imbalance == self._last_imbalance_log_key:
            return
        self._last_imbalance_log_key = fill_imbalance
        if fill_imbalance == 0:
            self.logger().info("[Perp XEMM] Imbalance neutral (fill_delta=0).")
            return
        self.logger().info(
            f"[Perp XEMM] Imbalance: fill_delta={fill_imbalance} "
            f"(max=±{self.config.max_executors_imbalance})."
        )

    # -----------------------------------------------------------------------
    # Funding settlement control (bps compare vs round-trip cost)
    # -----------------------------------------------------------------------

    @staticmethod
    def _has_funding_info(connector, trading_pair: str) -> bool:
        perpetual_trading = getattr(connector, "_perpetual_trading", None)
        if perpetual_trading is None:
            return False
        return trading_pair in perpetual_trading._funding_info

    @staticmethod
    def _leg_funding_rate_contribution(position_base: Decimal, funding_rate: Decimal) -> Decimal:
        """
        Signed funding rate contribution for one leg (rate is per that leg's funding period).
        Positive rate: longs pay, shorts receive.
        """
        if position_base == 0 or funding_rate == 0:
            return Decimal("0")
        if position_base > 0:
            return funding_rate
        return -funding_rate

    def _combined_net_funding_bps(
        self,
        maker_base: Decimal,
        taker_base: Decimal,
        maker_rate: Decimal,
        taker_rate: Decimal,
    ) -> Decimal:
        """Combined net funding cost across both legs, in bps (>0 = net pay)."""
        net_rate = (
            self._leg_funding_rate_contribution(maker_base, maker_rate)
            + self._leg_funding_rate_contribution(taker_base, taker_rate)
        )
        return net_rate * Decimal("10000")

    def _nearest_funding_settlement(self) -> Optional[float]:
        try:
            maker_conn = self.market_data_provider.get_connector(self.config.maker_connector)
            taker_conn = self.market_data_provider.get_connector(self.config.taker_connector)
            if (not self._has_funding_info(maker_conn, self.config.maker_trading_pair)
                    or not self._has_funding_info(taker_conn, self.config.taker_trading_pair)):
                return None
            maker_next = maker_conn.get_funding_info(
                self.config.maker_trading_pair
            ).next_funding_utc_timestamp
            taker_next = taker_conn.get_funding_info(
                self.config.taker_trading_pair
            ).next_funding_utc_timestamp
            return min(maker_next, taker_next)
        except Exception:
            return None

    def _is_funding_quoting_paused(self) -> bool:
        return self.market_data_provider.time() < self._funding_pause_until

    @staticmethod
    def _is_perp_xemm_executor(executor_info) -> bool:
        """True only for XEMM quote executors; leg-close uses OrderExecutorConfig."""
        return getattr(executor_info.config, "type", None) == "perp_xemm_executor"

    def _occupies_level(self, executor) -> bool:
        """
        True while a perp XEMM executor is actively quoting on a level.
        SHUTTING_DOWN executors no longer maintain maker orders; release the slot.
        """
        if not self._is_perp_xemm_executor(executor):
            return False
        return executor.status in (RunnableStatus.NOT_STARTED, RunnableStatus.RUNNING)

    def _stop_active_xemm_executors(self) -> List[StopExecutorAction]:
        return [
            StopExecutorAction(
                controller_id=self.config.id,
                executor_id=executor.id,
                keep_position=True,
            )
            for executor in self.executors_info
            if not executor.is_done and self._is_perp_xemm_executor(executor)
        ]

    def _build_leg_close_action(
        self,
        connector_name: str,
        trading_pair: str,
        position_base: Decimal,
        leverage: int,
    ) -> Optional[CreateExecutorAction]:
        if position_base == 0:
            return None
        connector = self.market_data_provider.get_connector(connector_name)
        close_side = TradeType.SELL if position_base > 0 else TradeType.BUY
        amount = connector.quantize_order_amount(trading_pair, abs(position_base))
        if amount <= 0:
            return None
        config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=connector_name,
            trading_pair=trading_pair,
            side=close_side,
            amount=amount,
            position_action=PositionAction.CLOSE,
            execution_strategy=ExecutionStrategy.MARKET,
            leverage=leverage,
        )
        return CreateExecutorAction(controller_id=self.config.id, executor_config=config)

    def _build_flatten_both_legs_actions(
        self,
        maker_base: Decimal,
        taker_base: Decimal,
    ) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []
        actions.extend(self._stop_active_xemm_executors())
        maker_close = self._build_leg_close_action(
            self.config.maker_connector,
            self.config.maker_trading_pair,
            maker_base,
            self.config.maker_leverage,
        )
        taker_close = self._build_leg_close_action(
            self.config.taker_connector,
            self.config.taker_trading_pair,
            taker_base,
            self.config.taker_leverage,
        )
        if maker_close:
            actions.append(maker_close)
        if taker_close:
            actions.append(taker_close)
        return actions

    def _log_funding_control_once(self, log_key: tuple, message: str):
        if log_key == self._last_funding_control_log_key:
            return
        self._last_funding_control_log_key = log_key
        self.logger().info(message)

    def _funding_settlement_control_actions(self) -> List[ExecutorAction]:
        """
        Within eval window before settlement:
          net <= 0 bps          -> no action
          0 < net <= cost bps   -> pause new quotes until settlement (no flatten)
          net > cost bps        -> flatten both legs + pause until settlement
        """
        if not self.config.funding_settlement_control_enabled:
            return []

        now = self.market_data_provider.time()
        next_settlement = self._nearest_funding_settlement()
        if next_settlement is None:
            return []

        secs_to_settle = next_settlement - now
        eval_window = self.config.funding_settlement_eval_window_s

        if secs_to_settle <= 0:
            if self._funding_evaluated_settlement_ts == next_settlement:
                self._funding_evaluated_settlement_ts = None
            return []

        if secs_to_settle > eval_window:
            return []

        if self._funding_evaluated_settlement_ts == next_settlement:
            return []

        try:
            maker_conn = self.market_data_provider.get_connector(self.config.maker_connector)
            taker_conn = self.market_data_provider.get_connector(self.config.taker_connector)
            maker_info = maker_conn.get_funding_info(self.config.maker_trading_pair)
            taker_info = taker_conn.get_funding_info(self.config.taker_trading_pair)
            maker_base, taker_base = self._read_leg_positions()
            net_bps = self._combined_net_funding_bps(
                maker_base=maker_base,
                taker_base=taker_base,
                maker_rate=maker_info.rate,
                taker_rate=taker_info.rate,
            )
        except Exception as exc:
            self.logger().warning(f"[Perp XEMM] Funding settlement eval failed: {exc}")
            return []

        self._funding_evaluated_settlement_ts = next_settlement
        self._funding_pause_until = (
            next_settlement + self.config.funding_post_settlement_resume_s
        )
        cost_bps = self.config.funding_flatten_cost_bps

        if net_bps <= 0:
            self._funding_pause_until = 0.0
            self._log_funding_control_once(
                (next_settlement, "favorable", float(net_bps)),
                f"[Perp XEMM] Funding eval: net {net_bps:.2f} bps (favorable), "
                f"settlement in {secs_to_settle:.0f}s — no action.",
            )
            return []

        actions: List[ExecutorAction] = []

        if net_bps > cost_bps:
            actions.extend(self._build_flatten_both_legs_actions(maker_base, taker_base))
            self._log_funding_control_once(
                (next_settlement, "flatten", float(net_bps)),
                f"[Perp XEMM] Funding eval: net {net_bps:.2f} bps > cost {cost_bps} bps, "
                f"settlement in {secs_to_settle:.0f}s — flatten both legs, pause quotes until "
                f"{self._funding_pause_until:.0f}.",
            )
        else:
            actions.extend(self._stop_active_xemm_executors())
            self._log_funding_control_once(
                (next_settlement, "pause", float(net_bps)),
                f"[Perp XEMM] Funding eval: 0 < net {net_bps:.2f} bps <= cost {cost_bps} bps, "
                f"settlement in {secs_to_settle:.0f}s — pause new quotes (no flatten).",
            )

        return actions

    # -----------------------------------------------------------------------
    # Stale position control (unchanged positions → flatten, resume immediately)
    # -----------------------------------------------------------------------

    def _track_position_changes(self) -> tuple[Decimal, Decimal]:
        maker_base, taker_base = self._read_leg_positions()
        snapshot = (maker_base, taker_base)
        now = self.market_data_provider.time()

        if self._last_position_snapshot is None:
            self._last_position_snapshot = snapshot
            self._last_position_change_ts = now
        elif snapshot != self._last_position_snapshot:
            self._last_position_snapshot = snapshot
            self._last_position_change_ts = now
            self._position_stale_flatten_dispatched = False

        if maker_base == 0 and taker_base == 0:
            self._position_stale_flatten_dispatched = False

        return maker_base, taker_base

    def _position_has_stale_exposure(
        self,
        maker_base: Decimal,
        taker_base: Decimal,
        mid_price: Decimal,
    ) -> bool:
        min_notional = self.config.position_stale_min_notional_quote
        if min_notional <= 0:
            return maker_base != 0 or taker_base != 0
        if mid_price.is_nan() or mid_price <= 0:
            return maker_base != 0 or taker_base != 0
        try:
            maker_notional = abs(maker_base * mid_price)
            taker_notional = abs(taker_base * mid_price)
        except Exception:
            return maker_base != 0 or taker_base != 0
        return maker_notional >= min_notional or taker_notional >= min_notional

    def _safe_mid_price(self) -> Optional[Decimal]:
        mid = self.market_data_provider.get_price_by_type(
            self.config.maker_connector,
            self.config.maker_trading_pair,
            PriceType.MidPrice,
        )
        if mid is None or mid.is_nan() or mid <= 0:
            return None
        return mid

    def _position_stale_control_actions(self) -> List[ExecutorAction]:
        if not self.config.position_stale_control_enabled:
            return []

        try:
            maker_base, taker_base = self._track_position_changes()
            mid_price = self._safe_mid_price()
            if mid_price is None:
                return []

            if not self._position_has_stale_exposure(maker_base, taker_base, mid_price):
                return []

            if self._position_stale_flatten_dispatched:
                return []

            if self._last_position_change_ts is None:
                return []

            idle_s = self.market_data_provider.time() - self._last_position_change_ts
            if idle_s < self.config.max_position_idle_s:
                return []

            self._position_stale_flatten_dispatched = True
            self.logger().info(
                f"[Perp XEMM] Stale position flatten after {idle_s / 3600:.1f}h unchanged "
                f"(maker={maker_base}, taker={taker_base})."
            )
            return self._build_flatten_both_legs_actions(maker_base, taker_base)
        except Exception:
            return []

    # -----------------------------------------------------------------------
    # Margin gate helper
    # -----------------------------------------------------------------------

    def _counts_toward_fill_imbalance(self, executor) -> bool:
        """
        True for executors with maker fills that still have in-flight hedge exposure.
        TERMINATED executors are excluded so fill_delta reflects open imbalance only.
        """
        return (
            self._is_perp_xemm_executor(executor)
            and not executor.is_done
            and executor.filled_amount_quote > Decimal("0")
        )

    def _count_filled_executors(self, maker_side: TradeType) -> int:
        return len([
            e for e in self.executors_info
            if self._counts_toward_fill_imbalance(e)
            and e.config.maker_side == maker_side
        ])

    def _log_margin_gate_skip(
        self,
        leg: str,
        quote: str,
        required: Decimal,
        available: Decimal,
        wallet: Decimal,
        nominal_quote: Decimal,
    ) -> None:
        """Rate-limited margin gate warning with wallet vs available breakdown."""
        warn_key = (leg, f"{required:.2f}", f"{available:.2f}", f"{wallet:.2f}")
        now = self.market_data_provider.time()
        if (
            warn_key == self._margin_gate_last_warn_key
            and now - self._margin_gate_last_warn_ts < 30
        ):
            return
        self._margin_gate_last_warn_key = warn_key
        self._margin_gate_last_warn_ts = now
        frozen = max(wallet - available, Decimal("0"))
        self.logger().warning(
            f"[Margin Gate] {leg} margin insufficient for ~{nominal_quote:.0f} {quote} nominal: "
            f"need {required:.2f}, available {available:.2f} "
            f"(wallet {wallet:.2f}, frozen ~{frozen:.2f}). Skipping new executor."
        )

    def _has_sufficient_margin(
        self,
        nominal_quote: Decimal,
        maker_side: TradeType,
        order_amount_base: Decimal,
        mid_price: Decimal,
    ) -> bool:
        """
        Controller-level margin gate using the exchange budget checker only.

        Relies on connector ``get_available_balance`` (Caishen reconciles stale order
        freeze after network/cancel glitches).
        """
        try:
            maker_conn = self.market_data_provider.get_connector(self.config.maker_connector)
            taker_conn = self.market_data_provider.get_connector(self.config.taker_connector)
            _, maker_quote = self.config.maker_trading_pair.split("-")
            _, taker_quote = self.config.taker_trading_pair.split("-")

            maker_wallet = maker_conn.get_balance(maker_quote)
            maker_available = maker_conn.get_available_balance(maker_quote)
            taker_wallet = taker_conn.get_balance(taker_quote)
            taker_available = taker_conn.get_available_balance(taker_quote)

            taker_order_side = (
                TradeType.SELL if maker_side == TradeType.BUY else TradeType.BUY
            )
            maker_candidate = PerpetualOrderCandidate(
                trading_pair=self.config.maker_trading_pair,
                is_maker=True,
                order_type=OrderType.LIMIT,
                order_side=maker_side,
                amount=order_amount_base,
                price=mid_price,
                leverage=Decimal(str(self.config.maker_leverage)),
                position_close=False,
            )
            taker_candidate = PerpetualOrderCandidate(
                trading_pair=self.config.taker_trading_pair,
                is_maker=False,
                order_type=OrderType.MARKET,
                order_side=taker_order_side,
                amount=order_amount_base,
                price=mid_price,
                leverage=Decimal(str(self.config.taker_leverage)),
                position_close=False,
            )
            maker_adj = maker_conn.budget_checker.adjust_candidates([maker_candidate])[0]
            taker_adj = taker_conn.budget_checker.adjust_candidates([taker_candidate])[0]

            if maker_adj.amount <= Decimal("0"):
                required = nominal_quote / Decimal(str(self.config.maker_leverage))
                self._log_margin_gate_skip(
                    "Maker", maker_quote, required, maker_available, maker_wallet, nominal_quote,
                )
                return False
            if taker_adj.amount <= Decimal("0"):
                required = nominal_quote / Decimal(str(self.config.taker_leverage))
                self._log_margin_gate_skip(
                    "Taker", taker_quote, required, taker_available, taker_wallet, nominal_quote,
                )
                return False
            return True

        except Exception as exc:
            self.logger().error(f"[Margin Gate] Error checking margin: {exc}. Allowing executor creation.")
            return True

    # -----------------------------------------------------------------------
    # Executor creation
    # -----------------------------------------------------------------------

    def _level_in_requote_cooldown(
        self,
        maker_side: TradeType,
        target_profitability: Decimal,
    ) -> bool:
        cooldown = self.config.level_requote_cooldown_s
        if cooldown <= 0:
            return False
        now = self.market_data_provider.time()
        for executor in self.executors_info:
            if not self._is_perp_xemm_executor(executor):
                continue
            if executor.config.maker_side != maker_side:
                continue
            if executor.config.target_profitability != target_profitability:
                continue
            if not executor.is_done or executor.close_timestamp is None:
                continue
            if now - executor.close_timestamp < cooldown:
                return True
        return False

    def _log_level_cooldown_skip(
        self,
        maker_side: TradeType,
        target_profitability: Decimal,
    ):
        log_key = (maker_side, target_profitability)
        if self._level_cooldown_last_log_key == log_key:
            return
        self._level_cooldown_last_log_key = log_key
        self.logger().info(
            f"[Perp XEMM] Level cooldown ({self.config.level_requote_cooldown_s:.0f}s): "
            f"skip {maker_side.name} target={target_profitability} re-quote."
        )

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

        funding_actions = self._funding_settlement_control_actions()
        if funding_actions:
            return funding_actions

        if self._is_funding_quoting_paused():
            return executor_actions

        stale_actions = self._position_stale_control_actions()
        if stale_actions:
            return stale_actions

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
            filter_func=lambda e: (
                self._occupies_level(e) and e.config.maker_side == TradeType.BUY
            ),
        )
        active_sell_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda e: (
                self._occupies_level(e) and e.config.maker_side == TradeType.SELL
            ),
        )
        filled_buy_count = self._count_filled_executors(TradeType.BUY)
        filled_sell_count = self._count_filled_executors(TradeType.SELL)
        fill_imbalance = filled_buy_count - filled_sell_count

        total_buy_amount = sum(amt for _, amt in self.buy_levels_targets_amount) or Decimal("1")
        total_sell_amount = sum(amt for _, amt in self.sell_levels_targets_amount) or Decimal("1")

        buy_side_quote = self.config.total_amount_quote * Decimal("0.5")
        sell_side_quote = self.config.total_amount_quote * Decimal("0.5")

        self._log_imbalance_if_changed(fill_imbalance)

        # --- Buy levels ---
        for target_profitability, level_amount in self.buy_levels_targets_amount:
            has_active = any(
                e.config.target_profitability == target_profitability
                for e in active_buy_executors
            )
            if has_active or fill_imbalance >= self.config.max_executors_imbalance:
                continue
            if self._level_in_requote_cooldown(TradeType.BUY, target_profitability):
                self._log_level_cooldown_skip(TradeType.BUY, target_profitability)
                continue

            proportional_quote = (level_amount / total_buy_amount) * buy_side_quote
            order_amount_base = proportional_quote / mid_price

            # Controller-level margin gate
            if not self._has_sufficient_margin(
                proportional_quote, TradeType.BUY, order_amount_base, mid_price
            ):
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
            if has_active or fill_imbalance <= -self.config.max_executors_imbalance:
                continue
            if self._level_in_requote_cooldown(TradeType.SELL, target_profitability):
                self._log_level_cooldown_skip(TradeType.SELL, target_profitability)
                continue

            proportional_quote = (level_amount / total_sell_amount) * sell_side_quote
            order_amount_base = proportional_quote / mid_price

            # Controller-level margin gate
            if not self._has_sufficient_margin(
                proportional_quote, TradeType.SELL, order_amount_base, mid_price
            ):
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
        if self._is_funding_quoting_paused():
            rows.append(
                f"Funding pause: quoting suspended until "
                f"{self._funding_pause_until:.0f} (now={self.market_data_provider.time():.0f})"
            )
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
