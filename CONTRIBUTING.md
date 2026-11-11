# 贡献指南

欢迎提交问题、文档修订、测试与代码。普通问题请通过仓库 Issues 反馈，附版本、操作系统、最小复现步骤、预期和实际结果。请删去凭据、真实数据和实例路径。安全问题请按[安全策略](SECURITY.md)处理。

## 提交修改

1. 阅读[架构](docs/product/ARCHITECTURE.md)及适用的 [开发约定](AGENTS.md)，从目标分支创建任务分支。
2. 使用 Python 3.12 安装 `requirements-product.txt`，运行 `python tools/test_product.py`。按[测试策略](docs/product/TESTING.md)补充受影响模块的测试。
3. PR 说明问题、改动、兼容性、验证结果和未验证项。不要把模拟结果当作真实服务验收。
4. 涉及权限、凭据、SQL 执行或模型外发的变更须独立安全复核。新增数据或代码时核对授权并保留第三方通知。

不要提交实例数据、真实 Key 或未授权资产。
