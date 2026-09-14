#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Markdown 报告渲染器（2026-08-17：产物改为报告形式存储，不再生成 HTML）。

原则：
- 报告主体直观展示**骨架树（schema §二 六维 + §四 主卡 + 招标文件获取）全部可找到字段**，
  字段按维度分组；缺失字段不堆"待补"，统一汇总到报告末尾「待补充与人工审核提醒」
- 内部字段（G0-G4、L0-L3、confidence_score、parser_version、crawler_task_id、raw_record_id、
  intake_id、fact_gate_status 等）一律不渲染；来源说明使用业务语言
- 报告末尾提醒：① 待补充字段清单（逐条）② 人工审核队列 ③ 迟到公告说明
- 渲染完成后执行完整性检查（防内部字段泄漏防呆）
"""

from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path
from string import Template

from . import models as M
from . import timeutil as tu
from . import urgency as UG
from .config import ROOT, load_config

TEMPLATE_DIR = ROOT / "templates"

# 字段 key → 客户业务文案（仅展示层标签，不改事实字段）
LABELS = {
    "project_name": "项目名称", "tender_no": "招标编号", "tenderee": "招标人",
    "agency": "招标代理机构", "region": "地区", "project_type": "项目类型",
    "industry": "行业专业", "procurement_method": "招标方式",
    "announcement_type": "公告类型", "publish_time": "公告发布时间",
    "deadline_signup": "报名/文件获取截止", "deadline_clarify": "答疑/澄清截止",
    "deadline_bid": "投标文件递交截止", "open_date": "开标时间",
    "deadline_bond": "保证金到账截止", "award_time": "中标公示时间",
    "budget_amount": "预算金额", "ceiling_price": "最高投标限价",
    "bid_bond_amount": "投标保证金", "performance_bond": "履约/质量保证金",
    "currency": "币种与单位", "pricing_method": "计价方式",
    "qualification": "资质要求", "personnel": "人员要求", "performance": "业绩要求",
    "finance": "财务要求", "credit": "信用要求", "quality_standard": "质量标准",
    "safety_license": "安全生产许可证",
    "joint_venture": "联合体要求", "other_qualification": "其他资格",
    "review_method": "审查方式", "review_standard": "评审办法",
    "business_scoring": "商务评分", "technical_scoring": "技术评分",
    "credit_scoring": "资信评分", "disqualification_clauses": "废标条款",
    "clarification_rules": "澄清/补正规则",
    "dark_bid": "暗标要求", "ca_requirement": "CA/电子签章",
    "submission_method": "递交方式", "authorization_requirement": "授权委托书",
    "site_visit": "踏勘/标前会", "other_compliance": "其他合规条款",
    "tender_doc_method": "招标文件获取方式", "tender_doc_window": "招标文件获取时间",
    "tender_doc_link": "招标文件获取入口",
    "source_platform": "信息来源", "source_url": "原文链接",
}

# 骨架树字段分组（schema §二 六维 + 招标文件获取 + 溯源）；报告按此分组直观展示
FIELD_GROUPS = [
    ("项目基本信息", ["project_name", "tender_no", "tenderee", "agency", "region",
                   "project_type", "industry", "procurement_method"]),
    ("时间线", ["publish_time", "deadline_signup", "deadline_clarify", "deadline_bid",
             "open_date", "deadline_bond", "award_time"]),
    ("资格要求", ["qualification", "personnel", "performance", "finance", "credit",
               "safety_license", "joint_venture", "other_qualification", "review_method"]),
    ("评分规则", ["review_standard", "business_scoring", "technical_scoring",
               "credit_scoring", "disqualification_clauses", "clarification_rules"]),
    ("金额与限价", ["budget_amount", "ceiling_price", "bid_bond_amount", "performance_bond",
                 "currency", "pricing_method"]),
    ("合规约束", ["dark_bid", "ca_requirement", "submission_method",
               "authorization_requirement", "site_visit", "other_compliance"]),
    ("招标文件获取", ["tender_doc_method", "tender_doc_window", "tender_doc_link"]),
    ("溯源", ["source_platform", "source_url"]),
]

_DATETIME_FIELDS = {"publish_time", "deadline_signup", "deadline_clarify", "deadline_bid",
                    "open_date", "deadline_bond", "award_time"}
_EMPTY_VALUES = {"", "待补", "待补（公告未载明编号）", "暂无", "未知", "None", "null"}


def _load_template(name: str) -> Template:
    p = TEMPLATE_DIR / name
    if not p.exists():
        raise FileNotFoundError(f"模板缺失: {p}")
    return Template(p.read_text(encoding="utf-8"))


def _fmt_field(key: str, value) -> str:
    """格式化字段值：时间字段转北京时间可读格式，其他原样。"""
    if value in (None, "") or str(value).strip() in _EMPTY_VALUES:
        return ""
    if key in _DATETIME_FIELDS:
        dt = tu.parse_dt(str(value))
        return tu.fmt_dt(dt) if dt else str(value)
    return str(value)


def _field_value(e: M.AnnouncementEvent, key: str) -> str:
    """取值：事件字段优先，detail_fields 兜底，空值归一为空串。"""
    if key == "source_url":
        return e.primary_source_url or ""
    raw = getattr(e, key, None) or (e.detail_fields or {}).get(key)
    return _fmt_field(key, raw)


def _missing_fields(e: M.AnnouncementEvent) -> list[str]:
    """该公告缺失的骨架树字段（用于文末待补充清单）。"""
    missing = []
    for _, keys in FIELD_GROUPS:
        for k in keys:
            if not _field_value(e, k):
                missing.append(k)
    return missing


def _render_event_md(e: M.AnnouncementEvent, seq: int, urgency: str) -> str:
    """单条公告报告：按骨架树分组展示全部可找到字段。"""
    lines = [f"### {seq}. {e.title or e.project_name}"]
    if urgency and urgency != "unknown":
        lines.append(f"**紧迫程度**：{UG.URGENCY_LABELS.get(urgency, '')}")
    lines.append("")
    for group_name, keys in FIELD_GROUPS:
        kv = [(k, _field_value(e, k)) for k in keys if _field_value(e, k)]
        if not kv:
            continue
        lines.append(f"**【{group_name}】**")
        for k, v in kv:
            label = LABELS.get(k, k)
            if k == "source_url":
                lines.append(f"- **{label}**：<{v}>")
            else:
                lines.append(f"- **{label}**：{v}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_customer_context(issue: M.DailyIssue,
                           events: dict[str, M.AnnouncementEvent]) -> dict[str, M.AnnouncementEvent]:
    """统一客户展示上下文（P0-3）：只返回当前日报 issue.items 允许展示的事件。

    所有客户渠道（微信/网页/邮件/Markdown/HTML）必须共用此接口消费公告；
    禁止任何客户渠道直接读取全量事件库（data/announcement_events/*、raw/*、情报库/*）。
    """
    allowed_ids = {it.announcement_event_id for it in issue.items}
    return {eid: events[eid] for eid in allowed_ids if eid in events}


def _assert_customer_input(issue: M.DailyIssue,
                           issue_events: dict[str, M.AnnouncementEvent]) -> None:
    """P1-1 渲染前硬性校验：客户上下文不得包含 issue.items 之外的事件。"""
    allowed_ids = {it.announcement_event_id for it in issue.items}
    extra = set(issue_events) - allowed_ids
    if extra:
        raise RuntimeError(f"客户渲染数据越权：事件 {sorted(extra)} 不在日报 items 中，禁止渲染")


def _assert_customer_output(issue: M.DailyIssue, events_all: dict[str, M.AnnouncementEvent],
                            issue_events: dict[str, M.AnnouncementEvent],
                            text: str, cfg: dict) -> None:
    """P1-1 渲染后硬性校验：
    1) 报告中不得出现 items 之外的事件标题（全量事件库中非当天事件的任何标题）
    2) 报告中不得出现"迟到公告"等后台章节/内部状态
    3) 渲染条目数必须等于 issue.item_count
    """
    allowed_ids = {it.announcement_event_id for it in issue.items}
    # 1. 标题边界：全量事件库标题 − 日报条目事件标题 = 禁止出现在客户报告中的标题集合
    allowed_titles = {issue_events[eid].title for eid in allowed_ids if eid in issue_events}
    forbidden_titles = {e.title for e in events_all.values()
                        if e.title and e.title not in allowed_titles}
    for t in forbidden_titles:
        if t and t in text:
            raise RuntimeError(f"客户报告泄漏非日报条目事件标题: {t}")
    # 2. 章节/状态边界
    for kw in ("迟到公告", "late_discovered", "supplement_section", "pending_review"):
        if kw in text:
            raise RuntimeError(f"客户报告泄漏后台章节/状态: {kw}")
    # 3. 条目数一致
    rendered = len(re.findall(r"^### \d+\. ", text, flags=re.M))
    if rendered != issue.item_count:
        raise RuntimeError(f"报告条目数 {rendered} 与 issue.item_count={issue.item_count} 不一致")


def render_daily_issue_md(issue: M.DailyIssue, events: dict[str, M.AnnouncementEvent],
                          cfg: dict | None = None) -> str:
    """日报 Markdown 报告（骨架树全字段 + 文末待补充/后台统计提醒）。

    P0-1 数据边界：渲染输入严格为 build_customer_context(issue, events)，
    即只允许 issue.items 中的当天公告；历史/迟到/待复核事件一律不得进入客户报告。
    """
    cfg = cfg or load_config()
    tmpl = _load_template("daily_issue_report.md")

    issue_events = build_customer_context(issue, events)     # P0-1 唯一公告输入集合
    _assert_customer_input(issue, issue_events)              # P1-1 渲染前校验

    groups: dict[str, list] = {}
    for it in issue.items:
        groups.setdefault(it.section, []).append(it)
    order = [s for s in cfg["daily_issue"].get("grouping", []) if s in UG.SECTION_TITLES]
    order += [s for s in UG.SECTION_TITLES if s not in order]

    parts = []
    seq = 0
    for sec in order:
        if sec not in groups:
            continue
        parts.append(f"## {UG.SECTION_TITLES[sec]}\n")
        for it in groups[sec]:
            e = issue_events.get(it.announcement_event_id)
            if e is None:
                continue
            seq += 1
            parts.append(_render_event_md(e, seq, it.urgency))

    empty_note = ""
    if not issue.items:
        empty_note = "📭 **当日无新增可参与公告**（未发现截止时间未过的可报名/可下载招标公告）\n"

    # —— 文末提醒：待补充字段（仅当天公告，按骨架树维度分组） ——
    pending_lines = []
    for it in issue.items:
        e = issue_events.get(it.announcement_event_id)
        if e is None:
            continue
        miss = _missing_fields(e)
        if not miss:
            continue
        by_group: dict[str, list[str]] = {}
        for gname, keys in FIELD_GROUPS:
            hit = [LABELS.get(k, k) for k in keys if k in miss]
            if hit:
                by_group[gname] = hit
        detail_lines = "；".join(f"【{g}】{'、'.join(v)}" for g, v in by_group.items())
        pending_lines.append(f"- **{e.title or e.project_name}**（{e.id}）：{detail_lines}（公告原文未载明）")
    pending_fields = "\n".join(pending_lines) if pending_lines else "- 无（本日报公告字段已齐）"

    # —— 文末提醒：后台统计（P0-2 仅统计数字，不含任何事件标题/时间/链接/原因） ——
    # v0.3.0：participable 模式下"迟到/后台" = 截止已过（excluded）；兼容 day 模式统计 late_discovered
    late_count = sum(1 for e in events.values()
                     if e.delivery_status in ("excluded", "late_discovered"))
    pending_count = len(issue.pending_review_items)

    updated = _fmt_field("publish_time", issue.last_updated_at)
    stable_path = f"{cfg['storage'].get('report_daily_dir', '报告/日报')}/{issue.issue_date}.md"
    out = tmpl.substitute(
        title=issue.title,
        issue_date=issue.issue_date,
        status=issue.status,
        item_count=issue.item_count,
        urgent_count=issue.urgent_count,
        version=issue.version,
        generated_at=updated,
        last_updated_at=updated,
        stable_path=stable_path,
        empty_note=empty_note,
        groups="\n".join(parts).rstrip() + "\n" if parts else "",
        pending_fields=pending_fields,
        pending_review_count=pending_count,
        late_count=late_count,
    )
    _assert_customer_output(issue, events, issue_events, out, cfg)   # P1-1 渲染后校验
    _integrity_check(out, cfg)
    return out


def write_daily_report(issue: M.DailyIssue, events: dict[str, M.AnnouncementEvent],
                       cfg: dict | None = None) -> Path:
    """落盘日报 Markdown 报告 → 报告/日报/<date>.md。返回路径。"""
    cfg = cfg or load_config()
    report_dir = ROOT / cfg["storage"].get("report_daily_dir", "报告/日报")
    report_dir.mkdir(parents=True, exist_ok=True)
    p = report_dir / f"{issue.issue_date}.md"
    p.write_text(render_daily_issue_md(issue, events, cfg), encoding="utf-8")
    return p


def render_report_index(issues: list[M.DailyIssue], cfg: dict | None = None, kind: str = "daily") -> str:
    """报告索引（Markdown）。kind: daily | source。"""
    cfg = cfg or load_config()
    if kind == "daily":
        title = "日报报告索引"
        rows = []
        for i in sorted(issues, key=lambda x: x.issue_date, reverse=True):
            empty = "（当日无新增公告）" if i.item_count == 0 else ""
            rows.append(f"- **{i.issue_date}** ｜ 新增 {i.item_count} 条 ｜ 紧急 {i.urgent_count} 条"
                        f" ｜ [打开报告]({i.issue_date}.md){empty}")
        body = "\n".join(rows) if rows else "- 暂无日报"
        return f"# {title}\n\n{body}\n"
    return "# 来源与覆盖周报索引\n\n- 暂无周报\n"


def write_report_indexes(issues: list[M.DailyIssue], cfg: dict | None = None) -> Path:
    """重建日报报告索引 报告/日报/_索引.md。"""
    cfg = cfg or load_config()
    report_dir = ROOT / cfg["storage"].get("report_daily_dir", "报告/日报")
    report_dir.mkdir(parents=True, exist_ok=True)
    p = report_dir / "_索引.md"
    p.write_text(render_report_index(issues, cfg, "daily"), encoding="utf-8")
    return p


def render_weekly_report_md(report: M.WeeklySourceReport, cfg: dict | None = None) -> str:
    """周报 Markdown 报告（§8.3：覆盖概况/信息漏斗/对账/日报链接/血缘明细）。"""
    cfg = cfg or load_config()
    lines = [
        f"# 来源与覆盖周报（{report.period_start[:10]} 至 {report.period_end[:10]}）",
        "",
        "> 时间口径：北京时间（Asia/Shanghai）｜ 本报告仅汇总巡检运行与推送覆盖情况，不含抓取过程、失败堆栈、鉴权信息与内部技术细节",
        "",
        "## 一、巡检覆盖概况",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 计划覆盖平台 | {report.source_count} |",
        f"| 实际成功巡检 | {report.successful_source_count} |",
        f"| 部分成功平台 | {report.partial_source_count} |",
        f"| 失败平台 | {report.failed_source_count} |",
        f"| 总巡检次数 | {report.funnel.get('run_count', 0)} |",
        f"| 巡检成功率 | {report.funnel.get('success_rate', 0)}% |",
        "",
        "### 每平台统计",
        "",
        "| 平台 | 巡检次数 | 成功 | 部分成功 | 失败 | 原始发现 | 有效事件 | 去重 | 迟到 | 最后成功巡检 |",
        "|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|------|",
    ]
    for s in report.source_stats:
        lines.append(f"| {s['source_name']} | {s['runs']} | {s['success']} | {s['partial']} | {s['failed']} "
                     f"| {s['raw_items']} | {s['valid_events']} | {s['duplicates']} | {s['late']} "
                     f"| {_fmt_field('publish_time', s.get('last_success_at'))} |")
    if not report.source_stats:
        lines.append("| （本期无巡检记录） |")
    lines += [
        "",
        "## 二、信息漏斗",
        "",
        "| 阶段 | 数量 |",
        "|------|------|",
        f"| 原始发现数量 | {report.funnel.get('raw_items', 0)} |",
        f"| 有效事件数 | {report.funnel.get('valid_events', 0)} |",
        f"| 去重后事件数 | {report.funnel.get('deduplicated_events', 0)} |",
        f"| **进入日报数量** | **{report.funnel.get('delivered_events', 0)}** |",
        f"| 迟到公告 | {report.funnel.get('late_events', 0)} |",
        f"| 复核队列 | {report.funnel.get('pending_review', 0)} |",
        f"| 其他过滤 | {report.funnel.get('excluded_others', 0)} |",
        "",
        "## 三、数量对账",
        "",
    ]
    r = report.reconciliation
    lines.append(f"- 周报「进入日报数量」：**{r.get('delivered_total', 0)}**")
    lines.append(f"- 该周各日报条目合计：**{r.get('daily_item_sum', 0)}**")
    lines.append(f"- 对账结果：**{'一致 ✅' if r.get('balanced') else '不一致 ⚠️'}**")
    lines.append(f"- 差异原因：{'；'.join(r.get('diff_reasons', []))}")
    lines += [
        "",
        "## 四、每日简报链接",
        "",
    ]
    if report.daily_links:
        for d in report.daily_links:
            lines.append(f"- **{d['date']}**：{d['count']} 条（`{d['url']}`）")
    else:
        lines.append("- 周期内无日报")
    lines += [
        "",
        "## 五、信息血缘明细",
        "",
        "| 公告名称 | 公告类型 | 来源平台 | 原始发布时间 | 系统采集时间 | 跨平台重复 | 所属日报 | 处理状态 |",
        "|------|------|------|------|------|:--:|:--:|------|",
    ]
    for ln in report.lineage:
        lines.append(f"| {ln['title']} | {ln['announcement_type']} | {ln['source_platform']} "
                     f"| {ln['publish_time']} | {ln['discovered_at']} | {ln['cross_platform_dup']} "
                     f"| {ln['issue_date'] or '—'} | {ln['status']} |")
    if not report.lineage:
        lines.append("| （无血缘记录） |")
    lines += [
        "",
        "---",
        f"导出明细：`来源与覆盖周报.xlsx` / `来源与覆盖周报.csv`（与本文档同目录）",
        "",
    ]
    out = "\n".join(lines)
    _integrity_check(out, cfg)
    return out


def share_message(issue: M.DailyIssue, cfg: dict | None = None) -> str:
    """微信/飞书分享摘要：只含日期与新增数量（§5.3，不含巡检信息）。"""
    cfg = cfg or load_config()
    tpl = cfg["delivery"].get("share_message",
                              "某建设招标信息日报｜{date}｜今日新增 {count} 条，紧急 {urgent} 条")
    return tpl.format(date=issue.issue_date, count=issue.item_count,
                      urgent=issue.urgent_count, url=issue.stable_url)


def _integrity_check(text: str, cfg: dict) -> None:
    """防泄漏完整性检查：报告文本中不得出现内部字段名。"""
    forbidden = set(cfg["render"].get("forbidden_fields", []))
    for f in forbidden:
        if f and re.search(rf"\b{f}\b", text):
            raise RuntimeError(f"内部字段泄漏到报告: {f}")
    if "confidence_score" in text or "parser_version" in text or "crawler_task_id" in text:
        raise RuntimeError("内部字段泄漏到报告")


# ---------- B 方案（2026-08-17）：飞书推送卡片渲染（模板驱动，杜绝 Agent 自由发挥） ----------

# 优先级标注（与推送卡片模板顶部一致）；unknown（截止字段缺失）按 P2 常规处理
P_LEVEL_LABELS = {
    "urgent": "⏰ 报名即将截止 · P0 最高优先级",
    "high": "📅 报名窗口有限 · P1 次高优先级",
    "normal": "常规商机 · P2",
    "unknown": "常规商机 · P2",
}

# 推送卡片模板 9 段字段映射（模板文案 → field key）；缺失字段渲染为「待补」
CARD_SECTIONS = [
    ("项目信息", [
        ("项目名称", "project_name"), ("招标编号", "tender_no"),
        ("项目类型", "__project_type__"), ("地区", "region"),
    ]),
    ("招标主体", [("招标人", "tenderee"), ("招标代理机构", "agency")]),
    ("时间线", [
        ("公告发布时间", "publish_time"), ("报名/文件获取", "deadline_signup"),
        ("答疑/澄清截止", "deadline_clarify"), ("投标文件递交截止", "deadline_bid"),
        ("开标时间", "open_date"), ("保证金到账截止", "deadline_bond"),
    ]),
    ("金额", [("预算金额", "budget_amount"), ("最高投标限价", "ceiling_price"),
              ("投标保证金", "bid_bond_amount")]),
    ("资格要求（原文摘录）", [
        ("资质要求", "qualification"), ("人员要求", "personnel"), ("业绩要求", "performance"),
        ("财务要求", "finance"), ("信用要求", "credit"), ("质量标准", "quality_standard"),
        ("联合体", "joint_venture"), ("审查方式", "review_method"),
    ]),
    ("评分规则（原文摘录）", [
        ("评审办法", "review_standard"), ("商务评分", "business_scoring"),
        ("技术评分", "technical_scoring"), ("资信评分", "credit_scoring"),
    ]),
    ("合规约束", [
        ("暗标要求", "dark_bid"), ("CA/电子签章", "ca_requirement"),
        ("递交方式", "submission_method"), ("踏勘/标前会", "site_visit"),
    ]),
    ("招标文件获取", [
        ("获取方式", "tender_doc_method"), ("获取时间", "tender_doc_window"),
        ("入口链接", "tender_doc_link"),
    ]),
    ("溯源", [("来源平台", "source_platform"), ("原文链接", "source_url"),
              ("置信度", "confidence")]),
]

# 链接标注（与推送卡片模板「链接可达性标注」章节一致）
LINK_OK_LABEL = "可点击"
LINK_BLOCKED_LABEL = "⚠️ 当前浏览器可能打不开（平台 TLS 兼容问题，内容存在）"
LINK_UNREACHABLE_LABEL = "❌ 链接不可访问，待人工验证"


def _link_mark(url: str, checks: dict | None) -> str:
    """根据链接复检结果返回标注文案。checks: {url: verdict}（browser_ok/browser_blocked/browser_fail/unreachable）。"""
    if not url:
        return ""
    verdict = (checks or {}).get(url)
    if verdict in ("browser_blocked", "browser_fail"):
        return LINK_BLOCKED_LABEL
    if verdict == "unreachable":
        return LINK_UNREACHABLE_LABEL
    if verdict == "browser_ok":
        return LINK_OK_LABEL
    return LINK_OK_LABEL  # 未复检时保守标可点击（采集时 web_extract 已能取到内容）


def _render_push_card(e: M.AnnouncementEvent, item: M.DailyIssueItem,
                      link_checks: dict | None, cfg: dict, seq: int = 1) -> tuple[str, list[str]]:
    """单条公告 → 一张飞书推送卡片（按 templates/推送卡片模板.md 分组）。

    有值字段才展示（缺失字段不堆正文）；返回 (卡片文本, 缺失字段中文清单)。
    v0.3.1：Google/Facebook 通知风格——项目标序号（1. 2. …）、紧凑排版（去多余空行），
    分组结构与字段内容不变。
    """
    lines = []
    # 序号 + 优先级标签（如「1. 📅 报名窗口有限 · P1 次高优先级」）
    lines.append(f"{seq}. {P_LEVEL_LABELS.get(item.urgency, '常规商机 · P2')}")
    sep = "━━━━━━━━━━━━━━━━━━━━━━━━"
    missing_fields: list[str] = []          # 缺失字段的中文标签（文末汇总用）
    project_type = _field_value(e, "project_type")
    industry = _field_value(e, "industry")
    pt_val = project_type if project_type else ""
    if industry and industry not in _EMPTY_VALUES:
        pt_val = f"{pt_val}（{industry}）" if pt_val else industry

    for group_name, fields in CARD_SECTIONS:
        kv: list[tuple[str, str]] = []
        for label, key in fields:
            if key == "__project_type__":
                if pt_val:
                    kv.append(("项目类型", pt_val))
                else:
                    missing_fields.append("项目类型")
                continue
            if key == "source_url":
                raw = e.primary_source_url or ""
                if not raw:
                    missing_fields.append("原文链接")
                    continue
                mark = _link_mark(raw, link_checks)
                if mark and mark != LINK_OK_LABEL:
                    val = f"{raw}（{mark}；备选：{_fallback_text(raw, e)}）"
                else:
                    val = f"{raw}（{mark}）" if mark else raw
                kv.append(("原文链接", val))
                continue
            if key == "tender_doc_link":
                raw = _field_value(e, "tender_doc_link")
                if not raw:
                    missing_fields.append("招标文件获取入口")
                    continue
                mark = _link_mark(raw, link_checks)
                kv.append((label, f"{raw}（{mark}）" if mark else raw))
                continue
            v = _field_value(e, key)
            if v:
                kv.append((label, v))
            else:
                missing_fields.append(label)
        if not kv:
            continue
        lines.append(sep)
        lines.append(f"【{group_name}】")
        for label, v in kv:
            lines.append(f"{label} | {v}")
    return "\n".join(lines).rstrip() + "\n", missing_fields


def _fallback_text(url: str, e: M.AnnouncementEvent) -> str:
    """备选入口文案：平台首页（命中 FALLBACK_ENTRIES 名单）+ 公告标题关键词。"""
    try:
        from check_links_browser import FALLBACK_ENTRIES  # scripts/ 同级
    except Exception:
        FALLBACK_ENTRIES = {}
    entry = next((v for k, v in FALLBACK_ENTRIES.items() if k in url), None)
    base = entry or f"{url.split('/')[0]}//{url.split('/')[2]}/（平台首页）"
    title = e.project_name or e.title or ""
    return f"{base} 搜索「{title}」" if title else base


def render_push_cards_md(issue: M.DailyIssue, events: dict[str, M.AnnouncementEvent],
                         cfg: dict | None = None, link_checks: dict | None = None) -> str:
    """飞书推送卡片文本（B 方案，2026-08-17）。

    按 templates/推送卡片模板.md 分组渲染每条公告，**有值字段才展示**；
    缺失字段不堆在卡片正文，统一汇总到文末「待补充字段」清单（公告原文未载明，不编造）。
    数据边界与日报一致：唯一输入 build_customer_context(issue, events)（P0-1），
    只允许 issue.items 中的当天公告；链接标注来自 check_links_browser.py 复检结果。
    空日报 → 「当日无新增公告」说明。
    """
    cfg = cfg or load_config()
    issue_events = build_customer_context(issue, events)     # P0-1 唯一公告输入集合
    _assert_customer_input(issue, issue_events)              # P1-1 渲染前校验

    parts = []
    all_missing: list[tuple[str, list[str]]] = []   # (公告标题, 缺失字段清单)
    if not issue.items:
        parts.append("📭 **当日无新增可参与公告**（未发现截止时间未过的可报名/可下载招标公告）\n")
    else:
        parts.append(f"📰 {issue.title}")
        parts.append(f"日报日期：{issue.issue_date} ｜ 可参与公告：{issue.item_count} 条 ｜ 紧急事项：{issue.urgent_count} 条")
        for seq, it in enumerate(issue.items, start=1):
            e = issue_events.get(it.announcement_event_id)
            if e is None:
                continue
            card, missing = _render_push_card(e, it, link_checks, cfg, seq=seq)
            parts.append(card)
            if missing:
                title = e.project_name or e.title or e.id
                all_missing.append((title, missing))

    if all_missing:
        parts.append("")
        parts.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        parts.append("【待补充字段】（公告原文未载明，不编造）")
        for title, missing in all_missing:
            parts.append(f"- {title}：{'、'.join(missing)}")
        parts.append("")

    out = "\n".join(parts).rstrip() + "\n"
    # P1-1 渲染后校验：条目数一致（序号开头）+ 无后台状态泄漏
    rendered = len(re.findall(r"^\d+\. ", out, flags=re.M))
    if issue.items and rendered != issue.item_count:
        raise RuntimeError(f"推送卡片条目数 {rendered} 与 issue.item_count={issue.item_count} 不一致")
    for kw in ("迟到公告", "late_discovered", "supplement_section", "pending_review"):
        if kw in out:
            raise RuntimeError(f"推送卡片泄漏后台章节/状态: {kw}")
    _integrity_check(out, cfg)
    return out
