# 配置目录

当前已保存百炼 Flash 非敏感 Provider 配置、循环策略和 JSON schema。真实 Token、密码、连接串不得进入本目录。

## 规划结构

```text
configs/
├─ environments/    dev、test、demo 的非敏感差异
├─ providers/       Provider、Base URL、模型、协议和价格快照
├─ models/          后续 ModelRegistry 能力与版本记录
├─ prompts/         Prompt 模板与版本
├─ schemas/         结构化输入/输出 schema
├─ policies/        Token、温度、超时、重试、熔断和预算
└─ logging/         脱敏、采样和保留期
```

当前 Provider 配置：`providers/bailian-qwen3.7-flash.json`。它只允许北京百炼
`qwen3.7-flash`，不包含真实 Key。

D3 公共契约候选包括：

- `schemas/prompt-contract.v1.schema.json`：Prompt 元数据、版本、路径和固定生成参数。
- `schemas/task-result-envelope.v1.schema.json`：四类任务统一成功/错误封装。
- `prompts/README.md`：任务 Prompt 目录、命名和冻结规则。

公共契约须经 B/C 评审后才能冻结；各任务的业务 Prompt 和结果 schema 由 D3-01、
D3-02、D3-03 分别补充。

## 仅通过批准的外部文件/环境/密钥服务提供的值

- 当前百炼 API Key；运行时从批准的项目外路径读入内存。
- 未来环境变量名建议为 `DASHSCOPE_API_KEY`，迁移时不得把值写入配置。
- 数据库密码和任何私有连接凭据

这些值不得出现在 YAML/JSON、命令历史、截图、测试 fixture 或聊天记录中。

## 可进入非敏感配置的字段

- 当前 Provider、Base URL、模型 ID 和地域；备用字段只有新 ADR 后才启用。
- Prompt、schema、policy 的版本引用。
- 输入/输出 Token 上限、温度、连接/读取超时、重试次数。
- 熔断窗口、失败阈值和恢复时间；当前 fallback 必须关闭。
- 价格快照日期、输入/输出单价、日/月预算和告警比例。
- Trace 采样率、数据分级、脱敏规则和日志保留天数。

## 环境隔离

- `dev`：允许 Prompt 调试，低预算，使用开发 Key。
- `test`：固定模型/Prompt/schema/policy，禁止读取 prompt-dev 之外的答案。
- `demo`：候选版只读配置、独立 Key、硬预算和单 Provider/网络降级预检。

启动时必须检查必需变量、模型 ID、Base URL、预算和策略引用；缺失或组合不兼容时应拒绝启动，而不是使用隐式默认值。
