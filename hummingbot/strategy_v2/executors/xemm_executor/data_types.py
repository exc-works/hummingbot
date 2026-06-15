from decimal import Decimal
from typing import Literal

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, ExecutorConfigBase


class XEMMExecutorConfig(ExecutorConfigBase):
    type: Literal["xemm_executor"] = "xemm_executor"
    buying_market: ConnectorPair
    selling_market: ConnectorPair
    maker_side: TradeType
    order_amount: Decimal
    min_profitability: Decimal
    target_profitability: Decimal
    max_profitability: Decimal
    max_hedge_slippage_bps: Decimal = Decimal("0")
    max_hedge_order_base: Decimal = Decimal("0")
    min_hedge_notional: Decimal = Decimal("0")
    min_depth_notional: Decimal = Decimal("0")
    reserve_taker_base: Decimal = Decimal("0")
    reserve_taker_quote: Decimal = Decimal("0")
