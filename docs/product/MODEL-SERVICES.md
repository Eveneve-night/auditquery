# 可配置模型服务 v1

协议 `openai-chat-completions`，配置版本 `1.0.0`；只支持 `/chat/completions` 的 JSON object 输出、token usage 和精确 model 回传。OpenAI Responses、Anthropic、Gemini 等原生协议尚不支持；不承诺任意标为兼容的服务都支持所有字段。

管理员在设置页填写服务商标识、模型、Base URL、认证模式、Key 和 CNY/百万 token 单价。保存只进行本地验证/加密，不调用模型；连通测试是独立明确点击，最多一次调用并受既有测试限流。普通问答不能覆盖这些字段。部署者也可以通过同一受鉴权接口配置，本轮不增加未鉴权启动参数或浏览器直连。

真实设置页需启动 `--mode online`：`python -m backend.product.server serve --instance <仓库外实例目录> --mode online`，外部主密钥配置同 README。online 装配本身不调用模型；提交问题/明确测试才可能调用。offline 首页为显式 mock，后端连通测试也拒绝发送，不能用 offline 演示证明真实配置成功。

远程服务必须 HTTPS，拒绝 URL 内凭据/query/fragment、危险 scheme、非公网解析地址、重定向和环境代理；每次解析全部地址，验证后直接连接选定 IP 并用原主机验证 TLS。私网部署暂不支持；本地模式只允许 localhost/回环地址，HTTP 和无认证必须显式配置。Key 是 1–4096 个可见 ASCII 字符，不要求 sk 前缀；拒绝空白/控制字符/非 ASCII 以保护 HTTP 头。Key 只经管理员到本服务的请求提交，不存入浏览器存储、状态响应或日志。正式远程部署必须使用受保护的管理传输。

未知单价保存为 null，状态显示 unknown；**在发送前拒绝调用**，不套用千问价格、不伪称免费，也不让未知价格绕过现有费用上限。单价是管理员声明的估算，不是服务商账单；填写 0 表示管理员明确声明该服务免费。调用数/token/成本账本和循环/超时/费用上限不提高，无隐式服务商 fallback。默认 token/响应大小限制保持现有安全上限。

## API 与兼容

- GET `/api/v3/admin/model-settings`：配置及 revision、configured、credential_configured、pricing_status，永不返回 Key。
- PUT 同路径：`{config, api_key, expected_revision}`，CAS 替换整个配置/凭据；切换服务需重新明确提交对应 Key，禁止把旧 Key 自动送新主机。
- DELETE 同路径：`{expected_revision}`，加密 tombstone 禁止旧实例 Key 复活。
- POST `/api/v3/admin/model-settings/test`：`{explicit_confirmation:true}`。

所有端点由后端管理员鉴权；写入及测试需 CSRF。严格字段/schema 在 `configs/schemas/model-services/service.v1.schema.json`；URL/DNS、CAS、密文认证和权限由运行时检查，不冒充 JSON Schema 可以证明。

模型配置与 Key 作为一个加密文档写入 `secrets.sqlite3/model_settings`，AAD 绑定独立用途/版本/revision；与数据源凭据隔离。首次未迁移实例可读取原百炼密文及原 AAD；第一次保存新配置在一个事务中写新密文并撤销旧 Key 槽。失败全部回滚。备份保留加密 DB，不包含外部主密钥。

旧 `/admin/secrets/bailian` 和冻结 v1 schema 仅作兼容入口，不能改变已迁移的活动配置；新管理页使用新端点。百炼仍可选，非百炼不附加专属 enable_thinking 字段。SQL 在线装配每次调用读取当前加密配置；配置切换/撤销无需重启且重启后保持，切换期间不匹配的进行中 SQL 结果失败关闭。RAG 仍为确定性摘录，不调用模型；不把本次配置能力写成已实现 RAG 模型综合答案。

## 限制

作者验证：核心回归210（含新模型配置专项17）、在线SQL离线专项27、前端回归64；专项含实际回环 HTTP 模拟服务、重定向不跟随、DNS私网阻断、原子迁移/撤销/恢复、响应反射Key拒绝且消耗入账、配置 revision 改变令在途旧结果失效。不是对真实云服务商逐一验收，不代表旧全仓测试通过。安全边界仍须独立终审，作者自检不冒充批准。

已知操作边界：配置切换不强行取消已发往旧端点的请求，旧 revision 结果不允许进入 SQL 执行；已有网关熔断冷却不因重新保存配置而自动清除。计费估算基于管理员声明的单价，不是账单上限保证；服务商行为/计量差异仍须用户核对。模型服务错误只显示稳定类别与固定安全文案，不保存其原始错误正文或请求ID。

尚无环境变量 Key 来源、私网非回环 endpoint、原生协议适配、流式输出、模型列表获取或新业务准确率证明。默认离线管理 mock 仅演示配置交互，测试返回 fixture:true/calls:0 并显示“不能证明可达”；须启动产品服务使用真实管理设置，不得把 mock 当真实配置持久化。真实用户配置后的正常产品调用不需要每次人工授权；开发 Agent 使用真实 Key/付费 API 仍需另行授权，本轮为零。
