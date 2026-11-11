# 开发与运维工具

- `test_product.py`：默认离线产品回归；从仓库根目录执行。
- `data_assets/`、`rag/`、`schema_linking/`：生成合成数据、文档夹具和元数据。
- `evaluation/`：专项校验和评测。运行前检查输入、依赖与输出路径，不是默认测试入口。
- `demo/`、`deployment/`：演示和部署探针。
- `provider/`：显式在线连通测试，可能产生外部调用和费用；普通开发不运行。

默认验证见[测试策略](../docs/product/TESTING.md)，实例备份工具见[部署指南](../infra/README.md)。
