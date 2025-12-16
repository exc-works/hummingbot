#!/bin/bash
# 生成多个Hummingbot实例的配置脚本（纯Shell版本）
# 无需Python环境，直接在服务器上运行

set -e

# 默认配置（脚本只负责生成配置，不负责构建镜像）
STRATEGY_FILE="${STRATEGY_FILE:-perpetual_market_making.yml}"
DERIVATIVE="${DERIVATIVE:-binance_perpetual}"
LEVERAGE="${LEVERAGE:-5}"
POSITION_MODE="${POSITION_MODE:-One-way}"
BID_SPREAD="${BID_SPREAD:-0.8}"
ASK_SPREAD="${ASK_SPREAD:-0.8}"
ORDER_AMOUNT="${ORDER_AMOUNT:-0.5}"
ORDER_REFRESH_TIME="${ORDER_REFRESH_TIME:-15}"
# 镜像名默认使用本地预先构建好的 :dev 标签；
# 可通过环境变量覆盖，例如：
# IMAGE="hummingbot/hummingbot:test" ./generate_instances.sh -f trading_pairs.txt
IMAGE="${IMAGE:-hummingbot/hummingbot:dev}"

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 显示帮助信息
show_help() {
    cat << EOF
生成多个Hummingbot实例的配置脚本

用法: $0 [选项] <交易对文件或交易对列表>

选项:
    -f, --file FILE              从文件读取交易对列表（每行一个）
    -p, --password PASSWORD      配置文件密码 (默认: admin)
    -s, --strategy FILE          策略配置文件名 (默认: perpetual_market_making.yml)
    -d, --derivative NAME        交易所名称 (默认: binance_perpetual)
    -l, --leverage NUM           杠杆倍数 (默认: 20)
    -m, --position-mode MODE     持仓模式 One-way 或 Hedge (默认: One-way)
    --bid-spread NUM             买单价差百分比 (默认: 0.5)
    --ask-spread NUM             卖单价差百分比 (默认: 0.5)
    --order-amount NUM           订单数量 (默认: 1.0)
    --order-refresh-time NUM     订单刷新时间/秒 (默认: 60)
    -h, --help                   显示此帮助信息

环境变量:
    也可以通过环境变量设置上述参数，例如:
    CONFIG_PASSWORD=xxx DERIVATIVE=binance_perpetual $0 -f trading_pairs.txt

示例:
    # 从文件读取交易对
    $0 -f trading_pairs.txt

    # 直接指定交易对（逗号分隔）
    $0 BTC-USDT,ETH-USDT,BNB-USDT

    # 使用环境变量
    CONFIG_PASSWORD=mypass LEVERAGE=10 $0 -f trading_pairs.txt
EOF
}

# 解析命令行参数
TRADING_PAIRS_FILE=""
TRADING_PAIRS_LIST=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -f|--file)
            TRADING_PAIRS_FILE="$2"
            shift 2
            ;;
        -p|--password)
            CONFIG_PASSWORD="$2"
            shift 2
            ;;
        -s|--strategy)
            STRATEGY_FILE="$2"
            shift 2
            ;;
        -d|--derivative)
            DERIVATIVE="$2"
            shift 2
            ;;
        -l|--leverage)
            LEVERAGE="$2"
            shift 2
            ;;
        -m|--position-mode)
            POSITION_MODE="$2"
            shift 2
            ;;
        --bid-spread)
            BID_SPREAD="$2"
            shift 2
            ;;
        --ask-spread)
            ASK_SPREAD="$2"
            shift 2
            ;;
        --order-amount)
            ORDER_AMOUNT="$2"
            shift 2
            ;;
        --order-refresh-time)
            ORDER_REFRESH_TIME="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        -*)
            echo -e "${RED}错误: 未知选项 $1${NC}" >&2
            show_help
            exit 1
            ;;
        *)
            if [ -z "$TRADING_PAIRS_LIST" ]; then
                TRADING_PAIRS_LIST="$1"
            else
                echo -e "${RED}错误: 只能指定一个交易对列表${NC}" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

