# Chinook 领域包来源与许可声明

## 来源

- `source.chinook_database`：Chinook Sample Database v1.4.5（MIT），
  https://github.com/lerocha/chinook-database/releases/tag/v1.4.5 。
  仅读取公开样例数据的聚合值集合（国家枚举、发票年份范围），未包含任何客户个人信息。
- `source.chinook_schema_extract`：本仓库冻结的 schema 描述文件
  `data/schema/chinook-v1.4.5.schema.json`（项目内部内容）。
- `source.d3_contract_vocabulary`：D3 冻结契约的槽位词汇
  （`configs/prompts/clarification/v1/system.md` 契约语义；槽位键名与取值令牌
  和 prompt_dev 输入侧 `context.required_slots` / `input.known_slots` 对齐）。

## 数据边界

- 构建过程不读取任何金标 `expected` / `gold_sql` / `result` 字段。
- validation / test split 内容零进入本包。
- 值证据仅含公开样例数据库的聚合值集合，不含客户级个人数据。
- 本包不含隐藏测试题、答案或金标 SQL。

## 许可与数据级别

- 外部事实：MIT（Chinook）。
- 项目内部内容：随仓库分发。
- 数据级别：包级 L1；`slots.json` 与 `validation-rules.json` 为 L0。
