# AuditQuery

AuditQuery 是面向开发者、数据分析人员和小团队的本地只读数据问答工具。查询结果可核对 SQL、数据来源和实际执行记录；文档摘录可核对原文及页码。

## 能做什么

- 对随附的 Chinook 数据库执行受安全门约束的单表只读查询；缺少必要信息时请求澄清。
- 检索已发布的 PDF，展示带页码的确定性摘录。
- 管理员可配置兼容 Chat Completions 的模型服务；服务商、模型和 Key 不固定。此能力以所检出的版本为准。

当前不支持任意 SQLite 数据库接入、JOIN、跨源问答或生成式文档综合回答。数据源管理界面为预览，离线演示使用固定样例。

## 本地试用

需要 Python 3.12。在仓库根目录执行以下 PowerShell 命令；它为一次性离线试用生成进程级主密钥：

```powershell
python -m pip install -r requirements-product.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$env:ICT_SECRET_MASTER_KEY_B64 = python -c "import secrets; import base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
$instance = Join-Path ([System.IO.Path]::GetTempPath()) ('auditquery-' + [guid]::NewGuid())
python -m backend.product.server serve --instance $instance --mode offline
```

打开 `http://127.0.0.1:8000/demo`。离线试用不需要模型 Key。若需要重启后保留实例，请使用[部署指南](infra/README.md)中的持久主密钥方案，不要为同一个实例重新生成密钥。

## 按你的需求阅读

| 我想做什么 | 从哪里开始 |
|---|---|
| 了解适用场景和产品边界 | [产品范围](docs/product/PRODUCT.md) |
| 安装、配置在线模型、备份恢复 | [部署](infra/README.md) · [模型服务设置](docs/product/MODEL-SERVICES.md) |
| 阅读或修改代码 | [源码导航](src/README.md) · [架构](docs/product/ARCHITECTURE.md) · [贡献指南](CONTRIBUTING.md) |
| 运行测试或查找样例 | [测试](tests/README.md) · [数据](data/README.md) · [工具](tools/README.md) |
| 反馈缺陷或安全问题 | [问题反馈](CONTRIBUTING.md) · [安全策略](SECURITY.md) |

项目自有代码采用 [MIT](LICENSE)。数据和依赖的许可见[第三方声明](THIRD_PARTY_NOTICES.md)和[资产许可](docs/product/ASSET-LICENSES.md)。
