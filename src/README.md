# 源码导航

| 路径 | 职责 |
|---|---|
| `backend/product/` | 服务入口、管理员认证、会话、加密存储和文档生命周期 |
| `backend/api/` | API 与在线问答服务接线 |
| `backend/agent/` | 路由、澄清和循环预算 |
| `backend/nl2sql/` | SQL 候选校验与只读执行 |
| `backend/llm/` | 模型适配、网关、错误与用量账本 |
| `backend/data_sources/` | 数据源契约和事务 Registry，不等同于可用连接器 |
| `frontend/` | 产品管理界面、访客页面和离线演示 |
| `shared/` | 跨模块契约 |

从 [产品服务入口](backend/product/server.py) 追踪调用关系，边界详见[架构](../docs/product/ARCHITECTURE.md)。修改 SQL 链前阅读[模块约定](backend/nl2sql/AGENTS.md)。
