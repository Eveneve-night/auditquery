# 测试目录

当前已具备模型评测资产、循环守卫、Provider/密钥/脱敏单元测试和离线验证报告；后续继续按下列结构扩展：

```text
tests/
├─ unit/          确定性规则与纯函数
├─ integration/   数据库、模型、解析、检索和状态机
├─ e2e/           浏览器与完整任务脚本
├─ safety/        SQL 越权、提示注入、数据隔离
├─ performance/   两档/三档规模、并发和资源使用
├─ fixtures/      可公开的固定样本
├─ goldens/       金标准与标注版本（受控）
└─ reports/       可复核的离线/在线评测报告
```

当前证据：

- `goldens/model-eval/v1/`：60 条首批模型金标准。
- `goldens/loop-guard/v1/`：12 条循环攻击序列。
- `unit/test_loop_guard.py`：循环限制单元测试。
- `unit/test_llm_credentials.py`：显式密钥来源、回退与脱敏测试。
- `unit/test_bailian_provider.py`：单模型契约、错误分类、用量及安全测试。
- `unit/test_llm_gateway.py`：有限重试、熔断/半开、JSON 修复和预算边界测试。
- `reports/asset-validation-v1.json`：离线资产校验报告。
- `reports/provider-foundation-v1.json`：20 项离线单元测试证据。
- `reports/reliability-gateway-v1.json`：32 项离线单元测试和D2可靠性证据。
- `reports/bailian-connectivity-qwen3.7-flash.json`：单次百炼连通测试脱敏报告。

首次运行 D3 契约测试前安装固定的开发依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

`jsonschema` 仅用于本地 Draft 2020-12 契约校验；安装后执行测试不访问网络或模型 API。

`validation` 和未来的 `test` 集不得被提示词或少样本示例读取。
