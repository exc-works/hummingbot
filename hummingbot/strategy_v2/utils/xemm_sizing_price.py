from decimal import Decimal
from typing import Callable, Optional

from hummingbot.core.data_type.common import PriceType
from hummingbot.core.rate_oracle.rate_oracle import RateOracle


def _valid_price(price) -> bool:
    return price is not None and not price.is_nan() and price > 0


def _taker_to_maker_quote_price(
    taker_price: Decimal,
    maker_quote: str,
    taker_quote: str,
) -> Optional[Decimal]:
    if maker_quote == taker_quote:
        return taker_price
    rate_oracle = RateOracle.get_instance()
    conversion = rate_oracle.get_pair_rate(f"{taker_quote}-{maker_quote}")
    if not _valid_price(conversion):
        inverse = rate_oracle.get_pair_rate(f"{maker_quote}-{taker_quote}")
        if _valid_price(inverse):
            conversion = Decimal("1") / inverse
        else:
            return None
    return taker_price * conversion


def _resolve_maker_book_price(
    get_price_by_type: Callable[[str, str, PriceType], Decimal],
    maker_connector: str,
    maker_trading_pair: str,
) -> tuple[Optional[Decimal], Optional[str]]:
    price = get_price_by_type(maker_connector, maker_trading_pair, PriceType.MidPrice)
    if _valid_price(price):
        return price, "maker MidPrice"
    return None, None


def _resolve_taker_reference_price(
    get_price_by_type: Callable[[str, str, PriceType], Decimal],
    maker_trading_pair: str,
    taker_connector: str,
    taker_trading_pair: str,
) -> tuple[Optional[Decimal], Optional[str]]:
    _, maker_quote = maker_trading_pair.split("-")
    _, taker_quote = taker_trading_pair.split("-")

    for price_type in (PriceType.MidPrice, PriceType.LastTrade, PriceType.BestBid, PriceType.BestAsk):
        taker_price = get_price_by_type(taker_connector, taker_trading_pair, price_type)
        if not _valid_price(taker_price):
            continue
        maker_price = _taker_to_maker_quote_price(taker_price, maker_quote, taker_quote)
        if _valid_price(maker_price):
            return maker_price, f"taker {price_type.name}"
    return None, None


def resolve_xemm_sizing_price(
    get_price_by_type: Callable[[str, str, PriceType], Decimal],
    maker_connector: str,
    maker_trading_pair: str,
    taker_connector: str,
    taker_trading_pair: str,
    require_maker_order_book: bool = True,
) -> tuple[Optional[Decimal], Optional[str]]:
    """
    Resolve a reference price in maker-quote per base for sizing (quote → base).

    require_maker_order_book=True:
      Only maker MidPrice (both sides of the book must be present).

    require_maker_order_book=False:
      Ignore maker book; use taker MidPrice / LastTrade / BestBid / BestAsk,
      converted to maker quote when needed.
    """
    if require_maker_order_book:
        return _resolve_maker_book_price(
            get_price_by_type, maker_connector, maker_trading_pair
        )
    return _resolve_taker_reference_price(
        get_price_by_type, maker_trading_pair, taker_connector, taker_trading_pair
    )
