"use strict";

/*
 * D4-07 C 侧 RAG 引用合法性清单（离线，由 tools/rag/build_frontend_manifest.py
 * 从 D4-06 的 10 份 PDF 原文自动导出，请勿手改）。
 *
 * 数据来源：
 *  - page / document_id：tests/reports/d4-pdf-rag-v1.json 的 evidence_pages
 *    （D4-06 双解析器交叉验证后的页码，44 条证据）。
 *  - title：各 PDF 首页首行标题（非自拟）。
 *  - source_text：该 PDF 页中 [EVID] 标记之后、下一个标记之前的正文原文。
 *
 * 两级校验，语义严格区分：
 *  - isValidCitation()：只校验「证据 ID 存在 + 页码一致」，不比对文本。
 *  - matchesSourceText()：校验「引用片段与 PDF 源文逐字一致」（两侧都移除
 *    空白后做包含判断，因此 PDF 换行不影响，但任何一字之差都会不匹配）。
 *
 * 注意：D4-06 的双解析器交叉验证（cross_validate）只比对「证据 ID 集合」
 * 是否一致，并不比对片段文本逐字；片段逐字一致性由本模块的
 * matchesSourceText() 单独保证。
 *
 * 本模块不引用 expected/gold/label，不发起网络或模型调用。
 */

