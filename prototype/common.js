/* ============================================================
   投标智能体 · 原型公共组件（R011 / F010）
   顶部导航 + 用户旅程步骤条 + 通用渲染工具
   ============================================================ */

const STEPS = [
  { id: "index",     label: "搜索与推送", page: "index.html" },
  { id: "import",    label: "选择与解析", page: "import.html" },
  { id: "matrix",    label: "自动匹配",   page: "matrix.html" },
  { id: "score",     label: "风险与缺失", page: "score.html" },
  { id: "queue",     label: "人工补录",   page: "queue.html" },
  { id: "approval",  label: "人工审核",   page: "approval.html" },
];

const NAV = [
  { label: "搜索与推送", page: "index.html" },
  { label: "选择与解析", page: "import.html" },
  { label: "自动匹配", page: "matrix.html" },
  { label: "企业资料库（后台）", page: "materials.html" },
  { label: "风险与缺失", page: "score.html" },
  { label: "人工补录", page: "queue.html" },
  { label: "人工审核", page: "approval.html" },
  { label: "异常态演示", page: "exceptions.html" },
];

/* ---------- 角色（R012 起为真实登录态；本地函数保留兼容静态演示页） ---------- */
function getRole() {
  return localStorage.getItem("proto_role") || apiRole() || "bid_specialist";
}
function setRole(role) { localStorage.setItem("proto_role", role); }
function roleLabel(role) { return (ROLES[role] || ROLES.bid_specialist).label; }
function canApprove(role) { return (ROLES[role] || ROLES.bid_specialist).canApprove; }

/* ---------- 渲染工具 ---------- */
function el(tag, cls, html) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function badge(metaKey, value) {
  const meta = metaKey === "state" ? STATE_META[value] : MATCH_META[value];
  if (!meta) return "";
  return `<span class="badge ${meta.tone}">${meta.label}</span>`;
}

