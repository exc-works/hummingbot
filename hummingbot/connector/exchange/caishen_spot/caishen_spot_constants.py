from hummingbot.connector.derivative.caishen_perpetual import caishen_perpetual_constants as PERP_CONSTANTS
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

EXCHANGE_NAME = "caishen_spot"
DOMAIN = EXCHANGE_NAME
TESTNET_DOMAIN = "caishen_spot_testnet"

TRADING_DOMAIN_SPOT = "1"

MAINNET_REST_URL = "https://api.plato.exchange"
TESTNET_REST_URL = "https://api-dev.plato.exchange"

MAINNET_WS_URL = "wss://api.plato.exchange/v1/ws"
TESTNET_WS_URL = "wss://api-dev.plato.exchange/v1/ws"

EXCHANGE_INFO_URL = "/v1/symbols"
SNAPSHOT_REST_URL = "/v1/l2book"
TICKER_PRICE_CHANGE_URL = "/v1/tickers/by-symbol"
PING_URL = "/v1/udf/time"
GET_LATEST_BLOCK_URL = "/v1/explorer/block/latest"
SUBMIT_TX_URL = "/v1/tx"
BALANCES_URL = "/v1/balances"
ORDER_OPEN_URL = "/v1/orders/open"
ACCOUNT_TRADE_LIST_URL = "/v1/trades/by-account"

CHAIN_ID = PERP_CONSTANTS.CHAIN_ID
ORDER_STATE = PERP_CONSTANTS.ORDER_STATE
get_order_state = PERP_CONSTANTS.get_order_state

SPOT_PLACE_ORDER_ACTION = "SPOT_PLACE_ORDER"
SPOT_CANCEL_ORDER_ACTION = "SPOT_CANCEL_ORDER"

ORDER_TIME_IN_FORCE = {
    "GTC": 1,
    "IOC": 2,
    "FOK": 3,
    "POST_ONLY": 4,
}

ORDER_TYPE = {
    "LIMIT": 0,
    "MARKET": 1,
}

HBOT_ORDER_ID_PREFIX = ""
MAX_ORDER_ID_LEN = 16
CURRENCY = "USDC"

HEARTBEAT_TIME_INTERVAL = 30.0
MAX_REQUEST = 1200
ALL_ENDPOINTS_LIMIT = "All"

RATE_LIMITS = [
    RateLimit(ALL_ENDPOINTS_LIMIT, limit=MAX_REQUEST, time_interval=60),
]

ORDER_NOT_EXIST_CODE = 1139
