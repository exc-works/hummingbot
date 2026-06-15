from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock

from controllers.generic.xemm_multiple_levels import XEMMMultipleLevels, XEMMMultipleLevelsConfig
from hummingbot.core.data_type.common import PriceType, TradeType
from hummingbot.strategy_v2.executors.xemm_executor.risk import can_hedge


class TestXEMMRisk(TestCase):
    def setUp(self):
        self.market_data_provider = MagicMock()
        self.market_data_provider.get_price_by_type.return_value = Decimal("100")
        self.market_data_provider.get_vwap_for_volume.return_value = SimpleNamespace(result_price=Decimal("101"))

        def balance(connector_name, asset):
            balances = {"ETH": Decimal("10"), "USDT": Decimal("2000")}
            return balances[asset]

        self.market_data_provider.get_available_balance.side_effect = balance

    def test_can_hedge_sell_with_sufficient_base(self):
        capability = can_hedge(
            market_data_provider=self.market_data_provider,
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            side=TradeType.SELL,
            amount=Decimal("2"),
        )

        self.assertTrue(capability.allowed)
        self.assertEqual(capability.max_amount, Decimal("2"))
        self.market_data_provider.get_price_by_type.assert_called_with(
            "hyperliquid", "ETH-USDT", PriceType.MidPrice
        )

    def test_can_hedge_buy_uses_quote_balance_and_reserve(self):
        capability = can_hedge(
            market_data_provider=self.market_data_provider,
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            side=TradeType.BUY,
            amount=Decimal("16"),
            reserve_quote=Decimal("500"),
        )

        self.assertFalse(capability.allowed)
        self.assertEqual(capability.max_amount, Decimal("15"))
        self.assertEqual(capability.reason, "partial hedge capacity")

    def test_can_hedge_with_balance_only_risk_does_not_require_depth(self):
        self.market_data_provider.get_vwap_for_volume.side_effect = AssertionError("depth should not be queried")

        capability = can_hedge(
            market_data_provider=self.market_data_provider,
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            side=TradeType.SELL,
            amount=Decimal("1"),
            reserve_base=Decimal("1"),
        )

        self.assertTrue(capability.allowed)
        self.assertEqual(capability.estimated_price, Decimal("100"))
        self.market_data_provider.get_vwap_for_volume.assert_not_called()

    def test_can_hedge_blocks_slippage_over_limit(self):
        capability = can_hedge(
            market_data_provider=self.market_data_provider,
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            side=TradeType.BUY,
            amount=Decimal("1"),
            max_slippage_bps=Decimal("50"),
        )

        self.assertFalse(capability.allowed)
        self.assertIn("slippage", capability.reason)
        self.assertEqual(capability.slippage_bps, Decimal("100.00"))

    def test_can_hedge_blocks_missing_depth_when_required(self):
        self.market_data_provider.get_vwap_for_volume.return_value = SimpleNamespace(result_price=None)

        capability = can_hedge(
            market_data_provider=self.market_data_provider,
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            side=TradeType.SELL,
            amount=Decimal("1"),
            min_depth_notional=Decimal("1"),
        )

        self.assertFalse(capability.allowed)
        self.assertEqual(capability.reason, "insufficient order book depth")

    def test_can_hedge_blocks_partial_order_book_depth(self):
        self.market_data_provider.get_vwap_for_volume.return_value = SimpleNamespace(
            result_price=Decimal("100"),
            result_volume=Decimal("0.5"),
        )

        capability = can_hedge(
            market_data_provider=self.market_data_provider,
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            side=TradeType.SELL,
            amount=Decimal("1"),
            min_depth_notional=Decimal("1"),
        )

        self.assertFalse(capability.allowed)
        self.assertEqual(capability.max_amount, Decimal("0.5"))
        self.assertEqual(capability.reason, "insufficient order book depth")

    def test_can_hedge_treats_missing_balance_as_insufficient(self):
        self.market_data_provider.get_available_balance.side_effect = lambda connector_name, asset: None

        capability = can_hedge(
            market_data_provider=self.market_data_provider,
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            side=TradeType.SELL,
            amount=Decimal("1"),
        )

        self.assertFalse(capability.allowed)
        self.assertEqual(capability.reason, "insufficient hedge balance")

    def test_max_order_base_limits_depth_check_not_total_capacity(self):
        self.market_data_provider.get_vwap_for_volume.return_value = SimpleNamespace(
            result_price=Decimal("100"),
            result_volume=Decimal("1"),
        )

        capability = can_hedge(
            market_data_provider=self.market_data_provider,
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            side=TradeType.SELL,
            amount=Decimal("3"),
            max_order_base=Decimal("1"),
            max_slippage_bps=Decimal("1"),
        )

        self.assertTrue(capability.allowed)
        self.assertEqual(capability.max_amount, Decimal("3"))
        self.market_data_provider.get_vwap_for_volume.assert_called_with(
            connector_name="hyperliquid",
            trading_pair="ETH-USDT",
            volume=1.0,
            is_buy=False,
        )

    def test_controller_default_risk_disabled_does_not_precheck_taker_market(self):
        config = XEMMMultipleLevelsConfig(
            id="xemm_test",
            maker_connector="okx",
            maker_trading_pair="ETH-USDT",
            taker_connector="hyperliquid",
            taker_trading_pair="ETH-USDT",
        )
        controller = object.__new__(XEMMMultipleLevels)
        controller.config = config
        controller.executors_info = []
        controller.market_data_provider = MagicMock()

        capability = controller._check_risk_for_executor(
            maker_side=TradeType.BUY,
            amount=Decimal("1"),
        )

        self.assertTrue(capability.allowed)
        self.assertEqual(capability.max_amount, Decimal("1"))
        controller.market_data_provider.get_price_by_type.assert_not_called()
        controller.market_data_provider.get_vwap_for_volume.assert_not_called()
