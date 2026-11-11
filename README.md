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

## 仓库文件简介

### 目录

| 目录 | 作用 |
|---|---|
| [`.github/`](.github/) | GitHub 自动化与协作模板。查看离线回归工作流或 PR 提交要求时从这里开始。 |
| [`configs/`](configs/README.md) | 接口契约、提示词、路由和预算等非敏感配置。不存真实密钥。 |
| [`data/`](data/README.md) | 随项目提供的 SQLite 样例、合成 PDF、数据字典及来源清单。不是部署实例的存储目录。 |
| [`docs/product/`](docs/product/) | 产品范围、架构、模型服务配置、测试、资产许可和路线图。 |
| [`domain-packs/`](domain-packs/) | 按业务领域组织术语、别名、表列映射、查询槽位和验证规则。领域包描述数据含义，不负责连接数据库。 |
| [`infra/`](infra/README.md) | 本机启动、实例管理和备份恢复脚本，供部署与运维使用。 |
| [`src/`](src/README.md) | 产品源码：后端、前端与共享契约。修改应用行为时从这里定位。 |
| [`tests/`](tests/README.md) | 回归用例和合成测试夹具，验证权限、查询、证据和运行行为。 |
| [`tools/`](tools/README.md) | 运行测试、生成样例、校验资产与执行诊断的辅助脚本，不是应用启动入口。 |

### 根目录文件

| 文件 | 作用 |
|---|---|
| [`.dockerignore`](.dockerignore) | 控制哪些文件不发送到 Docker 构建上下文，减少无关文件和本地数据进入构建过程。 |
| [`.env.example`](.env.example) | 环境变量参考，列出外部主密钥文件路径和访客限流配置示例。不是实际配置，不存密钥值。 |
| [`.gitattributes`](.gitattributes) | 指定 Git 如何处理特定文件，例如将 PDF 视为二进制、固定生成清单的换行格式。 |
| [`.gitignore`](.gitignore) | 让 Git 忽略未跟踪的本地凭据、缓存和运行产物；不会移除已提交的文件。 |
| [`AGENTS.md`](AGENTS.md) | 给代码代理和维护者的工作约定，说明修改、测试与安全边界。 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 给贡献者的指南，说明如何报告问题、验证修改和提交 PR。 |
| [`Dockerfile`](Dockerfile) | 定义单个应用镜像如何构建，包括运行依赖、应用文件和默认入口。 |
| [`LICENSE`](LICENSE) | 项目自有内容的 MIT 许可正文。第三方内容另见其许可声明。 |
| [`README.md`](README.md) | 项目首页：介绍用途、功能边界、快速试用和阅读入口。 |
| [`SECURITY.md`](SECURITY.md) | 说明如何安全报告漏洞，以及本地部署与在线服务的数据外发注意事项。 |
| [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) | 列出随项目分发的第三方内容及其版权、许可说明。 |
| [`compose.yaml`](compose.yaml) | 定义如何运行应用容器，包括端口映射、实例卷和主密钥文件挂载。 |
| [`requirements-dev.txt`](requirements-dev.txt) | 开发环境依赖，含浏览器自动化等开发工具；只试用应用不必安装这一组。 |
| [`requirements-product.txt`](requirements-product.txt) | 产品运行所需的 Python 依赖及固定版本；本地试用和容器构建使用这一组。 |

项目自有代码采用 [MIT](LICENSE)。数据和依赖的许可见[第三方声明](THIRD_PARTY_NOTICES.md)和[资产许可](docs/product/ASSET-LICENSES.md)。