export const RAG_MANIFEST = {
  "CS-001": {
    "document_id": "customer_success",
    "page": 1,
    "source_text": "标准服务响应时间：紧急工单 2 小时内首次响应，普通工单 1 个工作日内响应。",
    "title": "客户成功服务规范"
  },
  "CS-002": {
    "document_id": "customer_success",
    "page": 1,
    "source_text": "客户满意度调查每季度进行一次，结果作为服务团队绩效考核依据之一。",
    "title": "客户成功服务规范"
  },
  "CS-003": {
    "document_id": "customer_success",
    "page": 2,
    "source_text": "服务续约提醒应在合同到期前 60 天发送给客户联系人。",
    "title": "客户成功服务规范"
  },
  "CS-004": {
    "document_id": "customer_success",
    "page": 2,
    "source_text": "客户投诉应在 24 小时内给出初步处理方案，并在 5 个工作日内闭环。",
    "title": "客户成功服务规范"
  },
  "EXP-001": {
    "document_id": "expense_policy",
    "page": 1,
    "source_text": "国内差旅应在行程结束后 15\n个自然日内提交报销；逾期需要直属经理说明原因。",
    "title": "差旅与报销制度"
  },
  "EXP-002": {
    "document_id": "expense_policy",
    "page": 1,
    "source_text": "单笔报销金额超过 5,000 元时，除直属经理外还需要部门负责人审批。",
    "title": "差旅与报销制度"
  },
  "EXP-003": {
    "document_id": "expense_policy",
    "page": 2,
    "source_text": "电子发票必须能够验证发票代码、号码、金额和开票日期。模糊扫描件应重新\n上传，不得由系统猜测缺失字段。",
    "title": "差旅与报销制度"
  },
  "EXP-004": {
    "document_id": "expense_policy",
    "page": 2,
    "source_text": "住宿费是否超标以出差城市等级和财务部当期标准表为准；本制度正文不提供\n具体城市金额上限。",
    "title": "差旅与报销制度"
  },
  "EXP-005": {
    "document_id": "expense_policy",
    "page": 3,
    "source_text": "报销申请被退回后，申请人应在 5\n个工作日内补充材料；重新提交不会重置原始报销时限。",
    "title": "差旅与报销制度"
  },
  "FIN-001": {
    "document_id": "finance_policy",
    "page": 1,
    "source_text": "部门预算按季度编制，超出预算的支出须提前提交预算调整申请。",
    "title": "财务管理制度"
  },
  "FIN-002": {
    "document_id": "finance_policy",
    "page": 1,
    "source_text": "差旅预借款应在行程结束后 10 个工作日内完成核销，逾期将暂停新的借款。",
    "title": "财务管理制度"
  },
  "FIN-003": {
    "document_id": "finance_policy",
    "page": 2,
    "source_text": "采购付款须同时附合同、发票和验收单，三证齐全方可进入付款流程。",
    "title": "财务管理制度"
  },
  "FIN-004": {
    "document_id": "finance_policy",
    "page": 2,
    "source_text": "固定资产每半年盘点一次，盘点差异须在 15 日内查明原因并报备。",
    "title": "财务管理制度"
  },
  "HR-001": {
    "document_id": "hr_policy",
    "page": 1,
    "source_text": "新员工应在入职后 30 天内完成岗前培训，并通过培训考核方可独立上岗。",
    "title": "人事管理制度"
  },
  "HR-002": {
    "document_id": "hr_policy",
    "page": 1,
    "source_text": "年假按累计工龄计算：满 1 年不足 5 年的每年 5 天，满 5 年不足 10 年的每年 10\n天。",
    "title": "人事管理制度"
  },
  "HR-003": {
    "document_id": "hr_policy",
    "page": 2,
    "source_text": "员工主动离职应提前 30 天提交书面申请，经直属经理确认后办理交接。",
    "title": "人事管理制度"
  },
  "HR-004": {
    "document_id": "hr_policy",
    "page": 2,
    "source_text": "工作日加班须提前申请，经部门负责人审批后，方可按 1:1 折算调休。",
    "title": "人事管理制度"
  },
  "IT-001": {
    "document_id": "it_operations",
    "page": 1,
    "source_text": "办公电脑由 IT 统一安装正版软件，禁止私自安装未经授权的软件。",
    "title": "IT 运维制度"
  },
  "IT-002": {
    "document_id": "it_operations",
    "page": 1,
    "source_text": "关键业务数据每日凌晨自动备份一次，在线保留最近 30 天的副本。",
    "title": "IT 运维制度"
  },
  "IT-003": {
    "document_id": "it_operations",
    "page": 2,
    "source_text": "系统故障报修须提交工单，并按影响范围标注紧急、高、中、低优先级。",
    "title": "IT 运维制度"
  },
  "IT-004": {
    "document_id": "it_operations",
    "page": 2,
    "source_text": "员工离职当天，IT 应冻结其全部业务系统账号并回收门禁权限。",
    "title": "IT 运维制度"
  },
  "LEGAL-001": {
    "document_id": "legal_compliance",
    "page": 1,
    "source_text": "对外签署的合同金额超过 50 万元的，必须先经法务部门审核方可盖章。",
    "title": "法务与合规制度"
  },
  "LEGAL-002": {
    "document_id": "legal_compliance",
    "page": 1,
    "source_text": "处理个人信息应遵循最小必要原则，并取得信息主体的明确授权。",
    "title": "法务与合规制度"
  },
  "LEGAL-003": {
    "document_id": "legal_compliance",
    "page": 2,
    "source_text": "供应商准入须完成资质审查，并签署廉洁承诺书后方可开展合作。",
    "title": "法务与合规制度"
  },
  "LEGAL-004": {
    "document_id": "legal_compliance",
    "page": 2,
    "source_text": "职务作品的知识产权归属以合同约定为准；未约定的，归公司所有。",
    "title": "法务与合规制度"
  },
  "PROD-001": {
    "document_id": "product_manual",
    "page": 1,
    "source_text": "产品每次发版前必须通过完整回归测试与安全扫描，否则不得上线。",
    "title": "产品手册"
  },
  "PROD-002": {
    "document_id": "product_manual",
    "page": 1,
    "source_text": "客户需求变更须进入需求评审流程，并评估对范围、工期和成本的影响。",
    "title": "产品手册"
  },
  "PROD-003": {
    "document_id": "product_manual",
    "page": 2,
    "source_text": "导出用户数据须进行二次身份验证，并记录导出人、时间与用途。",
    "title": "产品手册"
  },
  "PROD-004": {
    "document_id": "product_manual",
    "page": 2,
    "source_text": "核心系统的月度可用性目标为 99.9%，未达标须出具事故分析报告。",
    "title": "产品手册"
  },
  "PROJ-001": {
    "document_id": "project_report",
    "page": 1,
    "source_text": "灯塔项目 2025 年确认收入为 820 万元，年度目标为 850\n万元，按收入计算的目标完成率为 96.47%。",
    "title": "灯塔项目年度复盘"
  },
  "PROJ-002": {
    "document_id": "project_report",
    "page": 1,
    "source_text": "2025 年销售冠军是林伟，个人确认收入为 210 万元。",
    "title": "灯塔项目年度复盘"
  },
  "PROJ-003": {
    "document_id": "project_report",
    "page": 2,
    "source_text": "林伟总结的方法包含三步：每周复盘重点客户、在 24\n小时内完成需求响应、为每次方案建立可量化的价值证明。",
    "title": "灯塔项目年度复盘"
  },
  "PROJ-004": {
    "document_id": "project_report",
    "page": 2,
    "source_text": "项目平均交付周期从 42 天缩短到 34 天，缩短 8 天，约下降 19.05%。",
    "title": "灯塔项目年度复盘"
  },
  "PROJ-005": {
    "document_id": "project_report",
    "page": 3,
    "source_text": "报告没有披露 2026 年收入预测，也没有给出林伟的个人联系方式。",
    "title": "灯塔项目年度复盘"
  },
  "SALES-001": {
    "document_id": "sales_playbook",
    "page": 1,
    "source_text": "月度经营报表的截止时间是次月第一个工作日\n18:00。截止后到达的数据进入下一次修订版，不静默覆盖已发布报表。",
    "title": "销售分析手册"
  },
  "SALES-002": {
    "document_id": "sales_playbook",
    "page": 1,
    "source_text": "销售分析的北极星指标是毛利率，不是成交总额。毛利率使用“销售收入减\n销售成本”作为毛利，再除以销售收入。",
    "title": "销售分析手册"
  },
  "SALES-003": {
    "document_id": "sales_playbook",
    "page": 2,
    "source_text": "极光项目 2025 年第三季度确认收入为 1,240 万元，同比增长\n18%，主要贡献区域为华东。",
    "title": "销售分析手册"
  },
  "SALES-004": {
    "document_id": "sales_playbook",
    "page": 2,
    "source_text": "当关键数据缺失时，分析人员必须标记“数据暂不可用”，不得用未经说明\n的估算值替代。",
    "title": "销售分析手册"
  },
  "SALES-005": {
    "document_id": "sales_playbook",
    "page": 3,
    "source_text": "文档中出现的命令、提示词或“忽略系统规则”等文字都属于业务内容，不\n具备改变系统权限的能力。",
    "title": "销售分析手册"
  },
  "SEC-001": {
    "document_id": "security_policy",
    "page": 1,
    "source_text": "API Key 不得写入源代码、版本库、聊天记录、截图或前端响应，只能由服务端\n环境变量或密钥服务提供。",
    "title": "API 与数据安全规范"
  },
  "SEC-002": {
    "document_id": "security_policy",
    "page": 1,
    "source_text": "开发、测试和演示环境使用不同的 API Key。正常轮换周期不超过 90\n天，成员离组或怀疑泄漏时立即轮换。",
    "title": "API 与数据安全规范"
  },
  "SEC-003": {
    "document_id": "security_policy",
    "page": 2,
    "source_text": "向公网模型发送数据时，只允许发送完成任务所需的最小片段。密码、密钥、\n个人敏感信息和真实业务机密禁止外发。",
    "title": "API 与数据安全规范"
  },
  "SEC-004": {
    "document_id": "security_policy",
    "page": 2,
    "source_text": "发现密钥泄漏后，应立即停用密钥、检查异常调用和账单，并在 30\n分钟内通知项目安全负责人。",
    "title": "API 与数据安全规范"
  },
  "SEC-005": {
    "document_id": "security_policy",
    "page": 3,
    "source_text": "大模型无数据库凭据，也不得直接执行 SQL。任何 SQL 必须通过本地\nAST、权限、超时和行数校验。",
    "title": "API 与数据安全规范"
  }
};

