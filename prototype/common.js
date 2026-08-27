/* ============================================================
   投标智能体 · 原型公共组件（R011 / F010）
   顶部导航 + 用户旅程步骤条 + 通用渲染工具
   ============================================================ */

const STEPS = [
  { id: "index",     label: "新建任务",   page: "index.html" },
  { id: "import",    label: "导入与解析", page: "import.html" },
  { id: "matrix",    label: "要求矩阵",   page: "matrix.html" },
  { id: "materials", label: "资料选择",   page: "materials.html" },
  { id: "score",     label: "匹配与评分", page: "score.html" },
  { id: "queue",     label: "队列处置",   page: "queue.html" },
  { id: "approval",  label: "审批与审计", page: "approval.html" },
];

const NAV = [
  { label: "首页 · 新建任务", page: "index.html" },
  { label: "导入与解析", page: "import.html" },
  { label: "要求矩阵", page: "matrix.html" },
  { label: "资料选择", page: "materials.html" },
  { label: "匹配与评分", page: "score.html" },
  { label: "阻断/待补/复核", page: "queue.html" },
  { label: "审批/豁免/审计", page: "approval.html" },
  { label: "异常态演示", page: "exceptions.html" },
];

/* ---------- 角色（本地模拟，F010 §9：审批数据仅审批角色可见） ---------- */
function getRole() {
  return localStorage.getItem("proto_role") || "bid_specialist";
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
  const role = getRole();
  const navHtml = NAV.map(n =>
    `<a href="${n.page}" class="${n.page === activePage ? "active" : ""}">${n.label}</a>`
  ).join("");
  topbar.innerHTML = `
    <div class="brand"><span class="emblem">投</span>投标智能体</div>
    <nav>${navHtml}</nav>
    <span class="demo-tag">原型演示 · 脱敏数据</span>
    <div class="role-box">
      <span>角色：</span>
      <select id="role-select" title="切换演示角色（模拟登录）">
        ${Object.entries(ROLES).map(([k, v]) =>
          `<option value="${k}" ${k === role ? "selected" : ""}>${v.label}</option>`).join("")}
      </select>
    </div>`;
  const sel = document.getElementById("role-select");
  sel.addEventListener("change", () => {
    setRole(sel.value);
    window.location.reload();
  });
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
}