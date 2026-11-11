# 验证指南

运行 `python tools/test_product.py` 执行与 CI 一致的离线核心套件（219 项）。它覆盖凭据、身份/CSRF、只读 SQL、预算、证据、文档生命周期、HTTP/重启、快照与数据源存储，并包含 `test_test_entrypoints.py` 对两个测试入口的守卫。

## 专项测试入口

`python tools/test_specialized.py` 运行已声明、可在公开源码树内独立执行的专项套件。加 `--list` 查看分组、文件模式与依赖；加 `--suite <名称>` 只运行指定分组。

| 分组 | 覆盖 | 用例数 |
|---|---|---|
| `round2-document` | 文档布局解析、复杂度分级与复用 | 13 |
| `round2-schema-linking` | 公开用例集上的表与字段定位 | 23 |
| `frontend-console` | 控制台适配层、在线接线、安装与重启探针 | 64 |
| `fin-frontend` | 数据源管理组件与公开业务夹具 | 7 |
| `visitor-rag` | 访客会话、RAG 分发、已发布检索与答案组装 | 33 |

前端相关分组需要 Node.js 运行 ES 模块探针，`round2-document` 需要 `pdfplumber`（两者均已在 `requirements-product.txt`）。退出码含义：`0` 全部通过，`1` 有失败或声明的模式未匹配到文件，`2` 用法错误，`3` 有分组因缺少前置条件未运行。缺依赖时该分组记为 skipped 并返回 `3`，不会被当成通过；`--allow-skip` 只对已执行的部分给出成功。

两个入口都用文件模式声明范围，因此测试文件改名不会静默减少覆盖：模式匹配不到文件即判定失败。

## 已知缺口

`tests/unit/test_round2_contracts.py` 共 24 项，其中 1 项报 `public_example_drifted`，因此未纳入专项入口。原因是 `configs/examples/round2/v1/public-contracts.json` 记录 `domain_pack_ref.manifest_sha256` 为 `26906fd…`，而当前生成值为 `e9f80be…`；域包目录已由 `domain-packs/chinook_music_store/1.0.0` 改为 `domain-packs/chinook/v1` 与 `domain-packs/chinook/v2`，样例未随之重生成。逐字段比对显示两份样例仅有清单哈希与派生 `input_sha256` 不同，其余内容一致。

全目录发现（`python -m unittest discover -s tests/unit`）不是受支持的验收入口：部分专项依赖公开源码树之外的输入，浏览器端用例还会长时间阻塞。

报告运行命令、版本、通过/失败/跳过及未验证范围。工程回归不等于真实模型服务验收或业务准确率。
