/* ============================================================
   api.js — R012 真实 API 客户端（F020–F024 契约，Bearer 认证）
   演示上下文（localStorage）：
     api_base       API 基地址（默认 http://127.0.0.1:8000/api/v1）
     api_token      /auth/login 返回的 Bearer token
     api_role       角色（bid_specialist / data_admin / business_head / legal）
     api_name       显示名
     demo_project_id   当前演示项目（农大 ND-2025）
   ============================================================ */

/* 部署自适应（docs/11 部署方案 §3）：页面经 http(s) 从非本机地址提供（nginx 同源部署，
   如 http://<服务器IP>:2000/）时，默认 API 基址取同源 /api/v1（由 nginx 反代到 api 容器）；
   本机开发（file:// / localhost / 127.0.0.1）保持 127.0.0.1:8000 直连不变。 */
const SAME_ORIGIN_DEPLOY =
  /^https?:$/.test(location.protocol) && !/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname);
const API_BASE_DEFAULT = SAME_ORIGIN_DEPLOY ? `${location.origin}/api/v1` : "http://127.0.0.1:8000/api/v1";
const DEMO_PROJECT_ID = "ND-2025";          // 农大演示项目（已推送，R012 旅程固定样本）

const apiBase = () =>
  // 归一化：去掉尾斜杠，避免拼路径产生 /api/v1//intake/... 双斜杠 404
  (localStorage.getItem("api_base") || API_BASE_DEFAULT).replace(/\/+$/, "");

/* 探测候选 api_base（登录态健康检查 404 自愈：api_base 丢了 /api/v1 或拼错时，
   自动按 127.0.0.1:8000 → localhost:8000 顺序试 /api/v1 + /，命中即持久化并返回。
   只负责找对地址，不携带鉴权头（/healthz 匿名可读），避免把健康检查误当“未登录”。
   任一候选可达（2xx/非 404 响应均视为服务存在）即采纳；全部失败返回 null。 */
