from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from hummingbot.strategy_v2.executors.xemm_executor.hedge_exposure import HedgeExposureTracker


class TestHedgeExposureTracker(TestCase):
    def test_reconciles_fill_sum_and_floor_without_double_counting(self):
        tracker = HedgeExposureTracker()

        tracker.add_maker_fill(Decimal("1"))
        tracker.raise_maker_filled_floor(Decimal("0.5"))

        self.assertEqual(tracker.maker_filled_base, Decimal("1"))

        tracker.raise_maker_filled_floor(Decimal("1.5"))

        self.assertEqual(tracker.maker_filled_base, Decimal("1.5"))

    def test_tracks_submitted_and_actual_hedge_separately(self):
        tracker = HedgeExposureTracker()
        tracker.add_maker_fill(Decimal("2"))
        tracker.reserve_hedge(Decimal("1.5"))

        self.assertEqual(tracker.unsubmitted_hedge_base(), Decimal("0.5"))

        taker_orders = [
            SimpleNamespace(executed_amount_base=Decimal("1")),
            SimpleNamespace(executed_amount_base=Decimal("0.25")),
        ]

        self.assertEqual(tracker.actual_hedged_base(taker_orders), Decimal("1.25"))
        self.assertEqual(tracker.unhedged_base(taker_orders), Decimal("0.75"))

        tracker.release_hedge(Decimal("0.5"))

        self.assertEqual(tracker.submitted_hedge_base, Decimal("1"))
