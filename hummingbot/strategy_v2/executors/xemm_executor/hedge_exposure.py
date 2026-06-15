from decimal import Decimal
from typing import Iterable


class HedgeExposureTracker:
    """
    Tracks maker fills and hedge submissions as a small ledger.

    Maker fills are reconciled from two sources:
    - fill events, summed after trade-id deduplication
    - completed/tracked order totals, used as a floor for event gaps or races

    Hedge submissions are tracked separately from actual taker fills. This mirrors
    BBGO's covered-position idea: submitted hedge quantity prevents duplicate
    orders, while actual fills still decide when the executor can safely finish.
    """

    def __init__(self):
        self.maker_fills_sum = Decimal("0")
        self.maker_filled_floor = Decimal("0")
        self.submitted_hedge_base = Decimal("0")

    @property
    def maker_filled_base(self) -> Decimal:
        return max(self.maker_fills_sum, self.maker_filled_floor)

    def add_maker_fill(self, amount: Decimal):
        self.maker_fills_sum += amount

    def raise_maker_filled_floor(self, amount: Decimal):
        self.maker_filled_floor = max(self.maker_filled_floor, amount)

    def unsubmitted_hedge_base(self) -> Decimal:
        return max(self.maker_filled_base - self.submitted_hedge_base, Decimal("0"))

    def reserve_hedge(self, amount: Decimal):
        self.submitted_hedge_base += amount

    def release_hedge(self, amount: Decimal):
        self.submitted_hedge_base = max(self.submitted_hedge_base - amount, Decimal("0"))

    def actual_hedged_base(self, taker_orders: Iterable) -> Decimal:
        return sum((t.executed_amount_base for t in taker_orders), Decimal("0"))

    def unhedged_base(self, taker_orders: Iterable) -> Decimal:
        return max(self.maker_filled_base - self.actual_hedged_base(taker_orders), Decimal("0"))
