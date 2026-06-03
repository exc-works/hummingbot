##!/bin/bash
## NOTE: protoc is required
## NOTE: the python module `grpc_tools` is required
#
##source .venv/bin/activate
#
##FOLDER=$(pwd)
##I=$(PWD)/proto
##DEST=$(PWD)/deepproto/
##mkdir -p $DEST
##
##python3 -m grpc_tools.protoc -I=$I --python_out=$DEST --grpc_python_out=$DEST $(find $(PWD)/proto -iname "*.proto")
##
##
##create() {
##    for DIR in $(find $1 -type d); do
##    done        touch $DIR/__init__.py
##
##}
##
##create $DEST
#
#
## 设定输入和输出目录
INPUT_DIR="proto/"
OUTPUT_DIR="deepproto/"

# Hummingbot conda env ships protobuf 5.x runtime; gencode must not be newer.
check_protobuf_version() {
    local pb_version
    pb_version=$(python3 -c "import google.protobuf; print(google.protobuf.__version__)" 2>/dev/null || true)
    if [ -z "$pb_version" ]; then
        echo "错误: 未安装 google.protobuf，请先安装 grpcio-tools / protobuf 5.x"
        exit 1
    fi
    local major="${pb_version%%.*}"
    if [ "$major" -ge 6 ] 2>/dev/null; then
        echo "错误: 当前 protobuf runtime 为 ${pb_version}，与 Hummingbot 不兼容。"
        echo "请使用 protobuf 5.x 重新生成 deepproto，例如:"
        echo "  pip install 'protobuf>=5.28,<6' 'grpcio-tools>=1.68,<1.70'"
        exit 1
    fi
    echo "使用 protobuf runtime ${pb_version} 生成 deepproto..."
}

check_protobuf_version

# 创建输出目录（如果不存在）
mkdir -p $OUTPUT_DIR

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$SCRIPT_DIR/proto"
OUTPUT_DIR_ABS="$SCRIPT_DIR/$OUTPUT_DIR"

# 遍历和编译所有 .proto 文件
# 遍历所有子目录中的 .proto 文件
find "$PROTO_DIR" -name "*.proto" | while read proto_file; do
    # 编译 proto 文件
    # 使用绝对路径，并设置正确的 proto_path
    python3 -m grpc_tools.protoc \
        --proto_path="$PROTO_DIR" \
        --proto_path="$PROTO_DIR/third_party" \
        --python_out="$OUTPUT_DIR_ABS" \
        "$proto_file"
done
echo "所有 .proto 文件已成功编译到 $OUTPUT_DIR。"

# 创建所有目录的 __init__.py 文件
create() {
    for DIR in $(find "$1" -type d); do
        touch "$DIR/__init__.py"
    done
}

create "$OUTPUT_DIR_ABS"

# google.protobuf well-known types must come from site-packages, not third_party stubs
rm -rf "$OUTPUT_DIR_ABS/third_party/google/protobuf"
# Regular google/__init__.py shadows site-packages google.protobuf; use PEP 420 namespace merge
rm -f "$OUTPUT_DIR_ABS/third_party/google/__init__.py"

# 修复生成的 proto 文件中的导入路径
# 将错误的绝对导入路径替换为相对导入路径
echo "修复生成的 proto 文件中的导入路径..."

# 修复 third_party 相关的导入
find "$OUTPUT_DIR_ABS" -name "*_pb2.py" -type f | while read pb2_file; do
    # 将 caishen_perpetual.deepproto.third_party 替换为正确的导入
    # 对于 google.api 和 gogoproto，使用相对导入
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' \
            -e 's/from caishen_perpetual\.deepproto\.third_party\.google\.api/from google.api/g' \
            -e 's/from caishen_perpetual\.deepproto\.third_party\.gogoproto/from gogoproto/g' \
            "$pb2_file" 2>/dev/null
    else
        # Linux
        sed -i \
            -e 's/from caishen_perpetual\.deepproto\.third_party\.google\.api/from google.api/g' \
            -e 's/from caishen_perpetual\.deepproto\.third_party\.gogoproto/from gogoproto/g' \
            "$pb2_file" 2>/dev/null
    fi
done

# 修复 annotations_pb2.py 中的依赖路径问题
# 在 annotations_pb2.py 中，确保 http_pb2 在创建 DESCRIPTOR 之前被导入
ANNOTATIONS_PB2="$OUTPUT_DIR_ABS/third_party/google/api/annotations_pb2.py"
if [ -f "$ANNOTATIONS_PB2" ]; then
    echo "修复 annotations_pb2.py 中的依赖路径..."
    # 在 DESCRIPTOR 创建之前，确保 http_pb2 被导入并注册
    if ! grep -q "# Ensure http_pb2 DESCRIPTOR is registered" "$ANNOTATIONS_PB2"; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' \
                -e '/^from google\.api import http_pb2/a\
\
# Ensure http_pb2 DESCRIPTOR is registered in the descriptor pool\
# This must be done before creating annotations_pb2 DESCRIPTOR\
_ = google_dot_api_dot_http__pb2.DESCRIPTOR\
' \
                "$ANNOTATIONS_PB2"
        else
            sed -i \
                -e '/^from google\.api import http_pb2/a\
\
# Ensure http_pb2 DESCRIPTOR is registered in the descriptor pool\
# This must be done before creating annotations_pb2 DESCRIPTOR\
_ = google_dot_api_dot_http__pb2.DESCRIPTOR\
' \
                "$ANNOTATIONS_PB2"
        fi
    fi
fi

echo "完成！"

# Verify generated gencode matches protobuf 5.x (Hummingbot runtime is 5.29.x)
if grep -r "Protobuf Python Version: 6\." "$OUTPUT_DIR_ABS" --include="*_pb2.py" -q 2>/dev/null; then
    echo "错误: 检测到 protobuf 6.x 生成的 *_pb2.py，与 Hummingbot runtime 不兼容。"
    echo "请安装 protobuf 5.x 后重新运行此脚本。"
    exit 1
fi
echo "protobuf 版本检查通过 (gencode 5.x)。"
