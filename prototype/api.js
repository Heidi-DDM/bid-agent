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

const apiBase = () => localStorage.getItem("api_base") || API_BASE_DEFAULT;
const apiToken = () => localStorage.getItem("api_token") || "";
const apiRole = () => localStorage.getItem("api_role") || "";
const apiName = () => localStorage.getItem("api_name") || "";
const demoProjectId = () => localStorage.getItem("demo_project_id") || DEMO_PROJECT_ID;

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
