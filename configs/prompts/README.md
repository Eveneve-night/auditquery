# D3 Prompt 契约目录

本目录只保存已版本化、可评审的任务 Prompt 契约，不保存密钥、真实模型响应或未授权数据。

每类任务使用以下结构：

```text
configs/prompts/<task>/v<major>/
├─ contract.json
└─ system.md
```

`<task>` 只能是 `intent`、`clarification`、`nl2sql` 或 `grounded-answer`。其中
`grounded-answer` 是 RAG 任务的目录名，对应机器字段 `task_type=grounded_answer`。

`contract.json` 必须通过 `configs/schemas/prompt-contract.v1.schema.json`，并引用
`configs/schemas/tasks/<task>-result.v<major>.schema.json`。冻结后的 Prompt、契约或结果
schema 不得原地覆盖；不兼容修改必须创建新的主版本目录和 schema 文件。

任务 Prompt 必须要求模型仅返回一个 JSON 对象。模型输出只是候选结果；SQL 安全校验、
SQL 执行、evidence ID 校验、拒答、重试、费用和终止条件继续由本地代码决定。