const API_PROBE_CANDIDATES = () => {
  const base = apiBase();
  const host = (base.replace(/^https?:\/\//, "").split("/")[0] || "127.0.0.1:8000").replace(/\/+$/, "");
  const hosts = [host];
  // 本机回环地址在不同浏览器/代理环境下解析行为可能不同，两个地址都探测。
  if (host === "127.0.0.1:8000") hosts.push("localhost:8000");
  if (host === "localhost:8000") hosts.push("127.0.0.1:8000");
  const roots = hosts.flatMap((h) => [`http://${h}/api/v1`, `http://${h}`]);
  // 同源部署时优先探测同源地址（浏览器残留的本机 api_base 会在此被自愈修正）
  if (SAME_ORIGIN_DEPLOY) roots.unshift(`${location.origin}/api/v1`, location.origin);
  return [...new Set([base, ...roots])];
};

async function apiProbeHealth() {
  const candidates = API_PROBE_CANDIDATES();
  const tried = [];
  for (const cand of candidates) {
    tried.push(cand);
    try {
      const resp = await fetch(cand + "/healthz", { method: "GET" });
      if (resp.ok || resp.status !== 404) {   // 服务存在即算可达（含 401/403 网关拦截）
        // /healthz 位于 runtime 根路径，业务路由始终挂在 /api/v1；
        // 探测根地址时仍将业务基址规范化为 host/api/v1，避免修正后再次 404。
        const businessBase = /\/api\/v1\/?$/.test(cand)
          ? cand.replace(/\/+$/, "")
          : cand.replace(/\/+$/, "") + "/api/v1";
        if (businessBase !== apiBase()) localStorage.setItem("api_base", businessBase);
        return { base: businessBase, status: resp.status, tried };
      }
    } catch (_) { /* 网络错误：继续下一候选 */ }
  }
  return { base: null, status: 0, tried };
}

/* 健康检查（index 页搜索前调用；返回结构化结果供 UI 渲染）。
   - 200：API/DB/worker/源 状态
   - 401/403：后端对匿名请求仍拦截 → 返回 {auth_gate:true}，提示直连 8000
   - 404：api_base 拼错 → 自动探测修正后重试一次；仍 404 返回 {notFound:true} */
async function apiHealth() {
  let h = null;
  try {
    const resp = await fetch(apiBase() + "/intake/announcement/health", { method: "GET" });
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
    if (resp.ok) return { ok: true, data, base: apiBase() };
    if (resp.status === 404) {
      const probe = await apiProbeHealth();
      if (probe.base) {   // 已修正基址 → 用修正后地址重试一次
        const retryResp = await fetch(probe.base + "/intake/announcement/health", { method: "GET" });
        const retryText = await retryResp.text();
        let retryData = null;
        try { retryData = retryText ? JSON.parse(retryText) : null; } catch (_) { retryData = { raw: retryText }; }
        if (retryResp.ok) return { ok: true, data: retryData, base: probe.base, healed: true };
        // /healthz 可达但业务健康端点仍 404 → 后端版本过旧（v1.8 前无此端点）
        if (retryResp.status === 404) {
          // 兼容旧 runtime：/readyz 与 /healthz 同属进程级端点，可作为
          // 搜索前基础诊断，避免健康诊断缺失阻断联网搜索。逐源/worker
          // 详情仅由 F020 v1.8+ 的业务健康端点提供。
          try {
            const runtimeRoot = probe.base.replace(/\/api\/v1\/?$/, "").replace(/\/+$/, "");
            const readyResp = await fetch(runtimeRoot + "/readyz");
            const readyText = await readyResp.text();
            let readyData = null;
            try { readyData = readyText ? JSON.parse(readyText) : null; } catch (_) { readyData = null; }
            if (readyResp.ok || readyData) {
              return { ok: true, data: {
                api: "ok",
                db: readyData && readyData.checks && readyData.checks.database
                  ? readyData.checks.database : { available: null },
                worker: { available: null, reason: "旧版 runtime 未提供 worker 逐源健康诊断" },
                sources: [],
              }, base: probe.base, healed: true, legacyHealth: true };
            }
          } catch (_) { /* 基础诊断不可用时保留旧版错误 */ }
          return { ok: false, status: 404, data: retryData, base: probe.base,
                   healed: true, staleBackend: true };
        }
        return { ok: false, status: retryResp.status, data: retryData, base: probe.base, healed: true };
      }
      return { ok: false, status: 404, data, base: apiBase(), notFound: true, tried: probe.tried };
    }
    return { ok: false, status: resp.status, data, base: apiBase() };
  } catch (err) {
    return { ok: false, status: 0, network: true, error: err, base: apiBase() };
  }
}
const apiToken = () => localStorage.getItem("api_token") || "";
const apiRole = () => localStorage.getItem("api_role") || "";
const apiName = () => localStorage.getItem("api_name") || "";
const demoProjectId = () => localStorage.getItem("demo_project_id") || DEMO_PROJECT_ID;

/* 当前项目统一解析（2026-09-15）：URL 参数 project_id > 上次选择（localStorage，由
   import/requirements/queue/matrix/score 任一页带参进入时写入）> 内置演示项目。
   source 用于页面明示数据来源——内置演示只在用户从未选择过项目时出现，且必须带提示，
   避免把演示项目的匹配结果误认为用户所选项目（2026-09-15 实测问题④）。 */
function apiProjectId() {
  const q = (new URLSearchParams(location.search).get("project_id") || "").trim();
  const stored = (localStorage.getItem("demo_project_id") || "").trim();
  if (q) {
    if (q !== stored) localStorage.setItem("demo_project_id", q);
    return { id: q, source: "url" };
  }
  if (stored) return { id: stored, source: "last" };
  return { id: DEMO_PROJECT_ID, source: "builtin" };
}
/* 项目来源徽标（页面标题旁）：上次选择 / 内置演示（附指引） */
function apiProjectChip(p) {
  if (!p || p.source === "url") return "";
  if (p.source === "last")
    return ` <span class="badge gray" title="导航进入未带项目参数，已自动使用上次「选择深入」的项目">上次选择 ${p.id}</span>`;
  return ` <span class="badge warn" title="尚未选择任何项目，当前展示的是内置演示数据">内置演示 ${p.id}</span> <a class="hint" href="index.html">去搜索结果选择项目 →</a>`;
}

/* 角色展示元数据（与 data.js ROLES 对齐；权限门禁以后端为准，前端仅显示） */
const ROLE_META = {
  bid_specialist: { label: "投标专员" },
  data_admin:     { label: "数据管理员" },
  business_head:  { label: "经营负责人" },
  legal:          { label: "法务" },
};

function apiRoleLabel() {
  const r = apiRole();
  return (ROLE_META[r] || { label: r || "未登录" }).label;
}

/* ---------- 认证 ---------- */
async function apiLogin(username, password) {
  const data = await apiReq("POST", "/auth/login", { json: { username, password } });
  const ident = data.identity || {};   // 后端返回 {token, identity:{login,role,name}}（R024 正式认证契约）
  localStorage.setItem("api_token", data.token);
  localStorage.setItem("api_role", ident.role || "");
  localStorage.setItem("api_name", ident.name || username);
  return data;
}

function apiLogout() {
  localStorage.removeItem("api_token");
  localStorage.removeItem("api_role");
  localStorage.removeItem("api_name");
}

/* ---------- 请求封装（统一错误结构） ---------- */
async function apiReq(method, path, { json, form } = {}) {
  const headers = {};
  if (apiToken()) headers["Authorization"] = "Bearer " + apiToken();
  let body;
  if (form !== undefined) { body = form; }                 // FormData（multipart 上传）
  else if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  }
  const resp = await fetch(apiBase() + path, { method, headers, body });
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
  if (!resp.ok) {
    const msg = (data && data.error && data.error.message) || `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    err.requestId = (data && data.request_id) || "";
    err.data = data;
    if (resp.status === 401 && !path.startsWith("/auth/login")) {
      const ev = new CustomEvent("api:unauthorized", { detail: err });
      document.dispatchEvent(ev);
    }
    throw err;
  }
  return data;
}

const apiGet = (path) => apiReq("GET", path);
const apiPost = (path, json) => apiReq("POST", path, { json });
const apiDelete = (path) => apiReq("DELETE", path);
const apiUpload = (path, form) => apiReq("POST", path, { form });

/* 通用错误展示（页面内嵌 #api-error 时自动写入） */
function apiShowError(err, containerId = "api-error") {
  const box = document.getElementById(containerId);
  if (!box) return;
  if (!err) { box.innerHTML = ""; box.style.display = "none"; return; }  // 清除错误显示（2026-09-03 修复：confirm 成功分支调 apiShowError(null) 曾崩 TypeError → 误导「确认失败」alert）
  const rid = err.requestId ? `<br><span class="hint">请求编号：<code>${err.requestId}</code>（反馈问题时附上）</span>` : "";
  box.innerHTML = `<div class="notice error"><b>${err.message}</b>${rid}</div>`;
  box.style.display = "block";
}

/* ---------- 任务轮询（F020：job 轮询至终态） ---------- */
const TERMINAL_STATUS = new Set(["succeeded", "completed", "manual_review", "failed", "cancelled"]);

async function apiPollJob(getUrl, { intervalMs = 1500, timeoutMs = 6 * 60 * 1000, onTick, label = "任务" } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const data = await apiGet(getUrl);
    const status = data.status || data.state || "";
    if (onTick) onTick(data);
    if (TERMINAL_STATUS.has(status)) {
      if (status === "failed") {
        const e = new Error((data.error_message) || `${label}执行失败`);
        e.status = 500; e.data = data; e.requestId = data.request_id || "";
        throw e;
      }
      return data;
    }
    if (Date.now() > deadline) {
      const e = new Error(`${label}轮询超时`);
      e.status = 504;
      throw e;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

/* ---------- 渲染小工具（与 common.js 互补） ---------- */
function apiBadge(tone, text) {
  return `<span class="badge ${tone}">${text}</span>`;
}

function apiStatusBadge(status) {
  const map = {
    pending: ["info", "排队中"], queued: ["info", "排队中"], running: ["info", "执行中"],
    completed: ["ok", "完成"], succeeded: ["ok", "成功"], manual_review: ["review", "待人工复核"],
    failed: ["error", "失败"], cancelled: ["gray", "已取消"], stale: ["warn", "已过期"],
    parsed: ["ok", "已解析"], confirmed: ["ok", "已确认"], active: ["ok", "生效"],
    inactive: ["gray", "未生效"], expired: ["warn", "已过期"], blocked: ["error", "已阻断"],
  };
  const m = map[status] || ["gray", status || "未知"];
  return apiBadge(m[0], m[1]);
}

/* 金额/日期格式化 */
function apiFmtMoney(n) {
  if (n === null || n === undefined || n === "") return "—";
  if (typeof n === "number") return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return String(n);
}

/* 证据类型（规则 evidence_required / 引擎 evidence kind）→ 中文（页面不出现英文代码；未知类型原样返回） */
const EVIDENCE_KIND_LABEL = {
  qualification_record: "资质证书", safety_license: "安全生产许可证", business_license: "营业执照",
  performance_record: "业绩证明", similar_performance: "类似业绩证明",
  manager_profile: "项目经理资料", personnel_roster: "人员名册", technical_team_member: "技术团队人员",
  safety_officer_cert: "专职安全员 C 证", financial_report: "财务审计报告", financial_audit: "财务审计报告",
  bank_credit: "银行资信证明", credit_check: "信用核查记录", credit_check_report: "信用核查记录",
  bid_bond_receipt: "保证金到账凭证", bid_bond: "保证金到账凭证", bid_document: "投标文件",
  tax_social_proof: "纳税与社保证明", social_security_proof: "社保缴纳证明", honor_certificate: "荣誉/奖项证书",
  equipment_list: "拟投入设备清单", consortium_declaration: "联合体/独立投标声明", response_document: "响应性文件",
  bid_price_input: "人工录入报价", evidence_file: "证据文件",
};
function apiEvidenceKindLabel(kind) { return EVIDENCE_KIND_LABEL[kind] || String(kind || ""); }

/* 证据引用（material_id:vN:pM）→ 可读文本：「资料 MAT-… 第 M 页」 */
function apiEvidenceRefLabel(ref) {
  const m = /^(.+?):v(\d+)(?::p(\d+))?$/.exec(String(ref || ""));
  if (!m) return String(ref || "");
  return `资料 ${m[1]}${m[3] ? ` 第 ${m[3]} 页` : ""}（版本 ${m[2]}）`;
}

/* ---------- 准入结果中文化（score / approval 共用，2026-09-16：页面不出现英文枚举与系统编号列表） ---------- */
/* 项目准入状态（ADR-001 §2.2 状态机） */
const ADMISSION_STATE_META = {
  collecting: ["info", "信息收集中"], parsing: ["info", "解析中"], manual_review: ["review", "待人工复核"],
  matching: ["info", "匹配中 / 待人工复核"], blocked_missing_data: ["warn", "缺证阻断"],
  blocked_hard_requirement: ["danger", "硬性不满足"], blocked_waiver_expired: ["warn", "豁免过期阻断"],
  qualified_full_score: ["ok", "内部满分"], not_qualified: ["danger", "资格不满足"],
  pending_bid_approval: ["info", "待审批"], approved_for_bidding: ["ok", "已批准投标"],
  rejected_by_approver: ["gray", "已驳回"], archived: ["gray", "已归档"],
};
/* 四类结论（F008 §4.6） */
const QUALIFICATION_META = { passed: ["ok", "通过"], satisfied: ["ok", "通过"], pending: ["warn", "待定（有缺证或待复核）"], failed: ["danger", "未通过（硬性不满足）"] };
const SCORING_META = { full: ["ok", "满分"], not_full: ["warn", "未满分"] };
const READINESS_META = { ready: ["ok", "就绪"], not_ready: ["warn", "未就绪"] };
function apiMetaLabel(meta, value, fallback) { return (meta[value] || ["gray", fallback])[1]; }
function apiMetaBadge(meta, value, fallback) {
  const m = meta[value] || ["gray", fallback];
  return `<span class="badge ${m[0]}" title="${String(value || "")}">${m[1]}</span>`;
}
function apiAdmissionStateBadge(state) { return apiMetaBadge(ADMISSION_STATE_META, state, "状态待定"); }

/* 处置项（缺证 / 复核 / 硬性失败）的可读摘要：中文缺因或条款原文短摘 + 条款号；系统编号只放悬停提示 */
function apiQueueItemBrief(it, preferReason) {
  const escT = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clause = it.clause_ref || it.clause || "";
  const text = String(it.text || "").replace(/\s+/g, "");
  // 缺因只取主句（括号内的解释、分号后的补充不进摘要），如「投标动作尚未开始（报名…）」→「投标动作尚未开始」
  const reason = String(it.reason || "").replace(/\s+/g, "").split(/[（(；;]/)[0];
  let brief = preferReason && reason ? reason : text;
  if (!brief) brief = reason || "（无摘要）";
  const cut = preferReason ? 24 : 16;
  if (brief.length > cut) brief = brief.slice(0, cut) + "…";
  return `<span title="系统编号 ${escT(it.requirement_id || it.req || "")}">${escT(brief)}${clause ? `（${escT(clause)}）` : ""}</span>`;
}
function apiQueueBriefList(items, preferReason, max = 4) {
  const list = items || [];
  const parts = list.slice(0, max).map((it) => apiQueueItemBrief(it, preferReason));
  return parts.join("、") + (list.length > max ? `，等共 ${list.length} 项` : "");
}

/* 审计动作名 → 中文（审计时间线；未知动作原样显示，不猜） */
const AUDIT_ACTION_LABEL = {
  "auth.login": "登录", "project.create": "创建项目",
  "material.import": "材料入库", "material.duplicate": "材料重复（幂等复用）", "material.hash_mismatch": "文件指纹不一致",
  "material.metadata_update": "材料元数据更新", "material.supplement.linked": "补录材料回链要求", "material.verify": "材料核验",
  "ocr.route": "原文路由（文本/OCR）", "ocr.route.completed": "原文路由完成", "ocr.review": "OCR 结果复核",
  "parse.candidates_stored": "解析候选入库", "parse.candidate.review": "解析候选复核", "parse.confirmed": "确认写入规则集",
  "knowledge.index_triggered": "触发知识索引", "knowledge.index.completed": "知识索引完成",
  "knowledge.search": "知识检索", "knowledge.search_denied": "知识检索被拒（权限）",
  "match.trigger": "触发匹配", "match.auto_trigger": "自动触发首次匹配", "match.recalculate": "重算匹配", "match.completed": "匹配完成",
  "admission.generated": "生成准入结果", "admission.mark_stale": "旧准入结果标记过期",
  "blocked_missing_data": "缺证阻断", "blocked_hard_requirement": "硬性不满足阻断",
  "create_approval": "创建审批", "approval.create_denied": "创建审批被拒", "approve": "审批通过", "reject": "审批驳回",
  "add_waiver": "登记豁免", "expire_waivers": "豁免到期失效",
  "qualification.create": "新增资质", "performance.create": "新增业绩", "personnel.create": "新增人员", "evidence.create": "新增证据",
  "enterprise.ledger.preview": "台账导入预览", "enterprise.ledger.commit": "台账导入入库",
  "announcement.import.completed": "公告导入完成", "announcement.search.delete": "删除搜索任务",
};
function apiAuditActionLabel(action) {
  if (AUDIT_ACTION_LABEL[action]) return AUDIT_ACTION_LABEL[action];
  const m = /^enterprise\.(\w+)\.(import|expire|reimport|verify)$/.exec(action || "");
  if (m) {
    const kind = { qualification: "资质", performance: "业绩", manager: "项目经理", personnel: "人员" }[m[1]] || m[1];
    return kind + { import: "导入", expire: "失效", reimport: "重新导入", verify: "核验" }[m[2]];
  }
  if (/^rbac\.deny\./.test(action || "")) return "越权访问被拒";
  return String(action || "");
}

/* 初始化：页面加载时若已有 token 可静默探测 /auth/me（失败即登出） */
async function apiInitSession() {
  if (!apiToken()) return false;
  try {
    await apiGet("/auth/me");
    return true;
  } catch (err) {
    if (err.status === 401) apiLogout();
    return false;
  }
}
