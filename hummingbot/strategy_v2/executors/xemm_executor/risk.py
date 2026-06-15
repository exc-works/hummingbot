from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from hummingbot.core.data_type.common import PriceType, TradeType


@dataclass
class HedgeCapability:
    allowed: bool
    max_amount: Decimal
    reason: str = ""
    estimated_price: Optional[Decimal] = None
    slippage_bps: Optional[Decimal] = None


def to_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if converted.is_nan():
        return None
    return converted


def split_trading_pair(trading_pair: str):
    base, quote = trading_pair.split("-")
    return base, quote


def side_required_balance(trading_pair: str, side: TradeType, amount: Decimal, price: Decimal):
    base, quote = split_trading_pair(trading_pair)
    if side == TradeType.BUY:
        return quote, amount * price
    return base, amount


def available_balance(market_data_provider, connector_name: str, asset: str) -> Decimal:
    try:
        balance = to_decimal(market_data_provider.get_available_balance(connector_name, asset))
        return balance if balance is not None else Decimal("0")
    except Exception:
        return Decimal("0")


def max_hedgeable_amount(market_data_provider, connector_name: str, trading_pair: str,
                         side: TradeType, price: Decimal, reserve_base: Decimal = Decimal("0"),
                         reserve_quote: Decimal = Decimal("0")) -> Decimal:
    base, quote = split_trading_pair(trading_pair)
    if side == TradeType.BUY:
        available_quote = available_balance(market_data_provider, connector_name, quote)
        spendable_quote = max(available_quote - reserve_quote, Decimal("0"))
        return spendable_quote / price if price > 0 else Decimal("0")
    available_base = available_balance(market_data_provider, connector_name, base)
    return max(available_base - reserve_base, Decimal("0"))


def estimate_market_order_price(market_data_provider, connector_name: str, trading_pair: str,
                                side: TradeType, amount: Decimal) -> Tuple[Optional[Decimal], Decimal]:
    if amount <= 0:
        return None, Decimal("0")
    try:
        result = market_data_provider.get_vwap_for_volume(
            connector_name=connector_name,
            trading_pair=trading_pair,
            volume=float(amount),
            is_buy=side == TradeType.BUY,
        )
        price = getattr(result, "result_price", None)
        if price is None:
            price = getattr(result, "query_price", None)
        if price is None:
            return None, Decimal("0")
        result_volume = getattr(result, "result_volume", None)
        executable_amount = to_decimal(result_volume) if result_volume is not None else amount
        return to_decimal(price), executable_amount or Decimal("0")
    except Exception:
        return None, Decimal("0")


def estimate_slippage_bps(reference_price: Decimal, execution_price: Decimal, side: TradeType) -> Optional[Decimal]:
    if reference_price is None or execution_price is None or reference_price <= 0:
        return None
    if side == TradeType.BUY:
        slippage = (execution_price - reference_price) / reference_price
    else:
        slippage = (reference_price - execution_price) / reference_price
    return max(slippage * Decimal("10000"), Decimal("0"))


def is_depth_sufficient(amount: Decimal, execution_price: Optional[Decimal], min_depth_notional: Decimal) -> bool:
    if min_depth_notional <= 0:
        return True
    if execution_price is None:
        return False
    return amount * execution_price >= min_depth_notional


def can_hedge(market_data_provider, connector_name: str, trading_pair: str, side: TradeType, amount: Decimal,
              max_slippage_bps: Decimal = Decimal("0"), max_order_base: Decimal = Decimal("0"),
              min_notional: Decimal = Decimal("0"), min_depth_notional: Decimal = Decimal("0"),
              reserve_base: Decimal = Decimal("0"), reserve_quote: Decimal = Decimal("0")) -> HedgeCapability:
    if amount <= 0:
        return HedgeCapability(False, Decimal("0"), "amount is zero")

    try:
        mid_price = market_data_provider.get_price_by_type(connector_name, trading_pair, PriceType.MidPrice)
    except Exception:
        mid_price = None
    mid_price = to_decimal(mid_price)
    if mid_price is None or mid_price <= 0:
        return HedgeCapability(False, Decimal("0"), "mid price unavailable")

    max_by_balance = max_hedgeable_amount(
        market_data_provider=market_data_provider,
        connector_name=connector_name,
        trading_pair=trading_pair,
        side=side,
        price=mid_price,
        reserve_base=reserve_base,
        reserve_quote=reserve_quote,
    )
    if max_by_balance <= 0:
        return HedgeCapability(False, Decimal("0"), "insufficient hedge balance")
    if max_by_balance < amount:
        return HedgeCapability(False, max_by_balance, "partial hedge capacity")

    hedge_order_amount = min(amount, max_order_base) if max_order_base > 0 else amount
    if min_notional > 0 and hedge_order_amount * mid_price < min_notional:
        return HedgeCapability(False, hedge_order_amount, "hedge notional below minimum")

    needs_depth_check = max_slippage_bps > 0 or min_depth_notional > 0
    if not needs_depth_check:
        return HedgeCapability(True, amount, "", mid_price)

    execution_price, executable_amount = estimate_market_order_price(
        market_data_provider, connector_name, trading_pair, side, hedge_order_amount
    )
    if execution_price is None or executable_amount < hedge_order_amount:
        return HedgeCapability(False, executable_amount, "insufficient order book depth", execution_price)
    if not is_depth_sufficient(hedge_order_amount, execution_price, min_depth_notional):
        return HedgeCapability(False, hedge_order_amount, "insufficient order book depth", execution_price)

    slippage_bps = estimate_slippage_bps(mid_price, execution_price, side)
    if max_slippage_bps > 0:
        if slippage_bps is None:
            return HedgeCapability(False, hedge_order_amount, "slippage unavailable", execution_price)
        if slippage_bps > max_slippage_bps:
            return HedgeCapability(False, hedge_order_amount, f"slippage {slippage_bps:.4f} bps exceeds limit",
                                   execution_price, slippage_bps)

    return HedgeCapability(True, amount, "", execution_price, slippage_bps)
