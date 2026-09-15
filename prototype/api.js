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
  const rid = err.requestId ? `<br><span class="hint">request_id：<code>${err.requestId}</code></span>` : "";
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
