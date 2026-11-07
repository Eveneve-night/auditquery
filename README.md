# AuditQuery

可审计的本地只读数据问答原型。已有固定 Chinook 单表 SQL 候选、安全门与只读执行、澄清/拒答、预算控制、文档页级证据、身份/CSRF、加密凭据与持久化。

SQLite 数据源 Adapter 和管理 HTTP 尚未实现；数据源管理页是预览。文档回答主要为确定性摘录，尚无模型综合答案或跨源融合。离线页面使用示例。

## 启动与测试

Python 3.12，安装 requirements-product.txt，从根目录执行：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
# 设置 ICT_MASTER_KEY_FILE 为仓库外自行生成的主密钥文件。
python -m backend.product.server serve --instance <仓库外实例目录> --mode offline
python tools/test_product.py
```

服务默认绑定 127.0.0.1:8000。`/health/live` 检查存活，`/health/ready` 检查就绪；缺少主密钥时就绪失败。Windows 封装为 infra/start.ps1，容器入口为 compose.yaml。

在线模型可在管理员设置中配置服务商、Base URL、模型和 Key，支持 OpenAI 兼容 Chat Completions；百炼不再是唯一选择。远程 HTTPS、本地回环 HTTP/无认证显式开启，无隐式 fallback。未知单价显示 unknown 并在发送前失败关闭，需填写 CNY 单价后才能调用。其他原生协议未支持，见 [模型服务与迁移](docs/product/MODEL-SERVICES.md)。用户自行配置后的正常产品使用不需逐次人工审批；开发 Agent 仍不擅自读取真实 Key 或消耗付费 API。

核心回归入口不代表全部测试或业务准确率；完整发现仍包含待迁移的历史材料检查，见 [测试策略](docs/product/TESTING.md)。

[产品范围](docs/product/PRODUCT.md) · [架构](docs/product/ARCHITECTURE.md) · [任务](docs/product/TASKS.md) · [开发约定](AGENTS.md)。D3/D4/D5 为内部协议兼容版本标识。

当前是开发中源码原型，不是已完成的通用产品或 v0.1 功能发布。模型配置任务的核心回归为 210 项（含 17 项新专项），相关在线 SQL 离线回归 27 项、前端回归 64 项；原迁移阶段文档专项 13 项的结果只绑定其对应版本。这不代表完整旧测试集通过，也不代表真实服务商在线验收。历史测试迁移、干净安装和容器验收仍列为后续工作。
