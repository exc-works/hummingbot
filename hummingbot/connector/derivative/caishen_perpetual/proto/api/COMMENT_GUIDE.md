# proto/api 注释规范

`proto/api/*.proto` 的注释是**对外 API 契约**的源头：它既生成 swagger 文档，也经
`i18n/api_docs/*.po` 翻译成多语言、并随公开 proto 模块发布给第三方。所以注释必须当成
**给外部消费者读的接口文档**来写，而不是给内部同事的随手备注。

## 五条标准

1. **准确**——必须与代码实际行为一致。改注释前对照实现核实：
   `proto/api` rpc → `api/controller/*.go` → `api/service/*.go` 的 builder；字段单位/可空/
   计算口径以 builder 代码为准。拿不准的语义见下方"不确定项"。

2. **不暴露内部实现**——注释里不得出现：内部 Go 类型/包路径（`github.com/exc-works/...`）、
   `protoc-gen-*`、`share.CodeXxx`、引擎/WAL/snapshot 等内部机制、DB 表/列、内部文件路径、
   `TODO`/`FIXME`、大段内嵌 JSON schema。只描述"字段是什么、取值含义、约束"。

3. **精确**——补齐消费者必需的信息：
   - 时间：标明 `Unix 毫秒` 或 `Unix 秒`（以代码 `.UnixMilli()`/`.Unix()` 为准）。
   - 金额：标明计价币种与是否含手续费（能从代码确认时）。
   - 可空/可选：说明何时为 `null`/空串/0、是否 `optional`。
   - 域：标明适用 `Perp`/`Spot`/`Prediction`，同名异义字段要分别说明。

4. **枚举对齐**——字段注释里硬编码的枚举值列表，必须与 `proto/schema/*.proto` 里的权威
   `enum` 定义一致（值、含义、顺序）。格式统一：`0=含义, 1=含义`（用 `=`、逗号分隔、不省略、
   不乱序）。新增枚举优先在 schema 定义并复用，而非在注释里硬编码。

5. **规范、非口语**——书面、简洁、术语统一。禁止"可能为null""如xxx"等口语；可空写
   "未命中时为 null"之类的确定表述。不夹与本字段无对应关系的整段英文（注释统一中文源，
   英文由 i18n 产出）。

## 不确定项怎么办

代码也难定论的语义（计算是否含手续费、跨域口径差异、复杂可空条件等）：
- **只做安全清理**（口语/格式/内部泄露），**不改其实质断言、不猜测**——避免把一个错注释
  换成另一个错注释。
- 登记到 `i18n/api_docs/UNVERIFIED.md`（file:line + 现注释 + 代码证据 + 待确认问题），由
  领域同事确认后再改。**不要在 proto 里留 `TODO`**（会随翻译发布出去）。

## 改完注释后（i18n 同步）

注释即翻译源，改动会使 `en.po` 对应条目失效，必须同步：
```bash
make i18n-api-extract        # 刷新 messages.pot / 各 locale .po 的 msgid
# 为改动的中文补写 en.po 英文译文
make i18n-api-check-strict   # en 无空/fuzzy
make gen-proto-public        # 公开产物零中文（CJK guard）
```
