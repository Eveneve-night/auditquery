# 测试

在 Python 3.12 环境安装 `requirements-product.txt`，从仓库根目录运行：

```powershell
python tools/test_product.py
```

`unit/` 包含行为与契约测试；`fixtures/` 提供公开样例。默认入口使用临时存储、虚构凭据及模拟服务，不需要真实 Key。增加测试时优先构造最小的合成输入，并注明额外依赖。需要按模块分组运行时，用 `python tools/test_specialized.py --list` 查看已声明的专项套件。专项运行范围见[测试策略](../docs/product/TESTING.md)。
