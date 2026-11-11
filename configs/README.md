# 配置与契约

- `schemas/`、`contracts/`、`examples/`：结构化接口、版本化约束与示例。
- `prompts/`、`policies/`、`routing/`：任务提示、执行边界和路由配置。
- `providers/`：非敏感服务商配置，不是产品管理员设置的存储位置。
- `demo/`、`evaluation/`：演示输入与专项评测配置，不是用户数据。

配置在线服务请阅读[模型服务设置](../docs/product/MODEL-SERVICES.md)。禁止在本目录保存 Key、密码、主密钥或私有连接串。修改契约须说明兼容性并补测试。
