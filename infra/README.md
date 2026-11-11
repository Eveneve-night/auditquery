# 部署与实例管理

## 本机与持久化

先按 [README](../README.md) 完成离线试用。日常使用时，将实例目录和主密钥放在仓库外，并为密钥文件设置仅服务账号可读的权限。首次生成后妥善保存，重启必须使用同一密钥。

主密钥使用 32 字节随机值的 Base64 表示，可通过 `ICT_MASTER_KEY_FILE` 指定文件，或通过 `ICT_SECRET_MASTER_KEY_B64` 注入环境变量，两者不能同时设置。不要把密钥放进镜像、仓库、实例备份或日志。

```powershell
$env:ICT_MASTER_KEY_FILE = 'C:\auditquery-secrets\master-key.txt'
.\infra\start.ps1 -InstanceDir 'C:\auditquery-instance' -Mode offline
```

脚本需要 Python 3.12；使用已安装锁定依赖的环境时可指定 `-PythonExe` 和 `-UseExistingEnvironment`。默认监听 `127.0.0.1:8000`。`/health/live` 表示进程存活，`/health/ready` 表示实例可服务。

## 在线与容器

使用 `-Mode online` 启动后，按[模型服务设置](../docs/product/MODEL-SERVICES.md)配置模型。启动本身不会发模型请求；连通测试和在线问答可能产生费用。管理端应通过受保护的传输访问，不要直接暴露到公网。

Compose 使用命名卷，宿主端口仅绑定回环地址。设置外部主密钥文件后，先初始化或修改管理员密码，再启动离线容器：

```powershell
docker compose run --rm product python -m backend.product.server change-password-local --instance /instance
docker compose up --build
```

不要把管理员密码作为命令行参数。容器内非回环监听时，默认密码未更改会导致就绪检查失败。

## 备份与恢复

停止服务后操作，保证数据库和文档一致：

```powershell
python infra/product_snapshot.py backup C:\auditquery-instance C:\auditquery-backups\snapshot-001
python infra/product_snapshot.py restore C:\auditquery-backups\snapshot-001 C:\auditquery-restored
```

恢复只能写入新目录，工具会校验快照完整性。使用原主密钥启动恢复实例。升级前保留可恢复的快照和可运行版本；确认就绪、权限及查询行为后再切换。
