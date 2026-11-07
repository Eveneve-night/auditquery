# 测试策略

Python 3.12，从根目录执行 `python tools/test_product.py`。范围明确覆盖凭据、身份/CSRF、SQL 安全、预算、证据、文档生命周期、产品服务/重启、快照和数据源存储。只使用临时存储及虚构凭据，不需要真实模型 Key。

完整发现：设置 PYTHONPATH=src，执行 `python -m unittest discover -s tests/unit -p "test_*.py"`。历史报告、旧 Git 对象、评测 gold 已排除，部分旧检查会失败；完整发现结果与核心集必须分别报告，不能声称全仓通过。

`test_grounded_asset_history.py` 保留原来的评测资产匹配、问题/答案泄漏、文档族划分断言。`test_grounded_answer_contract.py` 保留产品 schema、成功/拒答/非法输入、提示边界断言，不再在类初始化时加载旧 gold。新的 `tests/fixtures/product/loop-attacks.jsonl` 是原创合成的事件上限/重复行为攻击集，采用项目 MIT 许可。

保留仍有行为价值的混合历史测试，逐项提取安全、敏感信息净化、失败关闭及指标计算断言后，才可删除未被使用的旧工具。默认核心集不是历史迁移完成的证明。CI 使用同一核心集，不运行真实 API、旧发布 Gate 或 LFS 下载。

验证日志与确切数量在私人交接中记录；未验证 Docker 冷安装时不得宣称通过。
