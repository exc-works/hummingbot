# Proto 文件导入路径修复指南

## 问题描述

生成的 proto 文件中，导入路径与实际的模块路径不匹配，导致：
1. `google.protobuf` 应该使用系统的（在 site-packages 中）
2. `google.api` 应该使用 `third_party/google/api` 中的
3. `gogoproto` 应该使用 `third_party/gogoproto` 中的
4. `schema` 应该使用 `deepproto/schema` 中的（相对导入）

## 解决方案

### 1. 修复编译脚本 (`create_proto_file.sh`)

脚本已经更新，添加了：
- 使用绝对路径编译
- 自动修复生成的导入路径（将 `caishen_perpetual.deepproto.third_party.*` 替换为正确的导入）

### 2. 创建路径管理文件

#### `deepproto/__init__.py`
- 将 `deepproto` 和 `third_party` 添加到 `sys.path`
- 确保 `third_party` 在 `site-packages` 之后，这样系统的 `google.protobuf` 优先

#### `deepproto/third_party/google/api/__init__.py`
- 确保 `http_pb2` 在 `annotations_pb2` 之前导入
- 强制注册 DESCRIPTOR 到 descriptor pool

### 3. 手动修复（如果自动修复失败）

如果生成的 proto 文件中仍有错误的导入路径，可以手动修复：

```bash
# 查找所有需要修复的文件
find deepproto -name "*_pb2.py" -exec grep -l "from caishen_perpetual.deepproto" {} \;

# 批量替换
find deepproto -name "*_pb2.py" -exec sed -i '' \
    -e 's/from caishen_perpetual\.deepproto\.third_party\.google\.api/from google.api/g' \
    -e 's/from caishen_perpetual\.deepproto\.third_party\.gogoproto/from gogoproto/g' \
    {} \;
```

## 验证

运行以下命令验证修复是否成功：

```bash
conda activate hummingbot
python -c "
from hummingbot.connector.derivative.caishen_perpetual.deepproto import __init__ as deepproto_init
from google.api import annotations_pb2
from gogoproto import gogo_pb2
from hummingbot.connector.derivative.caishen_perpetual.deepproto.api import tx_pb2
print('✅ All imports successful!')
"
```

## 注意事项

1. **不要手动编辑生成的 `*_pb2.py` 文件**（除非是临时修复）
2. **重新编译 proto 文件后**，需要重新运行修复脚本
3. **确保 `deepproto/__init__.py` 在任何 proto 文件导入之前被导入**

