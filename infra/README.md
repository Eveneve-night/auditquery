# D5-05 B 本机启动与实例数据（开发中）

本目录是 #76 的 B 侧打包入口。目前已有本机单体服务、管理员/文档/密钥存储接线、受限 multipart 上传入口、访客 Cookie 所有权/限流/重放防护、进程级 HTTP 重启回归、独立健康检查、Docker/Windows 启动骨架以及停机备份恢复。离线启动后可访问 `http://127.0.0.1:8000/demo`，查看明确标注的公开合成场景；该入口复用既有 D4 离线演示，不调用模型。SQL 适配层可在离线测试中显式注入并复用 D4 `OnlineSqlService`；CLI 默认离线，D5 `/api/v3/query` 明确返回 503，绝不把公开场景结果冒充任意访客提问的实时答案。通用在线模式尚缺可信意图路由与在线 RAG，不能把所有问题固定路由到 SQL，也不能把本版本称为可发布产品。

访客 `POST /api/v3/sessions` 会设置 `ict_visitor_session` 的 HttpOnly、SameSite=Strict Cookie；浏览器随后必须同源发送 Cookie，不能把 `session_id` 当作访问凭证。丢失 Cookie、跨会话访问、过期或删除后均失败关闭。SQL 适配器的 SecretStore 凭据提供者只在显式模型调用边界按需取 Key，但尚未接入 CLI；当前启动脚本不会产生模型调用。

## 外部主密钥与实例目录

实例目录必须在仓库外。`metadata.sqlite3`、`secrets.sqlite3` 与 `documents/` 均属于需要备份的实例状态。主密钥必须通过 `ICT_SECRET_MASTER_KEY_B64` 环境变量或 `ICT_MASTER_KEY_FILE` 指向的仓库外文件提供；两者不能同时提供。绝不把主密钥放入 `.env`、镜像、ZIP、数据库或备份。丢失主密钥后原有密文无法恢复。

## Windows

安装 Python 3.12，先在当前进程安全设置上述主密钥来源，再运行：

```powershell
./infra/start.ps1 -InstanceDir 'C:\ict-agent-instance' -Mode offline
```

脚本先找 `py -3.12`，再找 PATH 中的 `python`；若两者不可用，可显式传入 `-PythonExe 'C:\path\to\python.exe'`。解释器版本必须是 3.12。

Windows PowerShell 5.1 与 PowerShell 7 均可解析该启动脚本。若依赖已按 `requirements-product.txt` 安装在现有 Python 3.12 环境，可显式传入 `-PythonExe 'C:\path\to\python.exe' -UseExistingEnvironment`；此模式只从本地已安装包核对锁定版本，不联网安装，也不会建立 `.venv-product`。缺包或版本不符即停止。未传此开关时仍创建/使用 `.venv-product` 并安装锁定依赖。执行策略受限的机器可在本次进程中使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\infra\start.ps1 ...`，无需更改系统执行策略。

首次空实例按已批准的 `admin-bootstrap.v2` 契约自动创建固定 `admin`；密码只以 Argon2id 哈希保存在实例库。回环地址的本地演示可正常登录和管理，可在设置中主动改密；改密会撤销旧会话且不会在重启时恢复初始密码。密码不得放入命令、日志或截图。默认仅监听 `127.0.0.1:8000`。`-Mode online` 使用已经合并的 RAG 优先路由和受 D4-02 安全门约束的 SQL 适配器；启动本身不会调用模型，未配置凭据时 SQL 路径失败关闭。

## Docker Compose

把 `ICT_MASTER_KEY_FILE` 设为仓库外主密钥文件的绝对路径。Compose 容器需要监听 `0.0.0.0`，因此 v2 安全 Gate 要求先在一次性本地容器中交互改密；以下命令只提示输入，不把口令作为命令行参数：

```powershell
docker compose run --rm product python -m backend.product.server change-password-local --instance /instance
docker compose up --build
```

端口只映射到宿主机 `127.0.0.1:8000`，实例位于命名卷 `ict_instance`。空实例在首条命令中自动初始化；已初始化实例绝不覆盖密码。Compose 固定离线；健康探针用实际容器监听地址检查 readiness。默认密码仍有效时，非回环监听的 readiness 必须失败；`/health/live` 仅表示进程存在。不要在公网直接暴露该服务。本机尚无 Docker，以上流程尚待 C 在干净机复验。

## 停机备份、恢复、升级与回滚

备份前停止服务，确保 SQLite 与文档树处于一致时点：

```powershell
python infra/product_snapshot.py backup C:\ict-agent-instance C:\ict-backups\snapshot-001
python infra/product_snapshot.py restore C:\ict-backups\snapshot-001 C:\ict-agent-restored
```

恢复只能写入新目录，不覆盖现有实例；快照 v2 对数据库、PDF 与索引文件逐个核验 SHA-256，缺失或变更均拒绝恢复。另行提供原外部主密钥才能读取旧密文。升级前备份并保留旧镜像/代码版本，升级后以 `PYTHONPATH=src python -m backend.product.server check --instance <外部目录>` 检查；失败时停止新服务、用旧代码与新目录中的备份副本回滚。当前尚无 schema 迁移器；跨不兼容版本升级必须等待专门迁移与 C 侧安装复验，不应直接替换生产实例。