# 获取交易对列表
get_trading_pairs() {
    if [ -n "$TRADING_PAIRS_FILE" ]; then
        if [ ! -f "$TRADING_PAIRS_FILE" ]; then
            echo -e "${RED}错误: 文件不存在: $TRADING_PAIRS_FILE${NC}" >&2
            exit 1
        fi
        # 从文件读取，忽略空行和注释行
        grep -v '^[[:space:]]*#' "$TRADING_PAIRS_FILE" | grep -v '^[[:space:]]*$' | tr '\n' ' '
    elif [ -n "$TRADING_PAIRS_LIST" ]; then
        # 从命令行参数读取，逗号分隔
        echo "$TRADING_PAIRS_LIST" | tr ',' ' '
    else
        echo -e "${RED}错误: 请指定交易对文件(-f)或交易对列表${NC}" >&2
        show_help
        exit 1
    fi
}

# 将交易对名称转换为安全的目录名
sanitize_name() {
    echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/-/_/g' | sed 's/\//_/g'
}

# 创建实例目录结构
create_instance_dirs() {
    local instance_dir="$1"
    mkdir -p "$instance_dir/conf/connectors"
    mkdir -p "$instance_dir/conf/strategies"
    mkdir -p "$instance_dir/conf/controllers"
    mkdir -p "$instance_dir/conf/scripts"
    mkdir -p "$instance_dir/logs"
    mkdir -p "$instance_dir/data"
    mkdir -p "$instance_dir/certs"
}

# 生成策略配置文件
generate_strategy_config() {
    local trading_pair="$1"
    local config_file="$2"
    
    cat > "$config_file" << EOF
########################################################
###       Perpetual market making strategy config         ###
########################################################

template_version: 6
strategy: perpetual_market_making

# derivative and token parameters.
derivative: $DERIVATIVE

# Token trading pair for the exchange, e.g. BTC-USDT
market: caishen_perpetual_testnet

# What leverage to used
leverage: 5

# Position mode to use, hedge mode or one-way position mode.
position_mode: One-way

# How far away from mid price to place the bid order.
# Spread of 1 = 1% away from mid price at that time.
bid_spread: $BID_SPREAD

# How far away from mid price to place the ask order.
# Spread of 1 = 1% away from mid price at that time.
ask_spread: $ASK_SPREAD

# Minimum Spread
minimum_spread: -1

# Time in seconds before cancelling and placing new orders.
order_refresh_time: $ORDER_REFRESH_TIME

# The spread (from mid price) to defer order refresh process to the next cycle.
order_refresh_tolerance_pct: -1

# Size of your bid and ask order.
order_amount: $ORDER_AMOUNT

# long position take profit spread
long_profit_taking_spread: 0

# short position take profit spread
short_profit_taking_spread: 0

# Spread from position entry price to place a stop-loss order to close position
stop_loss_spread: 0

# Time to wait before refreshing a stop loss order that has not been executed
time_between_stop_loss_orders: 5

# Spread to include in stop loss orders covering possible price slippages in the market
stop_loss_slippage_buffer: 0

# Price band ceiling.
price_ceiling: -1

# Price band floor.
price_floor: -1

# Number of levels of orders to place on each side of the order book.
order_levels: 25

# Increase or decrease size of consecutive orders after the first order (if order_levels > 1).
order_level_amount: 0

# Order price space between orders (if order_levels > 1).
order_level_spread: 0

# How long to wait before placing the next order in case your order gets filled.
filled_order_delay: 60

# Whether to enable order optimization mode (true/false).
order_optimization_enabled: false

# The depth in base asset amount to be used for finding top ask (for order optimization mode).
ask_order_optimization_depth: 0

# The depth in base asset amount to be used for finding top bid (for order optimization mode).
bid_order_optimization_depth: 0

# The price source (current_market/external_market/custom_api).
price_source: current_market

# The price type (mid_price/last_price/last_own_trade_price/best_bid/best_ask).
price_type: mid_price
EOF
}

