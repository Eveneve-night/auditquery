# 架构与真实依赖

部署采用模块化单体，入口 `backend.product.server`，静态 UI 为 `src/frontend/d5-console`。离线演示由 D4DemoApplication 延迟装配；在线访客由 visitor_rag/build_rag_first_agent 串接文档检索和 visitor_sql/build_approved_sql_adapter。

SQL 链：api/online_sql_service → proposal、领域语义/Schema → SafetyGate → ReadonlyExecutor。固定 Chinook 领域仍是当前限制。模型没有数据库、文件或凭据直接权限。

product/auth、secret_store、document_store、visitor 负责身份、CSRF、加密凭据和生命周期。data_sources 提供事务 Registry/CAS/幂等、契约与授权；Connector 执行能力仍为 unsupported，管理 HTTP 尚未接通。

D3/D4/D5 及 ROUND2 是内部兼容协议标识，不应仅为命名修改字段或行为。当前保留历史工具中仍被测试 import 的模块，先迁移其安全、净化、计算和失败关闭断言再删除。tools/evaluation/validate_d5_05_postmerge_gate 仍依赖 actions receipt 工具；不能仅按名称删除。

本轮改动：外部文件凭据配置移除开发机默认路径，集中校验显式路径；缺失或空路径统一返回 CredentialError。没有改动 SQL/授权/预算的执行边界。

后续顺序：真实受控 Adapter → 授权快照与 Registry → HTTP/UI → 跨层验收。避免全仓换框架或过早拆服务。
