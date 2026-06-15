from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from test.logger_mixin_for_test import LoggerMixinForTest
from unittest.mock import MagicMock, Mock, PropertyMock, patch

from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderFilledEvent,
)
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.xemm_executor.data_types import XEMMExecutorConfig
from hummingbot.strategy_v2.executors.xemm_executor.xemm_executor import XEMMExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder


class TestXEMMExecutor(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    def setUp(self):
        super().setUp()
        self.strategy = self.create_mock_strategy()
        self.xemm_base_config = self.base_config_long
        self.update_interval = 0.5
        self.executor = XEMMExecutor(self.strategy, self.xemm_base_config, self.update_interval)
        self.set_loggers(loggers=[self.executor.logger()])

    @property
    def base_config_long(self) -> XEMMExecutorConfig:
        return XEMMExecutorConfig(
            timestamp=1234,
            buying_market=ConnectorPair(connector_name='binance', trading_pair='ETH-USDT'),
            selling_market=ConnectorPair(connector_name='kucoin', trading_pair='ETH-USDT'),
            maker_side=TradeType.BUY,
            order_amount=Decimal('100'),
            min_profitability=Decimal('0.01'),
            target_profitability=Decimal('0.015'),
            max_profitability=Decimal('0.02'),
        )

    @property
    def base_config_short(self) -> XEMMExecutorConfig:
        return XEMMExecutorConfig(
            timestamp=1234,
            buying_market=ConnectorPair(connector_name='binance', trading_pair='ETH-USDT'),
            selling_market=ConnectorPair(connector_name='kucoin', trading_pair='ETH-USDT'),
            maker_side=TradeType.SELL,
            order_amount=Decimal('100'),
            min_profitability=Decimal('0.01'),
            target_profitability=Decimal('0.015'),
            max_profitability=Decimal('0.02'),
        )

    @staticmethod
    def create_mock_strategy():
        market = MagicMock()
        market_info = MagicMock()
        market_info.market = market

        strategy = MagicMock(spec=StrategyV2Base)
        type(strategy).market_info = PropertyMock(return_value=market_info)
        type(strategy).trading_pair = PropertyMock(return_value="ETH-USDT")
        strategy.buy.side_effect = ["OID-BUY-1", "OID-BUY-2", "OID-BUY-3"]
        strategy.sell.side_effect = ["OID-SELL-1", "OID-SELL-2", "OID-SELL-3"]
        strategy.cancel.return_value = None
        binance_connector = MagicMock(spec=ExchangePyBase)
        binance_connector.supported_order_types = MagicMock(return_value=[OrderType.LIMIT, OrderType.MARKET])
        kucoin_connector = MagicMock(spec=ExchangePyBase)
        kucoin_connector.supported_order_types = MagicMock(return_value=[OrderType.LIMIT, OrderType.MARKET])
        strategy.connectors = {
            "binance": binance_connector,
            "kucoin": kucoin_connector,
        }
        return strategy

    def test_is_arbitrage_valid(self):
        self.assertTrue(self.executor.is_arbitrage_valid('ETH-USDT', 'ETH-USDT'))
        self.assertTrue(self.executor.is_arbitrage_valid('ETH-BUSD', 'ETH-USDT'))
        self.assertTrue(self.executor.is_arbitrage_valid('ETH-USDT', 'WETH-USDT'))
        self.assertFalse(self.executor.is_arbitrage_valid('ETH-USDT', 'BTC-USDT'))
        self.assertTrue(self.executor.is_arbitrage_valid('ETH-USDT', 'ETH-BTC'))

    @staticmethod
    def _mock_done_order(executed_base, avg_price, fees):
        order = Mock(spec=TrackedOrder)
        order.executed_amount_base = executed_base
        order.average_executed_price = avg_price
        order.cum_fees_quote = fees
        order.is_done = True
        return order

    def test_net_pnl_long(self):
        self.executor._status = RunnableStatus.TERMINATED
        self.executor.maker_order = self._mock_done_order(Decimal('1'), Decimal('100'), Decimal('1'))
        self.executor._maker_orders_by_id = {"OID-BUY-1": self.executor.maker_order}
        self.executor.taker_orders = [self._mock_done_order(Decimal('1'), Decimal('200'), Decimal('1'))]
        self.assertEqual(self.executor.net_pnl_quote, Decimal('98'))
        self.assertEqual(self.executor.net_pnl_pct, Decimal('0.98'))

    def test_net_pnl_short(self):
        executor = XEMMExecutor(self.strategy, self.base_config_short, self.update_interval)
        executor._status = RunnableStatus.TERMINATED
        executor.maker_order = self._mock_done_order(Decimal('1'), Decimal('100'), Decimal('1'))
        executor._maker_orders_by_id = {"OID-BUY-1": executor.maker_order}
        executor.taker_orders = [self._mock_done_order(Decimal('1'), Decimal('200'), Decimal('1'))]
        self.assertEqual(executor.net_pnl_quote, Decimal('98'))
        self.assertEqual(executor.net_pnl_pct, Decimal('0.98'))

    @patch.object(XEMMExecutor, "get_in_flight_order", return_value=None)
    def test_partial_fill_then_cancel_hedges_filled_amount(self, _in_flight_mock):
        # maker 部分成交后因 profitability 撤单，已成交部分必须被对冲
        self.executor._status = RunnableStatus.RUNNING
        self.executor.maker_order = TrackedOrder(order_id="OID-BUY-1")
        self.executor._maker_orders_by_id = {"OID-BUY-1": self.executor.maker_order}
        fill_event = OrderFilledEvent(
            timestamp=1234,
            order_id="OID-BUY-1",
            trading_pair="ETH-USDT",
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("100"),
            amount=Decimal("40"),
            trade_fee=AddedToCostTradeFee(flat_fees=[]),
        )
        self.executor.process_order_filled_event(1, MagicMock(), fill_event)
        # 已提交对冲 40 的成交量，进入收尾
        self.assertEqual(self.executor._maker_filled_base, Decimal("40"))
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("40"))
        self.assertEqual(len(self.executor.taker_orders), 1)
        self.assertEqual(self.executor.taker_orders[0].order_id, "OID-SELL-1")
        self.assertEqual(self.executor._status, RunnableStatus.SHUTTING_DOWN)

    def test_hedge_pending_respects_max_hedge_order_base(self):
        self.executor.config.max_hedge_order_base = Decimal("40")
        self.executor._maker_filled_base = Decimal("100")
        with patch.object(self.executor, "_can_place_taker_hedge", return_value=True):
            self.executor._hedge_pending()

        self.assertEqual(self.executor._submitted_hedge_base, Decimal("40"))
        self.assertEqual(len(self.executor.taker_orders), 1)
        self.assertEqual(self.executor._taker_amounts.get("OID-SELL-1"), Decimal("40"))

    @patch.object(XEMMExecutor, "get_in_flight_order", return_value=None)
    def test_two_partial_fills_without_trade_id_hedge_both(self, _in_flight_mock):
        # 两笔均无 exchange_trade_id（默认 ""）的 partial fill 不应被错误去重，应各自对冲
        self.executor._status = RunnableStatus.RUNNING
        self.executor.maker_order = TrackedOrder(order_id="OID-BUY-1")
        self.executor._maker_orders_by_id = {"OID-BUY-1": self.executor.maker_order}

        def make_fill(amount):
            return OrderFilledEvent(
                timestamp=1234,
                order_id="OID-BUY-1",
                trading_pair="ETH-USDT",
                trade_type=TradeType.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal("100"),
                amount=amount,
                trade_fee=AddedToCostTradeFee(flat_fees=[]),
            )

        self.executor.process_order_filled_event(1, MagicMock(), make_fill(Decimal("40")))
        self.executor.process_order_filled_event(1, MagicMock(), make_fill(Decimal("30")))
        self.assertEqual(self.executor._maker_filled_base, Decimal("70"))
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("70"))
        self.assertEqual(len(self.executor.taker_orders), 2)
        self.assertEqual(self.executor._taker_amounts.get("OID-SELL-1"), Decimal("40"))
        self.assertEqual(self.executor._taker_amounts.get("OID-SELL-2"), Decimal("30"))

    @patch.object(XEMMExecutor, "get_in_flight_order", return_value=None)
    def test_cancel_race_fill_after_clear_still_hedges(self, _in_flight_mock):
        # 撤单竞态：撤单清空 maker_order 后才收到成交回报，仍应对冲
        self.executor._status = RunnableStatus.RUNNING
        self.executor._maker_orders_by_id = {"OID-BUY-1": TrackedOrder(order_id="OID-BUY-1")}
        self.executor.maker_order = None  # 模拟撤单已清空引用
        fill_event = OrderFilledEvent(
            timestamp=1234,
            order_id="OID-BUY-1",
            trading_pair="ETH-USDT",
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("100"),
            amount=Decimal("25"),
            trade_fee=AddedToCostTradeFee(flat_fees=[]),
        )
        self.executor.process_order_filled_event(1, MagicMock(), fill_event)
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("25"))
        self.assertEqual(len(self.executor.taker_orders), 1)

    @patch.object(XEMMExecutor, "get_in_flight_order", return_value=None)
    def test_completed_event_before_fill_event_does_not_double_hedge(self, _in_flight_mock):
        # 事件乱序：completed 先到，fill 后到，不能重复对冲
        self.executor._status = RunnableStatus.RUNNING
        self.executor.maker_order = TrackedOrder(order_id="OID-BUY-1")
        self.executor._maker_orders_by_id = {"OID-BUY-1": self.executor.maker_order}
        completed_event = BuyOrderCompletedEvent(
            timestamp=1234,
            order_id="OID-BUY-1",
            base_asset="ETH",
            quote_asset="USDT",
            base_asset_amount=Decimal("100"),
            quote_asset_amount=Decimal("10000"),
            order_type=OrderType.LIMIT,
        )
        self.executor.process_order_completed_event(1, MagicMock(), completed_event)
        # completed 触发一次对冲 100
        self.assertEqual(self.executor._maker_filled_base, Decimal("100"))
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("100"))
        self.assertEqual(len(self.executor.taker_orders), 1)
        # 随后 fill 事件到达（同一笔成交），不得再次对冲
        fill_event = OrderFilledEvent(
            timestamp=1234,
            order_id="OID-BUY-1",
            trading_pair="ETH-USDT",
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("100"),
            amount=Decimal("100"),
            trade_fee=AddedToCostTradeFee(flat_fees=[]),
        )
        self.executor.process_order_filled_event(1, MagicMock(), fill_event)
        self.assertEqual(self.executor._maker_filled_base, Decimal("100"))
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("100"))
        self.assertEqual(len(self.executor.taker_orders), 1)

    def test_taker_partial_fill_then_failure_retries_only_unfilled_delta(self):
        # taker 市价单部分成交 60 后失败，只应补未成交的 40
        self.executor._maker_filled_base = Decimal("100")
        self.executor._maker_filled_floor = Decimal("100")
        self.executor._submitted_hedge_base = Decimal("100")
        taker = TrackedOrder(order_id="OID-SELL-0")
        taker_in_flight = Mock()
        taker_in_flight.executed_amount_base = Decimal("60")
        taker_in_flight.is_done = True
        taker.order = taker_in_flight
        self.executor.taker_orders = [taker]
        self.executor._taker_order_ids = {"OID-SELL-0"}
        self.executor._taker_amounts = {"OID-SELL-0": Decimal("100")}
        failure_event = MarketOrderFailureEvent(
            timestamp=1234,
            order_id="OID-SELL-0",
            order_type=OrderType.MARKET,
        )
        # _update_tracked_order_with_order_id 会调 get_in_flight_order；
        # 让它返回同一个 in-flight mock，保持 executed_amount_base=60 不变
        with patch.object(self.executor, "get_in_flight_order", return_value=taker_in_flight):
            self.executor.process_order_failed_event(1, MagicMock(), failure_event)
        # 只重下未成交的 40
        self.assertEqual(self.executor._taker_amounts.get("OID-SELL-1"), Decimal("40"))
        self.assertEqual(self.executor.taker_order.order_id, "OID-SELL-1")
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("100"))
        # 失败单标记完结，实际对冲量 = 已成交 60（失败单）
        self.assertIn("OID-SELL-0", self.executor._failed_taker_ids)
        self.assertEqual(self.executor._actual_hedged_base(), Decimal("60"))

    @patch.object(XEMMExecutor, "get_in_flight_order", return_value=None)
    def test_duplicate_taker_failure_event_does_not_double_retry(self, _in_flight_mock):
        # 同一个 taker failure 事件重复到达，只应补一笔单（幂等）
        self.executor._maker_filled_base = Decimal("100")
        self.executor._submitted_hedge_base = Decimal("100")
        self.executor.taker_orders = [TrackedOrder(order_id="OID-SELL-0")]
        self.executor._taker_order_ids = {"OID-SELL-0"}
        self.executor._taker_amounts = {"OID-SELL-0": Decimal("100")}
        failure_event = MarketOrderFailureEvent(
            timestamp=1234,
            order_id="OID-SELL-0",
            order_type=OrderType.MARKET,
        )
        self.executor.process_order_failed_event(1, MagicMock(), failure_event)
        self.executor.process_order_failed_event(1, MagicMock(), failure_event)
        # 第二次失败事件被幂等忽略：只生成一笔 retry，submitted 不被二次回退
        retry_orders = [t for t in self.executor.taker_orders if t.order_id != "OID-SELL-0"]
        self.assertEqual(len(retry_orders), 1)
        self.assertEqual(retry_orders[0].order_id, "OID-SELL-1")
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("100"))

    @patch.object(XEMMExecutor, "get_in_flight_order")
    def test_taker_failure_with_unrefreshed_tracked_reads_actual_fill(self, in_flight_mock):
        # taker 失败时 tracked.order 仍为 None（created 事件乱序），
        # failure 分支需先调 _update_tracked_order_with_order_id 才能正确读到已成交量（60），
        # 从而只补 40，而非按全量 100 重试
        self.executor._maker_filled_base = Decimal("100")
        self.executor._submitted_hedge_base = Decimal("100")
        taker = TrackedOrder(order_id="OID-SELL-0")
        # order 尚未挂上（模拟 created 事件缺失）
        self.assertEqual(taker.order, None)
        self.executor.taker_orders = [taker]
        self.executor._taker_order_ids = {"OID-SELL-0"}
        self.executor._taker_amounts = {"OID-SELL-0": Decimal("100")}

        # get_in_flight_order 返回已部分成交 60 的 in-flight order
        in_flight = Mock()
        in_flight.executed_amount_base = Decimal("60")
        in_flight_mock.return_value = in_flight

        failure_event = MarketOrderFailureEvent(
            timestamp=1234,
            order_id="OID-SELL-0",
            order_type=OrderType.MARKET,
        )
        self.executor.process_order_failed_event(1, MagicMock(), failure_event)
        # tracked 已被刷新，filled 读到 60，只补未成交的 40
        self.assertEqual(self.executor._taker_amounts.get("OID-SELL-1"), Decimal("40"))
        self.assertEqual(self.executor.taker_order.order_id, "OID-SELL-1")
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("100"))

    @patch.object(XEMMExecutor, "get_in_flight_order")
    def test_fallback_key_collision_floor_prevents_missed_hedge(self, in_flight_mock):
        # 无 exchange_trade_id 时两笔完全相同的 fill（同 ts/price/amount）fallback key 碰撞：
        # 第二笔命中 dedup 后直接 return，不能再靠 fills_sum 计入。
        # floor 必须在 dedup return 之前更新，确保第二笔 fill 时 floor 已抬升到真实累计量。
        # get_in_flight_order side_effect 模拟真实顺序：第一笔时 tracked 累计 35，第二笔时累计 70
        self.executor._status = RunnableStatus.RUNNING
        maker_tracked = TrackedOrder(order_id="OID-BUY-1")
        self.executor.maker_order = maker_tracked
        self.executor._maker_orders_by_id = {"OID-BUY-1": maker_tracked}

        in_flight_35 = Mock()
        in_flight_35.executed_amount_base = Decimal("35")
        in_flight_70 = Mock()
        in_flight_70.executed_amount_base = Decimal("70")
        # 每次 _update_tracked_order_with_order_id 调一次 get_in_flight_order
        in_flight_mock.side_effect = [in_flight_35, in_flight_70]

        def make_fill():
            return OrderFilledEvent(
                timestamp=1234,        # 相同 ts
                order_id="OID-BUY-1",
                trading_pair="ETH-USDT",
                trade_type=TradeType.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal("100"),  # 相同 price
                amount=Decimal("35"),  # 相同 amount → fallback key 碰撞
                trade_fee=AddedToCostTradeFee(flat_fees=[]),
            )

        self.executor.process_order_filled_event(1, MagicMock(), make_fill())
        # 第一笔：floor 由 tracked(35) 抬升，fills_sum=35，_maker_filled_base = max(35,35) = 35
        self.assertEqual(self.executor._maker_filled_base, Decimal("35"))

        self.executor.process_order_filled_event(1, MagicMock(), make_fill())
        # 第二笔：dedup return 前 floor 由 tracked(70) 抬升，_maker_filled_base = max(35,70) = 70
        # 虽然 fills_sum 仍是 35，floor 兜住了真实累计量
        self.assertEqual(self.executor._maker_filled_base, Decimal("70"))
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("70"))

    @patch.object(XEMMExecutor, "get_in_flight_order")
    def test_late_maker_fill_after_cancel_clear_preserves_pnl(self, in_flight_mock):
        # 撤单时 filled==0 清空 maker_order，随后迟到的 maker fill 仍应计入 PnL/fee
        self.executor._status = RunnableStatus.RUNNING
        retained_maker = TrackedOrder(order_id="OID-BUY-1")
        self.executor._maker_orders_by_id = {"OID-BUY-1": retained_maker}
        self.executor.maker_order = None  # 撤单已清空当前引用

        # 迟到的 maker fill：_update_tracked_order 会给 retained_maker 挂上真实 InFlightOrder
        maker_in_flight = InFlightOrder(
            client_order_id="OID-BUY-1",
            creation_timestamp=1234,
            trading_pair="ETH-USDT",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1"),
            price=Decimal("100"),
            initial_state=OrderState.FILLED,
        )
        maker_in_flight.executed_amount_base = Decimal("1")
        in_flight_mock.return_value = maker_in_flight
        fill_event = OrderFilledEvent(
            timestamp=1234,
            order_id="OID-BUY-1",
            trading_pair="ETH-USDT",
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("100"),
            amount=Decimal("1"),
            trade_fee=AddedToCostTradeFee(flat_fees=[]),
        )
        self.executor.process_order_filled_event(1, MagicMock(), fill_event)
        # maker 仍被对冲
        self.assertEqual(self.executor._maker_filled_base, Decimal("1"))
        self.assertEqual(len(self.executor.taker_orders), 1)
        # retained_maker 已被刷新出 tracked order（用于 PnL/fee），不再因 maker_order=None 而丢失
        self.assertIsNotNone(retained_maker.order)
        self.assertEqual(retained_maker.executed_amount_base, Decimal("1"))

    @patch.object(XEMMExecutor, 'get_trading_rules')
    @patch.object(XEMMExecutor, 'adjust_order_candidates')
    async def test_validate_sufficient_balance(self, mock_adjust_order_candidates, mock_get_trading_rules):
        # Mock trading rules
        trading_rules = TradingRule(trading_pair="ETH-USDT", min_order_size=Decimal("0.1"),
                                    min_price_increment=Decimal("0.1"), min_base_amount_increment=Decimal("0.1"))
        mock_get_trading_rules.return_value = trading_rules
        order_candidate = OrderCandidate(
            trading_pair="ETH-USDT",
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=TradeType.BUY,
            amount=Decimal("1"),
            price=Decimal("100")
        )
        # Test for sufficient balance
        mock_adjust_order_candidates.return_value = [order_candidate]
        await self.executor.validate_sufficient_balance()
        self.assertNotEqual(self.executor.close_type, CloseType.INSUFFICIENT_BALANCE)

        # Test for insufficient balance
        order_candidate.amount = Decimal("0")
        mock_adjust_order_candidates.return_value = [order_candidate]
        await self.executor.validate_sufficient_balance()
        self.assertEqual(self.executor.close_type, CloseType.INSUFFICIENT_BALANCE)
        self.assertEqual(self.executor.status, RunnableStatus.TERMINATED)

    @patch.object(XEMMExecutor, "get_resulting_price_for_amount")
    @patch.object(XEMMExecutor, "get_tx_cost_in_asset")
    async def test_control_task_running_order_not_placed(self, tx_cost_mock, resulting_price_mock):
        tx_cost_mock.return_value = Decimal('0.01')
        resulting_price_mock.return_value = Decimal("100")
        self.executor._status = RunnableStatus.RUNNING
        await self.executor.control_task()
        # Calculate expected maker target price using the new formula:
        # maker_price = taker_price / (1 + target_profitability + tx_cost_pct)
        # tx_cost_pct = (0.01 + 0.01) / 100 = 0.0002
        # maker_price = 100 / (1 + 0.015 + 0.0002) = 100 / 1.0152
        expected_price = Decimal("100") / (Decimal("1") + Decimal("0.015") + Decimal("0.02") / Decimal("100"))
        self.assertEqual(self.executor._status, RunnableStatus.RUNNING)
        self.assertEqual(self.executor.maker_order.order_id, "OID-BUY-1")
        self.assertEqual(self.executor._maker_target_price, expected_price)

    @patch.object(XEMMExecutor, "get_resulting_price_for_amount")
    @patch.object(XEMMExecutor, "get_tx_cost_in_asset")
    async def test_control_task_running_order_not_placed_sell_side(self, tx_cost_mock, resulting_price_mock):
        # Test maker SELL side (taker BUY) to cover line 155
        executor = XEMMExecutor(self.strategy, self.base_config_short, self.update_interval)
        tx_cost_mock.return_value = Decimal('0.01')
        resulting_price_mock.return_value = Decimal("100")
        executor._status = RunnableStatus.RUNNING
        await executor.control_task()
        # Calculate expected maker target price using the new formula for SELL side:
        # maker_price = taker_price / (1 - target_profitability - tx_cost_pct)
        # tx_cost_pct = (0.01 + 0.01) / 100 = 0.0002
        # maker_price = 100 / (1 - 0.015 - 0.0002) = 100 / 0.9848
        expected_price = Decimal("100") / (Decimal("1") - Decimal("0.015") - Decimal("0.02") / Decimal("100"))
        self.assertEqual(executor._status, RunnableStatus.RUNNING)
        self.assertEqual(executor.maker_order.order_id, "OID-SELL-1")
        self.assertEqual(executor._maker_target_price, expected_price)

    @patch.object(XEMMExecutor, "get_resulting_price_for_amount")
    @patch.object(XEMMExecutor, "get_tx_cost_in_asset")
    async def test_control_task_running_order_placed_refresh_condition_min_profitability(self, tx_cost_mock,
                                                                                         resulting_price_mock):
        tx_cost_mock.return_value = Decimal('0.01')
        resulting_price_mock.return_value = Decimal("100")
        self.executor._status = RunnableStatus.RUNNING
        self.executor.maker_order = Mock(spec=TrackedOrder)
        self.executor.maker_order.order_id = "OID-BUY-1"
        self.executor.maker_order.is_done = False
        self.executor.maker_order.executed_amount_base = Decimal("0")
        self.executor.maker_order.order = InFlightOrder(
            creation_timestamp=1234,
            trading_pair="ETH-USDT",
            client_order_id="OID-BUY-1",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("100"),
            price=Decimal("99.5"),
            initial_state=OrderState.OPEN,
        )
        await self.executor.control_task()
        self.assertEqual(self.executor._status, RunnableStatus.RUNNING)
        self.assertEqual(self.executor.maker_order, None)

    @patch.object(XEMMExecutor, "get_resulting_price_for_amount")
    @patch.object(XEMMExecutor, "get_tx_cost_in_asset")
    async def test_control_task_running_order_placed_refresh_condition_max_profitability(self, tx_cost_mock,
                                                                                         resulting_price_mock):
        tx_cost_mock.return_value = Decimal('0.01')
        resulting_price_mock.return_value = Decimal("103")
        self.executor._status = RunnableStatus.RUNNING
        self.executor.maker_order = Mock(spec=TrackedOrder)
        self.executor.maker_order.order_id = "OID-BUY-1"
        self.executor.maker_order.is_done = False
        self.executor.maker_order.executed_amount_base = Decimal("0")
        self.executor.maker_order.order = InFlightOrder(
            creation_timestamp=1234,
            trading_pair="ETH-USDT",
            client_order_id="OID-BUY-1",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("100"),
            price=Decimal("99.5"),
            initial_state=OrderState.OPEN,
        )
        await self.executor.control_task()
        self.assertEqual(self.executor._status, RunnableStatus.RUNNING)
        self.assertEqual(self.executor.maker_order, None)

    async def test_control_task_shut_down_process(self):
        self.executor.maker_order = Mock(spec=TrackedOrder)
        self.executor.maker_order.order = None  # 已不在挂单
        taker = Mock(spec=TrackedOrder)
        taker.is_done = True
        taker.executed_amount_base = Decimal("1")  # 实际已对冲 1
        self.executor.taker_orders = [taker]
        self.executor._maker_filled_base = Decimal("1")
        self.executor._submitted_hedge_base = Decimal("1")
        self.executor._status = RunnableStatus.SHUTTING_DOWN
        await self.executor.control_task()
        self.assertEqual(self.executor._status, RunnableStatus.TERMINATED)
        self.assertEqual(self.executor.close_type, CloseType.COMPLETED)

    async def test_control_task_shut_down_process_maker_order_cleared(self):
        self.executor.maker_order = None
        self.executor.taker_order = Mock(spec=TrackedOrder)
        self.executor.taker_order.is_done = True
        self.executor._status = RunnableStatus.SHUTTING_DOWN
        await self.executor.control_task()
        self.assertEqual(self.executor._status, RunnableStatus.TERMINATED)

    @patch.object(XEMMExecutor, "get_in_flight_order")
    def test_process_order_created_event(self, in_flight_order_mock):
        self.executor._status = RunnableStatus.RUNNING
        in_flight_order_mock.side_effect = [
            InFlightOrder(
                client_order_id="OID-BUY-1",
                creation_timestamp=1234,
                trading_pair="ETH-USDT",
                order_type=OrderType.LIMIT,
                trade_type=TradeType.BUY,
                amount=Decimal("100"),
                price=Decimal("100"),
            ),
            InFlightOrder(
                client_order_id="OID-SELL-1",
                creation_timestamp=1234,
                trading_pair="ETH-USDT",
                order_type=OrderType.MARKET,
                trade_type=TradeType.SELL,
                amount=Decimal("100"),
                price=Decimal("100"),
            )
        ]

        self.executor.maker_order = TrackedOrder(order_id="OID-BUY-1")
        self.executor._maker_orders_by_id = {"OID-BUY-1": self.executor.maker_order}
        taker_tracked = TrackedOrder(order_id="OID-SELL-1")
        self.executor.taker_orders = [taker_tracked]
        buy_order_created_event = BuyOrderCreatedEvent(
            timestamp=1234,
            type=OrderType.LIMIT,
            creation_timestamp=1233,
            order_id="OID-BUY-1",
            trading_pair="ETH-USDT",
            amount=Decimal("100"),
            price=Decimal("100"),
        )
        sell_order_created_event = BuyOrderCreatedEvent(
            timestamp=1234,
            type=OrderType.MARKET,
            creation_timestamp=1233,
            order_id="OID-SELL-1",
            trading_pair="ETH-USDT",
            amount=Decimal("100"),
            price=Decimal("100"),
        )
        self.assertEqual(self.executor.maker_order.order, None)
        self.assertEqual(taker_tracked.order, None)
        self.executor.process_order_created_event(1, MagicMock(), buy_order_created_event)
        self.assertEqual(self.executor.maker_order.order.client_order_id, "OID-BUY-1")
        self.executor.process_order_created_event(1, MagicMock(), sell_order_created_event)
        self.assertEqual(taker_tracked.order.client_order_id, "OID-SELL-1")

    @patch.object(XEMMExecutor, "get_in_flight_order", return_value=None)
    def test_process_order_completed_event(self, _in_flight_mock):
        self.executor._status = RunnableStatus.RUNNING
        self.executor.maker_order = TrackedOrder(order_id="OID-BUY-1")
        self.executor._maker_orders_by_id = {"OID-BUY-1": self.executor.maker_order}
        self.assertEqual(self.executor.taker_order, None)
        buy_order_created_event = BuyOrderCompletedEvent(
            base_asset="ETH",
            quote_asset="USDT",
            base_asset_amount=Decimal("100"),
            quote_asset_amount=Decimal("100"),
            order_type=OrderType.LIMIT,
            timestamp=1234,
            order_id="OID-BUY-1",
        )
        self.executor.process_order_completed_event(1, MagicMock(), buy_order_created_event)
        self.assertEqual(self.executor.status, RunnableStatus.SHUTTING_DOWN)
        self.assertEqual(len(self.executor.taker_orders), 1)
        self.assertEqual(self.executor.taker_orders[0].order_id, "OID-SELL-1")
        self.assertEqual(self.executor._submitted_hedge_base, Decimal("100"))

    @patch.object(XEMMExecutor, "get_in_flight_order", return_value=None)
    def test_process_order_failed_event(self, _in_flight_mock):
        self.executor.maker_order = TrackedOrder(order_id="OID-BUY-1")
        maker_failure_event = MarketOrderFailureEvent(
            timestamp=1234,
            order_id="OID-BUY-1",
            order_type=OrderType.LIMIT,
        )
        self.executor.process_order_failed_event(1, MagicMock(), maker_failure_event)
        self.assertEqual(self.executor.maker_order, None)

        self.executor._maker_filled_base = Decimal("100")
        self.executor._submitted_hedge_base = Decimal("100")
        self.executor.taker_orders = [TrackedOrder(order_id="OID-SELL-0")]
        self.executor._taker_order_ids = {"OID-SELL-0"}
        self.executor._taker_amounts = {"OID-SELL-0": Decimal("100")}
        taker_failure_event = MarketOrderFailureEvent(
            timestamp=1234,
            order_id="OID-SELL-0",
            order_type=OrderType.MARKET,
        )
        self.executor.process_order_failed_event(1, MagicMock(), taker_failure_event)
        # 失败单完全未成交，按原量 100 重下
        self.assertEqual(self.executor.taker_order.order_id, "OID-SELL-1")
        self.assertEqual(self.executor._taker_amounts.get("OID-SELL-1"), Decimal("100"))

    def test_get_custom_info(self):
        self.assertEqual(self.executor.get_custom_info(), {'maker_connector': 'binance',
                                                           'maker_target_price': Decimal('1'),
                                                           'maker_trading_pair': 'ETH-USDT',
                                                           'max_profitability': Decimal('0.02'),
                                                           'min_profitability': Decimal('0.01'),
                                                           'net_profitability': Decimal('-1'),
                                                           'order_amount': Decimal('100'),
                                                           'side': TradeType.BUY,
                                                           'taker_connector': 'kucoin',
                                                           'taker_price': Decimal('1'),
                                                           'taker_trading_pair': 'ETH-USDT',
                                                           'target_profitability_pct': Decimal('0.015'),
                                                           'trade_profitability': Decimal('0'),
                                                           'tx_cost': Decimal('1'),
                                                           'tx_cost_pct': Decimal('1'),
                                                           'maker_filled_base': Decimal('0'),
                                                           'submitted_hedge_base': Decimal('0'),
                                                           'actual_hedged_base': Decimal('0'),
                                                           'unhedged_base': Decimal('0'),
                                                           'last_hedge_block_reason': None,
                                                           'hedge_blocked_since': None,
                                                           'hedge_blocked_seconds': Decimal('0')})

    def test_to_format_status(self):
        self.assertIn("Maker Side: TradeType.BUY", self.executor.to_format_status())

    def test_early_stop(self):
        self.executor._status = RunnableStatus.RUNNING
        self.executor.maker_order = Mock(spec=TrackedOrder)
        self.executor.maker_order.is_open = True
        self.executor.early_stop()
        self.assertEqual(self.executor._status, RunnableStatus.TERMINATED)

    def test_get_cum_fees_quote_not_executed(self):
        self.assertEqual(self.executor.get_cum_fees_quote(), Decimal('0'))

    @patch.object(XEMMExecutor, 'rate_oracle', create=True)
    async def test_get_quote_asset_conversion_rate_none(self, mock_rate_oracle):
        mock_rate_oracle.get_pair_rate.return_value = None
        self.executor.quote_conversion_pair = "USDC-USDT"
        with self.assertRaises(ValueError):
            await self.executor.get_quote_asset_conversion_rate()

    @patch.object(XEMMExecutor, 'rate_oracle', create=True)
    async def test_get_quote_asset_conversion_rate_exception(self, mock_rate_oracle):
        mock_rate_oracle.get_pair_rate.side_effect = Exception("Test exception")
        self.executor.quote_conversion_pair = "USDC-USDT"
        with self.assertRaises(Exception):
            await self.executor.get_quote_asset_conversion_rate()
