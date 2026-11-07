# 工具目录

非产品运行时代码按用途分类：

```text
tools/
├─ data-prep/     数据导入、文档摄取、manifest 和 hash
├─ evaluation/    NL2SQL、RAG、澄清、安全和性能评测
├─ provider/      显式授权的模型连通与兼容测试
├─ demo/          演示环境预检、样例加载和录制辅助
└─ maintenance/   版本检查、数据校验和报告生成
```

工具输出必须写入明确的结果目录，不静默修改 `data/raw/`。

当前工具：

- `evaluation/validate_seed_assets.py`：完全离线校验 60 条金标准、Chinook hash/schema/SQL 结果、RAG 证据 ID、循环策略与攻击集。它不会查找或读取 `api key.txt`。
- `provider/bailian_connectivity_check.py`：对 `qwen3.7-flash` 发起一次最小请求，只保存脱敏的状态、时延、Token、费用估算与响应哈希。
