# 验证指南

运行 `python tools/test_product.py` 执行与 CI 一致的离线核心套件。它覆盖凭据、身份/CSRF、只读 SQL、预算、证据、文档生命周期、HTTP/重启、快照与数据源存储。

修改布局解析时，设置 `PYTHONPATH=src` 后运行 `python -m unittest discover -s tests/unit -p 'test_round2_document*.py'`。其他专项按改动选择，并核对其输入及外部依赖；全目录发现不是受支持的默认验收入口，部分专项需要公开源码树之外的输入。

报告运行命令、版本、通过/失败/跳过及未验证范围。工程回归不等于真实模型服务验收或业务准确率。