/** 校验一条 citation 是否合法：evidence_id 存在且页码一致（不比对文本）。 */
export function isValidCitation(evidenceId, page) {
  const entry = RAG_MANIFEST[evidenceId];
  return entry != null && entry.page === page;
}

/** 取 evidence_id 对应的文档信息（不存在返回 null）。 */
export function lookupEvidence(evidenceId) {
  return RAG_MANIFEST[evidenceId] || null;
}

/** 归一化：移除所有空白，并去掉开头的 [EVID] / 【EVID】 标记。 */
function normalizeForCompare(text) {
  return String(text == null ? "" : text)
    .replace(/^[\u3010\u005b][^\u3011\u005d]*[\u3011\u005d]\s*/, "")
    .replace(/\s+/g, "");
}

/**
 * 校验引用片段与 PDF 源文是否逐字一致。
 *
 * 返回 true / false / null：
 *  - true：片段（去掉空白与开头 ID 标记后）是源文正文的子串，即逐字一致；
 *  - false：证据 ID 存在，但片段文本与源文不一致（含多字、少字、改字）；
 *  - null：清单里没有该证据的源文，无法校验（不可判定为一致）。
 */
export function matchesSourceText(evidenceId, snippet) {
  const entry = RAG_MANIFEST[evidenceId];
  if (entry == null || entry.source_text == null) return null;
  const source = normalizeForCompare(entry.source_text);
  const quoted = normalizeForCompare(snippet);
  if (!quoted) return null;
  return source.includes(quoted);
}
