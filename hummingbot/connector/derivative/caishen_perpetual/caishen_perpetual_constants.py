# your_dex_perpetual_constants.py

from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

# === 基本信息 ===
EXCHANGE_NAME = "caishen_perpetual"
BROKER_ID = ""
MAX_ORDER_ID_LEN = 16
CHAIN_ID = 71
UPSTREAM_CHAIN_ID = 421614
EIP712_DOMAIN_NAME = "Plato"

# === 域名配置 ===
DOMAIN = EXCHANGE_NAME
TESTNET_DOMAIN = "caishen_perpetual_testnet"

# === API URL ===
PERPETUAL_BASE_URL = "https://api.plato.exchange"
TESTNET_BASE_URL = "https://api-dev.plato.exchange"

PERPETUAL_WS_URL = "wss://api.plato.exchange/v1/ws"
TESTNET_WS_URL = "wss://api-dev.plato.exchange/v1/ws"

TRADING_DOMAIN_PERP = "0"

# === API 端点路径 ===

EXCHANGE_INFO_URL = "/v1/symbols"
SNAPSHOT_REST_URL = "/v1/l2book"
TICKER_PRICE_CHANGE_URL = "/v1/tickers/by-symbol"
FUNDING_URL = "/v1/funding/realtime"
FUNDING_HISTORY_URL = "/v1/funding/history"
PING_URL = "/v1/udf/time"
GET_LATEST_BLOCK_URL = "/v1/explorer/block/latest"

SUBMIT_TX_URL = "/v1/tx"
ACCOUNT_INFO_URL = "/v1/balances/perp"
ORDER_OPEN_URL = "/v1/orders/open"
POSITION_INFORMATION_URL = "/v1/positions/open"
ACCOUNT_TRADE_LIST_URL = "/v1/trades/by-account"
ACCOUNT_FUNDING_HISTORY_URL = "/v1/positions/funding/history"

# === WebSocket 频道 ===
TRADES_ENDPOINT_NAME = "trades"
DEPTH_ENDPOINT_NAME = "orderbook"
USER_ORDERS_ENDPOINT_NAME = "orders"
USEREVENT_ENDPOINT_NAME = "user"

# === 订单状态映射 (OrderStatus in order.proto) ===
ORDER_STATE = {
    0: OrderState.PENDING_CREATE,    # INVALID
    1: OrderState.OPEN,              # NEW
    2: OrderState.CANCELED,          # CANCELLED
    3: OrderState.CANCELED,          # EXPIRED
    4: OrderState.PARTIALLY_FILLED,  # PARTIALLY_FILLED
    5: OrderState.CANCELED,          # PARTIALLY_CANCELLED
    6: OrderState.FILLED,            # FILLED
    7: OrderState.OPEN,              # TRIGGERED
}

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

# 如果需要同时支持 int 和 string，可以添加辅助函数
def get_order_state(status) -> OrderState:
    """根据 API 返回的状态值获取 OrderState"""
    if isinstance(status, int):
        return ORDER_STATE.get(status, OrderState.PENDING_CREATE)
    # 兼容字符串类型（如果 API 同时返回字符串）
    status_str_map = {
        "open": OrderState.OPEN,
        "filled": OrderState.FILLED,
        "canceled": OrderState.CANCELED,
        "cancelled": OrderState.CANCELED,
    }
    return status_str_map.get(str(status).lower(), OrderState.PENDING_CREATE)

# === 资金费率 ===
FUNDING_RATE_UPDATE_INTERNAL_SECOND = 60
CURRENCY = "USDC"

# === 速率限制 ===
HEARTBEAT_TIME_INTERVAL = 30.0
MAX_REQUEST = 1200
ALL_ENDPOINTS_LIMIT = "All"

RATE_LIMITS = [
    RateLimit(ALL_ENDPOINTS_LIMIT, limit=MAX_REQUEST, time_interval=60)
]

# === 错误消息 ===
ORDER_NOT_EXIST_MESSAGE = 1139
ORDER_NOT_FOUND_CODE = 1139