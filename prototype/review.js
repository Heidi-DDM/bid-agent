/* review.js — 解析候选复核卡片共享模块（F021 §2.1 v1.5）
   requirements.html（解析结果页）与 queue.html（复核页）共用，避免两页复核逻辑漂移。

   要点（2026-09-14 用户实测五问整改，见 docs/04-修改日志）：
   - 人能用的定位信息做主展示：第 N 页 + 条款号（仅真实提取到）+ 原文逐字摘录 + 「在原文中查看」
     （以 Bearer 拉原文 blob 后附 #page= 打开，token 不进 URL）；
   - 未定位到原文（missing）的候选：不显示猜测条款号，改显示「检索关键词」；动作三选一——
     本文件无此条款 / 人工定位补录（页码 + 原文摘录，后端逐字校验）/ 无法确认；**没有「通过」**；
   - 已定位候选：通过 / 编辑后确认（摘录须仍为原文逐字，修正说明另填）/ 无法确认；
   - 「系统追溯编号」折叠区配白话说明，不作为定位手段。
   依赖：api.js（apiBase/apiToken/apiPost/apiRole/apiName/apiShowError）。 */

const REVIEW = (() => {
  const REQ_TYPE_LABEL = { hard: "硬性", scored: "评分", action: "动作" };
  const REJECT_REASONS = ["扫描不清", "条款冲突", "未识别", "需业务解释"];
  const KIND_LABEL = { rule_candidate: "规则候选（招标要求）", main_card_field: "主卡字段（项目基本信息）", term_field: "条款字段（合同/商务条款）" };

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));

  function confLabel(c) {
    const m = { high: "高置信", medium: "中置信", low: "低置信" };
    return c ? (m[c] || c) : "—";
  }
  function isMissing(item) { return !!item.missing; }
  function needsRedecision(item) { return !!item.needs_redecision; }
  function isDecided(item) {
    return item.review_status && item.review_status !== "pending" && !needsRedecision(item);
  }
  function isField(item) {
    const k = item.audit && item.audit.kind;
    return k === "main_card_field" || k === "term_field";
  }

  function statusBadge(item) {
    if (needsRedecision(item)) return '<span class="badge warn">需重新决策</span>';
    switch (item.review_status) {
      case "confirmed": return '<span class="badge ok">已确认</span>';
      case "approved": return '<span class="badge ok">已通过</span>';
      case "revised": return '<span class="badge ok">已人工定位/修正</span>';
      case "rejected": return '<span class="badge danger">无法确认</span>';
      case "not_applicable": return '<span class="badge gray">本文件无此条款</span>';
      default: return isMissing(item)
        ? '<span class="badge warn">未定位到原文</span>'
        : '<span class="badge review">待复核</span>';
    }
  }

  /* ---------- 原文跳转：Bearer 拉 blob → #page=N（token 不进 URL） ---------- */
  const _blobCache = new Map();   // source_link → objectURL
  async function openSource(item, pageNo) {
    const link = item.source_link;
    if (!link) { alert("本条没有可打开的原文文件"); return; }
    let url = _blobCache.get(link);
    if (!url) {
      const rel = link.replace(/^\/api\/v1/, "");
      const resp = await fetch(apiBase() + rel, {
        headers: apiToken() ? { Authorization: "Bearer " + apiToken() } : {},
      });
      if (!resp.ok) { alert(`打开原文失败：HTTP ${resp.status}（请确认已登录且有权查看该材料）`); return; }
      const blob = await resp.blob();
      url = URL.createObjectURL(blob);
      _blobCache.set(link, url);
    }
    const p = pageNo || item.page_no;
    // Chrome/Edge/Firefox 内置 PDF 阅读器支持 #page= 定位；Safari 会打开文件但可能忽略页码
    window.open(p ? `${url}#page=${p}` : url, "_blank", "noopener");
  }

  /* ---------- 定位行：页码 / 条款 / 原文链接 或 检索关键词 ---------- */
  function locateHtml(item) {
    if (isMissing(item)) {
      const hints = (item.locate_hints || []).map((h) => `<code class="hint-chip">${esc(h)}</code>`).join(" ");
      return `<div class="req-meta">
        <span>原文位置：<b>未定位到</b>（系统未在原文找到这条要求的常见措辞）</span>
        ${hints ? `<span>检索关键词：${hints}</span>` : ""}
        ${item.source_link ? `<span><a href="#" data-open-source>打开原文全文检索</a></span>` : ""}
      </div>`;
    }
    return `<div class="req-meta">
      <span>页码：<span class="page-ref">${item.page_no != null ? "第 " + esc(item.page_no) + " 页" : "—"}</span></span>
      <span>条款：${item.clause_ref ? `<code>${esc(item.clause_ref)}</code>` : "—"}</span>
      <span>置信：${confLabel(item.confidence)}</span>
      ${item.source_link ? `<span><a href="#" data-open-source>在原文中查看${item.page_no != null ? "（跳到第 " + esc(item.page_no) + " 页）" : ""}</a></span>` : ""}
    </div>`;
  }

  /* ---------- 系统追溯编号（白话说明，非定位手段） ---------- */
  function auditHtml(item) {
    const a = item.audit || {};
    const row = (label, val, why) => val
      ? `<span><b>${esc(label)}</b>：<code>${esc(val)}</code><i class="audit-why">${esc(why)}</i></span>` : "";
    return `<details class="audit-fold"><summary>系统追溯编号（审计与去重用，不是原文位置）</summary>
      <div class="audit-code">
        ${row("候选编号", a.requirement_id, "系统内部草案号：材料前缀-类型(H硬性/S评分/A动作)-序号，写入规则集后成为要求 ID")}
        ${row("字段键", a.field_key, "主卡/条款字段的内部键名")}
        ${row("候选类型", a.kind ? (KIND_LABEL[a.kind] || a.kind) : "", "")}
        ${row("材料编号", a.material_id, "本招标文件在系统中的编号")}
        ${row("文件指纹", a.content_hash ? a.content_hash.slice(0, 16) + "…" : "", "上传文件的 SHA-256 前 16 位，用于核对“打开的就是解析用的那份文件”，不会变")}
      </div></details>`;
  }

  /* ---------- 单条候选卡 ---------- */
  function cardHtml(item, reviewable) {
    const rt = item.requirement_type ? (REQ_TYPE_LABEL[item.requirement_type] || item.requirement_type) : "";
    const field = isField(item);
    const valueLine = (!isMissing(item) && item.value !== undefined && item.value !== null && item.value !== "" && item.value !== "__待补__")
      ? `<div class="req-value">${field ? "字段值" : "要求值"}：${esc(typeof item.value === "string" ? item.value : JSON.stringify(item.value))}</div>` : "";
    const quote = item.assertion
      ? `<div class="req-quote">原文摘录：${esc(item.assertion)}</div>` : "";
    const issue = item.issue ? `<div class="issue-line">⚠ ${esc(item.issue)}</div>` : "";
    const decided = !reviewable && isDecided(item) && item.review_status !== "confirmed"
      ? `<div class="hint" style="margin-top:6px">复核记录：${decisionLabel(item.review_status)}${item.review_note ? "——" + esc(item.review_note) : ""}${item.reviewer ? `（${esc(item.reviewer)}）` : ""}</div>` : "";
    const actions = reviewable ? `<div class="review-actions" data-cid="${esc(item.id)}"></div>` : "";
    return `
      <div class="req-card${isMissing(item) ? " is-missing" : ""}" data-id="${esc(item.id)}">
        <div class="req-title">${esc(item.title)} ${rt ? `<span class="badge gray">${esc(rt)}</span>` : ""} ${statusBadge(item)}</div>
        ${valueLine}
        ${locateHtml(item)}
        ${quote}
        ${issue}
        ${decided}
        ${actions}
        ${auditHtml(item)}
      </div>`;
  }
  function decisionLabel(s) {
    return { approved: "已通过", revised: "已人工定位/修正", rejected: "已标记无法确认", not_applicable: "已确认本文件无此条款" }[s] || s;
  }

  /* 卡片渲染后绑定「在原文中查看」 */
  function bindCard(cardEl, item) {
    cardEl.querySelectorAll("[data-open-source]").forEach((a) =>
      a.addEventListener("click", (e) => { e.preventDefault(); openSource(item).catch((err) => alert(err.message)); }));
  }

  /* ---------- 复核动作 ---------- */
  function renderActions(item, { onDone } = {}) {
    const box = document.querySelector(`.review-actions[data-cid="${CSS.escape(item.id)}"]`);
    if (!box) return;
    // rejected（无法确认）= 搁置而非终局：找到原文/辨清扫描件后可改判（后端允许重开）。
    if (isDecided(item) && item.review_status !== "rejected") {
      box.innerHTML = `<span class="hint">${decisionLabel(item.review_status)}${item.review_note ? "：" + esc(item.review_note) : ""}</span>
        ${item.review_request_id ? `<span class="hint" style="display:block;margin-top:4px">写入请求 request_id <code>${esc(item.review_request_id)}</code></span>` : ""}`;
      return;
    }
    const reopenNote = item.review_status === "rejected"
      ? `<div class="hint" style="width:100%">此前标记「无法确认」${item.review_note ? "（" + esc(item.review_note) + "）" : ""}——找到原文后可改判人工定位补录/通过。</div>` : "";
    const missing = isMissing(item);
    const field = isField(item);
    const redo = needsRedecision(item)
      ? `<div class="issue-line" style="width:100%">此条此前被「通过」但并未定位到原文（旧版遗留）——请重新选择下面的处置。</div>` : "";
    const btns = missing
      ? `<button type="button" class="act" data-d="not_applicable">${field ? "本文件无此信息" : "本文件无此条款"}</button>
         <button type="button" class="act" data-d="locate">人工定位补录</button>
         <button type="button" class="act" data-d="rejected">无法确认</button>`
      : `<button type="button" class="act" data-d="approved">通过</button>
         <button type="button" class="act" data-d="revised">编辑后确认</button>
         <button type="button" class="act" data-d="rejected">标记无法确认</button>`;
    box.innerHTML = `${redo}${reopenNote}${btns}
      <span class="reason-chip-wrap" data-reasonwrap style="display:none">
        <span class="hint">原因：</span>
        ${REJECT_REASONS.map((r) => `<button type="button" class="reason-chip" data-reason="${esc(r)}">${esc(r)}</button>`).join("")}
      </span>
      <div class="review-form" data-form style="display:none"></div>`;

    const formBox = box.querySelector("[data-form]");
    const reasonWrap = box.querySelector("[data-reasonwrap]");
    const setOn = (d) => box.querySelectorAll(".act").forEach((b) => b.classList.toggle("on", b.dataset.d === d));

    const bApprove = box.querySelector('[data-d="approved"]');
    if (bApprove) bApprove.addEventListener("click", () => {
      setOn("approved"); formBox.style.display = "none"; reasonWrap.style.display = "none";
      submit(item, { decision: "approved", review_note: null }, box, onDone);
    });

    const bNA = box.querySelector('[data-d="not_applicable"]');
    if (bNA) bNA.addEventListener("click", () => {
      setOn("not_applicable"); reasonWrap.style.display = "none";
      formBox.style.display = "";
      formBox.innerHTML = `
        <div class="hint">${field
          ? "确认这份招标文件<b>没有</b>这项信息（如公告未写建设地点）。请写明你怎么核实的（审计留痕）。"
          : "确认这份招标文件<b>没有</b>这条要求。请写明你怎么核实的（审计留痕，例如：全文检索「审计报告」「财务」，资格审查与评标办法均无此要求）。"}</div>
        <div class="form-row"><input class="inp" data-note placeholder="人工核查说明（必填）" style="flex:1"></div>
        <div class="form-row"><button type="button" class="btn sm" data-submit>确认：${field ? "本文件无此信息" : "本文件无此条款"}</button></div>`;
      formBox.querySelector("[data-submit]").addEventListener("click", () => {
        const note = formBox.querySelector("[data-note]").value.trim();
        if (!note) { alert("请填写人工核查说明（如何确认本文件没有这条要求）"); return; }
        submit(item, { decision: "not_applicable", review_note: note }, box, onDone);
      });
    });

    const bLocate = box.querySelector('[data-d="locate"]');
    if (bLocate && field) bLocate.addEventListener("click", () => {
      setOn("locate"); reasonWrap.style.display = "none";
      formBox.style.display = "";
      formBox.innerHTML = `
        <div class="hint">你在原文里找到了这项信息：填写字段值（按原文抄录，不要归纳）。
          ${item.source_link ? `<a href="#" data-open-source>打开原文</a>` : ""}</div>
        <div class="form-row"><input class="inp" data-value placeholder="字段值（必填，按原文）" style="flex:1"></div>
        <div class="form-row"><input class="inp" data-note placeholder="出处说明（如：第 3 页公告标题）" style="flex:1"></div>
        <div class="form-row"><button type="button" class="btn sm" data-submit>提交人工定位</button></div>`;
      const openA = formBox.querySelector("[data-open-source]");
      if (openA) openA.addEventListener("click", (e) => { e.preventDefault(); openSource(item).catch((err) => alert(err.message)); });
      formBox.querySelector("[data-submit]").addEventListener("click", () => {
        const value = formBox.querySelector("[data-value]").value.trim();
        const note = formBox.querySelector("[data-note]").value.trim();
        if (!value) { alert("请填写字段值（按原文抄录）"); return; }
        submit(item, { decision: "revised", review_note: note || null, revised_payload: { value } }, box, onDone);
      });
    });
    if (bLocate && !field) bLocate.addEventListener("click", () => {
      setOn("locate"); reasonWrap.style.display = "none";
      formBox.style.display = "";
      formBox.innerHTML = `
        <div class="hint">你在原文里找到了这条要求：填<b>页码</b>并<b>从原文复制</b>那句话（系统会逐字核对，改写会被拒绝——这是“禁止编造”红线）。
          ${item.source_link ? `<a href="#" data-open-source>打开原文</a>` : ""}</div>
        <div class="form-row">
          <input class="inp" type="number" min="1" data-page placeholder="页码（必填）" style="width:120px">
          <input class="inp" data-clause placeholder="条款号（选填，如 3.5）" style="width:180px">
        </div>
        <div class="form-row"><textarea class="inp" data-assertion rows="3" placeholder="原文逐字摘录（必填，从原文复制）" style="flex:1"></textarea></div>
        <div class="form-row"><input class="inp" data-note placeholder="备注（选填）" style="flex:1"></div>
        <div class="form-row"><button type="button" class="btn sm" data-submit>提交人工定位</button></div>`;
      const openA = formBox.querySelector("[data-open-source]");
      if (openA) openA.addEventListener("click", (e) => { e.preventDefault(); openSource(item).catch((err) => alert(err.message)); });
      formBox.querySelector("[data-submit]").addEventListener("click", () => {
        const page = parseInt(formBox.querySelector("[data-page]").value, 10);
        const assertion = formBox.querySelector("[data-assertion]").value.trim();
        const clause = formBox.querySelector("[data-clause]").value.trim();
        const note = formBox.querySelector("[data-note]").value.trim();
        if (!page || page < 1) { alert("请填写摘录所在页码"); return; }
        if (!assertion) { alert("请从原文复制这条要求的原句（逐字）"); return; }
        submit(item, {
          decision: "revised", review_note: note || null,
          revised_payload: { assertion, page_no: page, ...(clause ? { clause_ref: clause } : {}) },
        }, box, onDone);
      });
    });

    const bRevise = box.querySelector('[data-d="revised"]');
    if (bRevise) bRevise.addEventListener("click", () => {
      setOn("revised"); reasonWrap.style.display = "none";
      formBox.style.display = "";
      if (field) {
        formBox.innerHTML = `
          <div class="hint">修正字段值（主卡/条款字段不做逐字校验，但修正说明必填留痕）。</div>
          <div class="form-row"><input class="inp" data-value value="${esc(typeof item.value === "string" ? item.value : "")}" placeholder="修正后的值（必填）" style="flex:1"></div>
          <div class="form-row"><input class="inp" data-note placeholder="修正说明 / 依据（必填）" style="flex:1"></div>
          <div class="form-row"><button type="button" class="btn sm" data-submit>提交修正</button></div>`;
        formBox.querySelector("[data-submit]").addEventListener("click", () => {
          const value = formBox.querySelector("[data-value]").value.trim();
          const note = formBox.querySelector("[data-note]").value.trim();
          if (!value) { alert("请填写修正后的值"); return; }
          if (!note) { alert("请填写修正说明（依据），以便审计留痕"); return; }
          submit(item, { decision: "revised", review_note: note, revised_payload: { value } }, box, onDone);
        });
      } else {
        formBox.innerHTML = `
          <div class="hint">修正原文摘录：摘录必须仍是原文<b>逐字</b>（系统核对），修正说明另填。
            ${item.source_link ? `<a href="#" data-open-source>打开原文</a>` : ""}</div>
          <div class="form-row">
            <input class="inp" type="number" min="1" data-page value="${item.page_no != null ? esc(item.page_no) : ""}" placeholder="页码" style="width:120px">
            <input class="inp" data-clause value="${esc(item.clause_ref || "")}" placeholder="条款号（选填）" style="width:220px">
          </div>
          <div class="form-row"><textarea class="inp" data-assertion rows="3" style="flex:1">${esc(item.assertion || "")}</textarea></div>
          <div class="form-row"><input class="inp" data-note placeholder="修正说明 / 依据（必填）" style="flex:1"></div>
          <div class="form-row"><button type="button" class="btn sm" data-submit>提交修正</button></div>`;
        const openA = formBox.querySelector("[data-open-source]");
        if (openA) openA.addEventListener("click", (e) => { e.preventDefault(); openSource(item).catch((err) => alert(err.message)); });
        formBox.querySelector("[data-submit]").addEventListener("click", () => {
          const page = parseInt(formBox.querySelector("[data-page]").value, 10);
          const assertion = formBox.querySelector("[data-assertion]").value.trim();
          const clause = formBox.querySelector("[data-clause]").value.trim();
          const note = formBox.querySelector("[data-note]").value.trim();
          if (!assertion) { alert("原文摘录不能为空"); return; }
          if (!note) { alert("请填写修正说明（依据），以便审计留痕"); return; }
          submit(item, {
            decision: "revised", review_note: note,
            revised_payload: { assertion, ...(page ? { page_no: page } : {}), ...(clause ? { clause_ref: clause } : {}) },
          }, box, onDone);
        });
      }
    });

    box.querySelector('[data-d="rejected"]').addEventListener("click", () => {
      setOn("rejected"); formBox.style.display = "none";
      reasonWrap.style.display = reasonWrap.style.display === "none" ? "" : "none";
    });
    box.querySelectorAll(".reason-chip").forEach((chip) => chip.addEventListener("click", () => {
      box.querySelectorAll(".reason-chip").forEach((c) => c.classList.remove("on"));
      chip.classList.add("on");
      submit(item, { decision: "rejected", review_note: chip.dataset.reason }, box, onDone);
    }));
  }

  async function submit(item, body, box, onDone) {
    const payload = { reviewer: apiName() || apiRole() || "投标专员", ...body };
    const ctrls = box.querySelectorAll("button, input, textarea");
    ctrls.forEach((b) => (b.disabled = true));
    try {
      const rev = await apiPost(`/parse/candidates/${encodeURIComponent(item.id)}/review`, payload);
      item.review_status = body.decision;
      item.review_note = body.review_note || null;
      item.needs_redecision = false;
      item.review_request_id = rev.request_id || "";
      if (body.decision === "revised" && body.revised_payload) {
        if (body.revised_payload.assertion) { item.assertion = body.revised_payload.assertion; item.missing = false; }
        if (body.revised_payload.page_no) item.page_no = body.revised_payload.page_no;
        if (body.revised_payload.clause_ref) item.clause_ref = body.revised_payload.clause_ref;
        if (body.revised_payload.value !== undefined) item.value = body.revised_payload.value;
      }
      renderActions(item, { onDone });
      const title = document.querySelector(`.req-card[data-id="${CSS.escape(item.id)}"] .req-title`);
      if (title) {
        title.querySelectorAll(".badge:not(.gray), .badge.gray").forEach((b) => {
          if (!Object.values(REQ_TYPE_LABEL).includes(b.textContent.trim())) b.remove();
        });
        title.insertAdjacentHTML("beforeend", statusBadge(item));
      }
      apiShowError(null);
      if (onDone) onDone(item, body.decision, rev);
    } catch (err) {
      apiShowError(err);
      ctrls.forEach((b) => (b.disabled = false));
      alert("复核提交失败：" + err.message);
    }
  }

  /* ---------- 进度辅助 ---------- */
  function pendingItems(groups) {
    const out = [];
    (groups || []).forEach((g) => (g.items || []).forEach((it) => {
      if (it.review_status === "pending" || needsRedecision(it)) out.push(it);
    }));
    return out;
  }
  function batchable(items) {
    // 仅高/中置信、已定位、非旧版遗留、非模型定位（低置信）项可批量通过
    return items.filter((it) => !isMissing(it) && !needsRedecision(it) && it.review_status === "pending"
      && (it.confidence === "high" || it.confidence === "medium"));
  }

  /* ---------- as_of 确认框（F021 §2.3） ---------- */
  function asOfPanelHtml(suggestion) {
    if (suggestion && suggestion.as_of) {
      const src = suggestion.source === "main_card:deadline_bid"
        ? `来源：招标文件主卡「投标文件递交截止时间」${suggestion.value ? "＝" + esc(suggestion.value) : ""}${suggestion.page_no ? "（第 " + esc(suggestion.page_no) + " 页，已人工确认）" : ""}`
        : `来源：本项目既有规则集的判定时点（澄清版本沿用）`;
      return `
        <div class="notice info" style="margin-top:8px">
          <b>判定时点 as_of</b>：资格与评分判定要锚定一个明确日期（证书有效期、业绩起算期都按这一天判，不按“今天”，F008 §4.1）。<br>
          <span class="hint">${src}</span>
          <div class="form-row" style="margin-top:6px">
            <input class="inp" type="date" data-asof value="${esc(suggestion.as_of)}" style="width:170px">
            <input class="inp" data-asof-reason placeholder="改写原因（改了日期才必填，审计留痕）" style="flex:1;display:none">
          </div>
        </div>`;
    }
    return `
      <div class="notice warn" style="margin-top:8px">
        <b>判定时点 as_of 缺少来源</b>：系统没有已确认的「投标文件递交截止时间」字段可预填（F008 §4.1 不允许默认当前时间）。
        请先在「项目基本信息」组确认/补录该字段，或在此直接填写判定日期并说明依据。
        <div class="form-row" style="margin-top:6px">
          <input class="inp" type="date" data-asof style="width:170px">
          <input class="inp" data-asof-reason placeholder="判定日期依据（必填）" style="flex:1">
        </div>
      </div>`;
  }
  /* 读取确认框：返回 {as_of, reason} 或 null（校验失败已 alert） */
  function readAsOf(panelEl, suggestion) {
    const v = (panelEl.querySelector("[data-asof]").value || "").trim();
    const reasonEl = panelEl.querySelector("[data-asof-reason]");
    const reason = reasonEl ? reasonEl.value.trim() : "";
    if (!v) { alert("请填写判定时点 as_of（YYYY-MM-DD）"); return null; }
    const changed = !suggestion || !suggestion.as_of || v !== suggestion.as_of;
    if (changed && !reason) { alert(suggestion && suggestion.as_of ? "你改写了系统预填的判定时点，请填写改写原因（审计留痕）" : "请填写判定日期的依据"); return null; }
    return { as_of: v, reason: changed ? reason : "", changed };
  }
  function bindAsOfPanel(panelEl, suggestion) {
    const inp = panelEl.querySelector("[data-asof]");
    const reasonEl = panelEl.querySelector("[data-asof-reason]");
    if (!inp || !reasonEl || !suggestion || !suggestion.as_of) return;
    inp.addEventListener("input", () => { reasonEl.style.display = inp.value !== suggestion.as_of ? "" : "none"; });
  }

  return { esc, confLabel, isMissing, isField, isDecided, needsRedecision, statusBadge, cardHtml, bindCard,
           renderActions, submit, openSource, pendingItems, batchable, asOfPanelHtml, readAsOf, bindAsOfPanel,
           REJECT_REASONS, REQ_TYPE_LABEL };
})();
