# 成品代码目录

当前已实现基础循环守卫、模型 Provider、可靠性网关、D4 离线 Agent 纵切和 D4-07 本机演示前端：

```text
src/
├─ backend/    API、领域服务、agent 状态机、NL2SQL、RAG、安全网关
├─ frontend/   D4-07 零依赖本机演示、证据卡片和轨迹时间线
└─ shared/     API schema、事件协议和跨模块契约
```

规则：领域逻辑不依赖具体 Web 框架；模型、数据库、解析器、检索器都通过接口适配；禁止在源码中保存密钥。

当前实现：

- `backend/agent/loop_guard.py`：依据版本化策略限制状态步数、调用次数、重复动作、时长、Token、费用和安全违规。
- `backend/llm/contracts.py`：供应商无关的请求与结果契约。
- `backend/llm/bailian.py`：仅允许 `qwen3.7-flash`、一次调用且无隐藏重试的百炼适配器。
- `backend/llm/credentials.py`：只读取两个显式外部路径的密钥组件。
- `backend/llm/errors.py` 与 `usage.py`：标准错误分类、脱敏和不保存正文的用量账本。
- `backend/llm/gateway.py`：有限重试、熔断/半开、一次 JSON 修复和 LoopGuard 接入。

当前 D4-07 演示只接受版本化公开场景 ID；普通单元测试继续保持离线，不得自动调用真实 API。任意自然语言在线业务链、经验校准和隐藏评测尚未实现。
