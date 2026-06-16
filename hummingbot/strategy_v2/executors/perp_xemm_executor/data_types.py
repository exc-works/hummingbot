from decimal import Decimal
from typing import Literal

from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, ExecutorConfigBase

# Default funding payment interval in seconds (8 hours, most common for major perpetual exchanges)
DEFAULT_FUNDING_PAYMENT_INTERVAL_S: int = 8 * 60 * 60

# Seconds before next funding settlement to enter the "pre-funding window"
DEFAULT_PRE_FUNDING_WINDOW_S: int = 10 * 60  # 10 minutes before settlement


class PerpXEMMExecutorConfig(ExecutorConfigBase):
    """
    Configuration for the Perp XEMM executor.

    Extends XEMMExecutorConfig with perpetual-specific fields:
    - maker_leverage / taker_leverage: per-leg leverage (default 1 = no leverage, safest)
    - position_mode: ONEWAY recommended for simplicity
    - funding_payment_interval_s: each leg's funding settlement interval (seconds)
    - pre_funding_window_s: seconds before settlement to enter the cautious zone
    - max_taker_slippage_pct: abort emergency close if taker slippage exceeds this
    - margin_buffer_pct: required free margin buffer above base requirement (isolated margin)
    """
    type: Literal["perp_xemm_executor"] = "perp_xemm_executor"

    # Market config (same as XEMMExecutorConfig)
    buying_market: ConnectorPair
    selling_market: ConnectorPair
    maker_side: TradeType
    order_amount: Decimal

    # Profitability thresholds (same as XEMMExecutorConfig)
    min_profitability: Decimal
    target_profitability: Decimal
    max_profitability: Decimal

    # --- Perpetual-specific fields ---

    # Per-leg leverage. Default 1 (isolated/no leverage). Increase with caution.
    maker_leverage: int = 1
    taker_leverage: int = 1

    # Position mode. ONEWAY simplifies position key management.
    position_mode: PositionMode = PositionMode.ONEWAY

    # Funding rate parameters
    # Interval (seconds) for funding payments on each leg (e.g. 28800 = 8h, 14400 = 4h, 3600 = 1h).
    maker_funding_interval_s: int = DEFAULT_FUNDING_PAYMENT_INTERVAL_S
    taker_funding_interval_s: int = DEFAULT_FUNDING_PAYMENT_INTERVAL_S

    # Seconds before next settlement to apply the pre-funding logic
    pre_funding_window_s: int = DEFAULT_PRE_FUNDING_WINDOW_S

    # Required free margin as a multiple of the base requirement.
    # 1.0 means 2x the minimum required margin must be available (important for isolated margin).
    margin_buffer_pct: Decimal = Decimal("1.0")

    # Max allowed taker slippage for the emergency close order.
    # If the emergency close cannot be placed within this slippage, log critical and stop.
    max_emergency_slippage_pct: Decimal = Decimal("0.05")
