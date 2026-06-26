from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from hummingbot.core.data_type.common import PriceType
from hummingbot.strategy_v2.utils.xemm_sizing_price import resolve_xemm_sizing_price


class TestXEMMSizingPrice(TestCase):
    def _prices(self, mapping):
        def get_price_by_type(connector, trading_pair, price_type):
            return mapping.get((connector, trading_pair, price_type))
        return get_price_by_type

    def test_require_maker_book_uses_maker_mid(self):
        prices = self._prices({
            ("caishen_spot", "AAVE-USDC", PriceType.MidPrice): Decimal("100"),
            ("okx", "AAVE-USDT", PriceType.MidPrice): Decimal("200"),
        })
        price, source = resolve_xemm_sizing_price(
            prices, "caishen_spot", "AAVE-USDC", "okx", "AAVE-USDT",
            require_maker_order_book=True,
        )
        self.assertEqual(price, Decimal("100"))
        self.assertEqual(source, "maker MidPrice")

    def test_require_maker_book_ignores_taker_when_maker_empty(self):
        prices = self._prices({
            ("caishen_spot", "AAVE-USDC", PriceType.MidPrice): Decimal("NaN"),
            ("okx", "AAVE-USDT", PriceType.MidPrice): Decimal("200"),
        })
        price, source = resolve_xemm_sizing_price(
            prices, "caishen_spot", "AAVE-USDC", "okx", "AAVE-USDT",
            require_maker_order_book=True,
        )
        self.assertIsNone(price)
        self.assertIsNone(source)

    def test_taker_only_mode_ignores_maker_book(self):
        prices = self._prices({
            ("caishen_spot", "AAVE-USDC", PriceType.MidPrice): Decimal("100"),
            ("okx", "AAVE-USDT", PriceType.MidPrice): Decimal("200"),
        })
        with patch(
            "hummingbot.strategy_v2.utils.xemm_sizing_price.RateOracle.get_instance"
        ) as mock_oracle:
            mock_oracle.return_value.get_pair_rate.return_value = Decimal("1")
            price, source = resolve_xemm_sizing_price(
                prices, "caishen_spot", "AAVE-USDC", "okx", "AAVE-USDT",
                require_maker_order_book=False,
            )
        self.assertEqual(price, Decimal("200"))
        self.assertEqual(source, "taker MidPrice")

    def test_taker_only_mode_falls_back_when_maker_empty(self):
        prices = self._prices({
            ("caishen_spot", "AAVE-USDC", PriceType.MidPrice): Decimal("NaN"),
            ("okx", "AAVE-USDT", PriceType.MidPrice): Decimal("150"),
        })
        price, source = resolve_xemm_sizing_price(
            prices, "caishen_spot", "AAVE-USDC", "okx", "AAVE-USDT",
            require_maker_order_book=False,
        )
        self.assertEqual(price, Decimal("150"))
        self.assertEqual(source, "taker MidPrice")