function fmtMoney(n) {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function fmtClause(ref) { return ref ? `<code>${ref}</code>` : ""; }

/* ---------- 顶部导航 ---------- */
function renderTopbar(activePage) {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;
  const navHtml = NAV.map(n =>
    `<a href="${n.page}" class="${n.page === activePage ? "active" : ""}">${n.label}</a>`
  ).join("");
  const authed = !!apiToken();
  let authHtml;
  if (authed) {
    authHtml = `
      <span class="auth-name">${apiRoleLabel()}</span>
      <code class="hint">${apiRole()}</code>
      <button class="btn sm ghost" id="btn-logout" title="退出登录">退出</button>`;
  } else {
    // 本机开发保留账号提示；同源部署（服务器）不暴露任何口令惯例（docs/11 §5）
    const remote = typeof SAME_ORIGIN_DEPLOY !== "undefined" && SAME_ORIGIN_DEPLOY;
    const loginHint = remote
      ? "演示账号与口令由项目负责人线下提供"
      : "演示账号：toubiao / jingying（密码 123456）；data_admin / legal（密码同账号）";
    authHtml = `
      <span class="hint" title="${loginHint}">未登录</span>
      <input id="login-user" class="inp" placeholder="账号" autocomplete="username" style="width:96px">
      <input id="login-pass" class="inp" type="password" placeholder="密码" autocomplete="current-password" style="width:96px">
      <button class="btn sm" id="btn-login">登录</button>`;
  }
  topbar.innerHTML = `
    <div class="brand"><span class="emblem">投</span>投标智能体</div>
    <nav>${navHtml}</nav>
    <span class="demo-tag" id="api-indicator" title="运行时 API">API…</span>
    <div class="role-box">${authHtml}</div>`;
  if (authed) {
    document.getElementById("btn-logout").addEventListener("click", () => {
      apiLogout();
      window.location.reload();
    });
  } else {
    const user = document.getElementById("login-user");
    const pass = document.getElementById("login-pass");
    const doLogin = async () => {
      if (!user.value.trim()) {
        const remoteDeploy = typeof SAME_ORIGIN_DEPLOY !== "undefined" && SAME_ORIGIN_DEPLOY;
        alert(remoteDeploy
          ? "请输入账号（演示账号由项目负责人提供）"
          : "请输入账号（投标专员 toubiao、经营负责人 jingying，密码均为 123456；数据管理员 data_admin、法务 legal 密码同账号）");
        return;
      }
      try {
        await apiLogin(user.value.trim(), pass.value || user.value.trim());
        window.location.reload();
      } catch (err) {
        // Failed to fetch = 浏览器层请求被拦（服务未起 / CORS / 代理），给出可操作指引
        if (!err.status && /Failed to fetch|NetworkError|fetch/i.test(err.message || "")) {
          alert("登录失败：无法连接 API（" + apiBase() + "）\n\n" +
            "常见原因：\n" +
            "① runtime 未启动——在项目终端运行：bash scripts/setup_local_env.sh start\n" +
            "② 页面打开方式不对——请从 http://127.0.0.1:8080 打开（直接双击 HTML（file://）或经编辑器预览端口打开会被浏览器跨域拦截，所有请求报 Failed to fetch）\n" +
            "③ 系统代理拦截 127.0.0.1——代理例外需包含 localhost/127.0.0.1");
          return;
        }
        alert("登录失败：" + err.message);
      }
    };
    document.getElementById("btn-login").addEventListener("click", doLogin);
    pass.addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  }
  // API 连接指示（仅提示，不阻断页面浏览）
  const ind = document.getElementById("api-indicator");
  fetch(apiBase().replace("/api/v1", "") + "/readyz", { method: "GET" })
    .then((r) => r.json())
    .then((j) => { ind.textContent = j.ready ? "API 就绪" : "API 未就绪"; ind.className = "demo-tag"; })
    .catch(() => { ind.textContent = "API 未连接"; ind.className = "demo-tag"; });
}

/* ---------- 用户旅程步骤条 ---------- */
function renderSteps(activeStepId) {
  const wrap = document.querySelector(".steps");
  if (!wrap) return;
  const activeIdx = STEPS.findIndex(s => s.id === activeStepId);
  wrap.innerHTML = STEPS.map((s, i) => {
    const cls = i < activeIdx ? "done" : (i === activeIdx ? "active" : "");
    return `<div class="step ${cls}">
      <span class="dot">${i < activeIdx ? "✓" : i + 1}</span>
      <span class="label"><a href="${s.page}">${s.label}</a></span>
      ${i < STEPS.length - 1 ? '<span class="arrow">›</span>' : ""}
    </div>`;
  }).join("");
}

/* ---------- 证据抽屉（F010 页面 5：字段级证据抽屉） ---------- */
function openDrawer(requirement) {
  const mask = document.getElementById("drawer-mask");
  const drawer = document.getElementById("drawer");
  if (!mask || !drawer) return;
  const r = requirement;
  document.getElementById("drawer-title").textContent = `${r.id} · ${r.category}`;
  const body = document.getElementById("drawer-body");
  body.innerHTML = `
    <div class="notice info" style="margin-top:0"><b>条款引用</b>：${fmtClause(r.clause)}</div>
    <h3 style="margin-top:6px">招标要求原文</h3>
    <div class="quote">${r.assertion}</div>
    <h3>匹配结论</h3>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      ${badge("match", r.match)}
      <span style="font-size:13px;color:var(--ink-2)">${r.reason}</span>
    </div>
    ${r.max_score != null ? `<div style="margin-top:6px;font-size:13px;color:var(--ink-2)">满分值：<b>${r.max_score}</b>${r.score != null ? ` ｜ 可复算得分：<b>${r.score}</b>` : ""}</div>` : ""}
    <h3>证据链（条款 ↔ 企业证据）</h3>
    ${(r.evidence || []).map(e => `
      <div class="evidence-line">
        <span class="badge ${e.kind === "条款" ? "info" : e.kind === "待补材料" ? "warn" : e.kind === "人工复核" ? "review" : "ok"}">${e.kind}</span>
        <div>
          <div><b>${e.name}</b> <span class="badge gray">${e.ref}</span></div>
          <div style="color:var(--ink-2)">${e.detail}</div>
          ${e.conf ? `<div class="hint">${e.conf}</div>` : ""}
        </div>
      </div>`).join("") || '<div class="hint">动作类要求无独立证据，以动作状态为准。</div>'}
    <h3>溯源要求（F003/ADR-001）</h3>
    <div class="hint">每条结论回链招标条款（clause_ref）与企业证据（evidence_ref）；缺失字段一律标注“待补/待核实”，禁止编造。</div>`;
  mask.classList.add("open");
  drawer.classList.add("open");
}

function closeDrawer() {
  document.getElementById("drawer-mask")?.classList.remove("open");
  document.getElementById("drawer")?.classList.remove("open");
}

function bindDrawer() {
  const mask = document.getElementById("drawer-mask");
  if (mask) {
    mask.addEventListener("click", closeDrawer);
    const close = document.getElementById("drawer-close");
    if (close) close.addEventListener("click", closeDrawer);
  }
}

/* ---------- 页面初始化 ---------- */
function initPage(activePage, activeStepId) {
  renderTopbar(activePage);
  renderSteps(activeStepId);
  bindDrawer();
  // R012：401 统一处理——会话失效自动登出并提示（页面刷新后回到登录态）
  document.addEventListener("api:unauthorized", () => {
    const had = !!apiToken();
    apiLogout();
    if (had) {
      const box = document.getElementById("api-error");
      if (box) {
        box.innerHTML = `<div class="notice error"><b>登录已失效或未登录</b>——请使用右上角账号登录后重试（投标专员 toubiao / 经营负责人 jingying，密码 123456）。</div>`;
        box.style.display = "block";
      } else {
        alert("登录已失效，请重新登录");
      }
      renderTopbar(activePage);
    }
  });
  // 会话恢复：已有 token 则静默校验，失效自动登出
  if (apiToken()) {
    apiInitSession().then((ok) => {
      if (!ok && document.getElementById("btn-logout")) renderTopbar(activePage);
    });
  }
}