# 生成docker-compose服务配置（始终使用预先构建好的镜像）
generate_docker_service() {
    local safe_name="$1"
    local trading_pair="$2"
    local container_name="hummingbot_${safe_name}"
    
    cat << EOF
  ${safe_name}:
    container_name: ${container_name}
    image: ${IMAGE}
    volumes:
      - ./instances/${safe_name}/conf:/home/hummingbot/conf
      - ./instances/${safe_name}/conf/connectors:/home/hummingbot/conf/connectors
      - ./instances/${safe_name}/conf/strategies:/home/hummingbot/conf/strategies
      - ./instances/${safe_name}/conf/controllers:/home/hummingbot/conf/controllers
      - ./instances/${safe_name}/conf/scripts:/home/hummingbot/conf/scripts
      - ./instances/${safe_name}/logs:/home/hummingbot/logs
      - ./instances/${safe_name}/data:/home/hummingbot/data
      - ./instances/${safe_name}/certs:/home/hummingbot/certs
      - ./scripts:/home/hummingbot/scripts
      - ./controllers:/home/hummingbot/controllers
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "5"
    # 使用交互模式，可以配置API密钥和策略
    tty: true
    stdin_open: true
    network_mode: host
    restart: unless-stopped
EOF
}

# 主函数
main() {
    echo -e "${GREEN}开始生成Hummingbot多实例配置...${NC}"
    echo ""
    
    # 获取交易对列表
    local trading_pairs
    trading_pairs=($(get_trading_pairs))
    
    if [ ${#trading_pairs[@]} -eq 0 ]; then
        echo -e "${RED}错误: 没有找到交易对${NC}" >&2
        exit 1
    fi
    
    echo -e "${YELLOW}配置参数:${NC}"
    echo "  交易对数量: ${#trading_pairs[@]}"
    echo "  策略文件: $STRATEGY_FILE"
    echo "  交易所: $DERIVATIVE"
    echo "  杠杆: $LEVERAGE"
    echo "  持仓模式: $POSITION_MODE"
    echo "  买单价差: $BID_SPREAD%"
    echo "  卖单价差: $ASK_SPREAD%"
    echo "  订单数量: $ORDER_AMOUNT"
    echo "  订单刷新时间: ${ORDER_REFRESH_TIME}秒"
    echo "  使用镜像: $IMAGE"
    echo ""
    
    # 创建instances目录
    local instances_dir="instances"
    mkdir -p "$instances_dir"
    
    # 生成docker-compose.yml文件头
    local compose_file="docker-compose.multiple.yml"
    cat > "$compose_file" << EOF
version: '3.8'

services:
EOF
    
    # 为每个交易对生成配置
    local count=0
    for trading_pair in "${trading_pairs[@]}"; do
        trading_pair=$(echo "$trading_pair" | xargs)  # 去除空格
        if [ -z "$trading_pair" ]; then
            continue
        fi
        
        count=$((count + 1))
        local safe_name=$(sanitize_name "$trading_pair")
        local instance_dir="$instances_dir/$safe_name"
        
        echo -e "${GREEN}[$count/${#trading_pairs[@]}] 生成 $trading_pair 的配置...${NC}"
        
        # 创建目录结构
        create_instance_dirs "$instance_dir"
        
        # 生成策略配置文件
        local strategy_config_file="$instance_dir/conf/strategies/$STRATEGY_FILE"
        generate_strategy_config "$trading_pair" "$strategy_config_file"
        
        # 添加到docker-compose.yml
        generate_docker_service "$safe_name" "$trading_pair" >> "$compose_file"
        
        echo -e "  ${GREEN}✓${NC} 配置已生成: $instance_dir"
    done
    
    echo ""
    echo -e "${GREEN}所有配置已生成完成！${NC}"
    echo ""
    echo -e "${YELLOW}生成的文件:${NC}"
    echo "  - $compose_file (包含所有 ${count} 个服务)"
    echo "  - $instances_dir/ (包含所有实例配置)"
    echo ""
    echo -e "${YELLOW}下一步操作:${NC}"
    echo "1. 启动所有实例:"
    echo "   docker compose -f $compose_file up -d"
    echo ""
    echo "2. 进入容器配置API密钥和策略（示例）:"
    echo "   docker exec -it hummingbot_<instance_name> /bin/bash"
    echo "   ./bin/hummingbot_quickstart.py"
    echo ""
}

# 运行主函数
main

