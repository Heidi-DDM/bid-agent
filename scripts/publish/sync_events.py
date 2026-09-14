#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AnnouncementEvent 增量同步器（采集流水线接入点，幂等）。

用途（对应 schema §6.4 G3 auto_action 扩展）：
  采集流水线在 G3 情报库门禁通过后调用本脚本，为每个已入库 intake 创建/更新公告事件，
  使发布层（日报编排）能消费最新公告。也用于历史数据补录。

行为：
- 幂等：事件已存在 → 仅同步上游事实字段（不覆盖发布层状态 delivery_status/exclusion_reason
  /late_discovered_at/related_event_ids——那些属于发布层生命周期，由日报编排与人工复核管理）
- content_hash：优先 intake.content_snapshot（G1 建议新增的正文归一化快照），否则用 intake
  文档 body（原文摘录）归一化计算；事件记录 content_hash_source 说明哈希输入来源
- 跨平台归并：同一 dedup_key 的事件合并为一条，多平台作为 source_links 追加（§9.1 架构：
  一个事件 = 一个公告事件 + 多个来源），来源只增不删
- 缺 publish_time 的 intake：跳过并报告（不推测发布时间，§10.3）

用法：
  python3 scripts/publish/sync_events.py [--dry-run]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publish import models as M                    # noqa: E402
from publish import timeutil as tu                 # noqa: E402
from publish import dedup as DD                    # noqa: E402
from publish.config import ROOT, load_config, storage_paths  # noqa: E402

RAW = ROOT / "raw"
INTEL = ROOT / "情报库"
CARDS = RAW / "cards"


def _read_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    return yaml.safe_load(parts[1]) or {} if len(parts) >= 3 else {}


def _read_body(path: Path) -> str:
    """intake 文档正文（frontmatter 之后的部分 = 原文摘录）。"""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2]
    return text


def _load_intake_files() -> list[dict]:
    """汇总 intake + dropped 的 frontmatter（dropped 标记 _dropped）。"""
    out = {}
    for d in (RAW / "intake", RAW / "_dropped"):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            fm = _read_frontmatter(p)
            if not fm.get("intake_id"):
                continue
            fm["_path"] = str(p)
            fm["_dropped"] = d.name == "_dropped"
            out[fm["intake_id"]] = fm
    return list(out.values())


def _main_card(project_id: str) -> dict | None:
    for d in (INTEL, INTEL / "归档"):
        if not d.exists():
            continue
        for p in d.glob("T-CARD-*.md"):
            fm = _read_frontmatter(p)
            if fm.get("project_id") == project_id or fm.get("card_id") == project_id:
                return fm
    return None


def _detail_fields(project_id: str) -> dict:
    card = _main_card(project_id) or {}
    d = {}
    for k in ("tender_no", "tenderee", "agency", "region", "project_type", "industry",
              "procurement_method", "budget_amount", "ceiling_price", "bid_bond_amount",
              "deadline_signup", "deadline_bid", "open_date", "qualification",
              "personnel", "performance", "joint_venture", "review_standard",
              "dark_bid", "submission_method", "tender_doc_method", "tender_doc_window",
              "tender_doc_link", "confidence"):
        v = card.get(k)
        if v and str(v).strip() not in ("待补", "待补（公告未载明编号）", "暂无", "未知", "None", ""):
            d[k] = v
    if "qualification" not in d:
        for p in sorted(CARDS.glob("*.md")):
            fm = _read_frontmatter(p)
            if fm.get("parent_project") == project_id and fm.get("fact_type") == "qualification":
                d["qualification"] = fm.get("assertion", "")
                break
    # 置信度兜底：主卡缺失时按来源级别推断（骨架树 §3.3：官方=confirmed；第三方=high）
    if "confidence" not in d:
        d["confidence"] = "confirmed"
    return d


# ── intake 正文（字段化摘录）→ detail_fields 提炼 ────────────────────────────
# 用途：raw/intake/*.md 正文是" `- 标签：值` "连排的字段化摘录（采集时整理，非逐字公告原文）。
# 发布卡只展示 detail_fields 里有的值（有值才显示），而 intake 正文里的 人员/信誉(信用)/
# 财务/联合体/质量标准/工期/标段 等段落常没被提炼进 detail_fields → 推送卡漏展示。
# 这里做确定性提炼：按「- 标签：值」行按段抓取，detail 缺失时补充；不编造，纯事实红线。
_INTAKE_LABEL_MAP = {
    "资质要求": "qualification", "人员要求": "personnel", "业绩要求": "performance",
    "财务要求": "finance", "信誉要求": "credit", "信用要求": "credit",
    "质量标准": "quality_standard", "质量要求": "quality_standard",
    "联合体": "joint_venture", "安全生产许可证": "safety_license",
    "其他资格": "other_qualification", "审查方式": "review_method",
}
# 项目负责人/设计负责人/项目经理 → 人员要求（intake 常拆多行）
_PERSON_LABELS = ("项目负责人", "设计负责人", "项目经理", "技术负责人")


def _intake_distill(body: str) -> dict:
    """从 intake 正文提炼 detail 字段（确定性：`- 标签：值` 行 + 段直至下一标签/标题）。"""
    out: dict[str, str] = {}
    lines = (body or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r"[-*]?\s*([^\s：:]{1,24})\s*[：:]\s*(.*)", line)
        if not m:
            i += 1
            continue
        label, first = m.group(1).strip(), m.group(2).strip()
        if label in ("##", "#"):
            i += 1
            continue
        key = _INTAKE_LABEL_MAP.get(label)
        if key is None and label in _PERSON_LABELS:
            key = "personnel"
        if key is None:
            i += 1
            continue
        chunks = [first] if first else []
        j = i + 1
        while j < len(lines):
            nxt = lines[j].strip()
            if not nxt:
                break
            if re.match(r"[-*]?\s*[^：:]{2,24}\s*[：:]", nxt) or nxt.startswith("##"):
                break
            chunks.append(nxt)
            j += 1
        val = " ".join(c for c in chunks if c).strip() or None
        if val:
            if key in out and out[key]:
                out[key] = f"{out[key]}；{label}：{val}"
            else:
                out[key] = f"{label}：{val}"
        i = j if j > i else i + 1
    return out


def _apply_intake_distill(d: dict, body: str) -> dict:
    """把 intake 正文提炼的 detail 字段补进当前为空/待补的 key（源值优先，不覆盖）。"""
    distilled = _intake_distill(body)
    for k, v in distilled.items():
        cur = str(d.get(k) or "").strip()
        if cur and cur not in ("", "待补", "暂无", "未知"):
            continue
        d[k] = v
    return d


def _event_id_for(fm: dict, pt) -> str:
    seq = fm["intake_id"].replace("zb-", "").replace("-", "")
    return f"AE-{pt.strftime('%Y%m%d')}-{seq}"


def sync_events(cfg: dict | None = None, dry_run: bool = False,
                intake_files: list[dict] | None = None,
                skip_gate_check: bool = False) -> dict:
    """增量同步公告事件。返回 {created, updated, merged, skipped, unchanged, gate_blocked}。

    G3 门禁硬校验（2026-08-17 C 方案，防 Agent 跳步）：
      每个 intake 必须满足 pipeline_status ∈ {carded, intel} 且存在对应主卡
      （情报库/T-CARD-*.md，project_id 匹配），否则**拒绝同步**（gate_blocked），
      除非 skip_gate_check=True（--force，记录审计，仅限人工确认场景）。
    状态语义（schema §6.4）：
      raw   = 仅采集入库（G0 完成，G1/G2/G3 未过）→ 不可同步
      carded= 事实卡已生成（G2 完成，G3 未过）→ 可同步但字段不全
      intel = 主卡已生成（G3 完成）→ 正常同步
    """
    cfg = cfg or load_config()
    paths = storage_paths(cfg)
    dk_len = cfg["dedup"].get("content_hash_length", 16)
    intakes = intake_files if intake_files is not None else _load_intake_files()
    existing = {e.id: e for e in M.load_all(paths["events_dir"], M.AnnouncementEvent)}
    # 按 dedup_key 索引已有事件（非空 key），用于跨平台归并
    by_dedup: dict[str, M.AnnouncementEvent] = {}
    for e in existing.values():
        if e.dedup_key:
            by_dedup.setdefault(e.dedup_key, e)

    result = {"created": [], "updated": [], "merged": [], "skipped": [],
              "unchanged": [], "gate_blocked": []}
    now_iso = tu.iso(tu.now()) or ""

    for fm in intakes:
        pt = tu.parse_dt(fm.get("publish_date"))
        if pt is None:
            result["skipped"].append(f"{fm['intake_id']}（缺 publish_date，不推测）")
            continue

        project_id = f"T-PROJ-{fm['intake_id'].replace('zb-', '')}"

        # —— G3 门禁硬校验（C 方案，2026-08-17）——
        pstatus = (fm.get("pipeline_status") or "").strip()
        main_card = _main_card(project_id)
        # pipeline_status 是 G2/G3 产出的权威记录：
        #   intel = 已过 G2 事实卡 + G3 主卡（主卡文件通常存在，但以状态为准，防历史路径差异误拦）
        #   carded = 已过 G2 事实卡（G3 未过）→ 需主卡文件佐证
        # 兼容历史 intake：无 pipeline_status 但主卡存在 → 视为已过 G2/G3（intel）
        if pstatus == "intel":
            gate_ok = True
        elif pstatus == "carded" and main_card is not None:
            gate_ok = True
        elif main_card is not None and not pstatus:
            gate_ok = True      # 历史数据：主卡即门禁证据
        else:
            gate_ok = False
        if not gate_ok:
            reason = (f"pipeline_status={pstatus or 'raw'}（缺事实卡/主卡），"
                      f"主卡={main_card is not None}")
            if not skip_gate_check:
                result["gate_blocked"].append(f"{fm['intake_id']}（{reason}）")
                continue
            result["skipped"].append(f"{fm['intake_id']}（--force 绕过门禁：{reason}）")

        detail = _detail_fields(project_id)
        # 合并 intake frontmatter 中可用的业务字段（主卡缺时兜底，如招标人/编号/地区/截止时间）
        for k in ("tender_no", "tenderee", "agency", "region", "project_type", "industry",
                  "procurement_method", "deadline_signup", "deadline_bid", "open_date",
                  "budget_amount", "ceiling_price", "bid_bond_amount", "qualification",
                  "joint_venture", "dark_bid", "submission_method", "priority"):
            v = fm.get(k)
            if v and str(v).strip() not in ("待补", "待补（公告未载明编号）", "暂无", "未知", "None", ""):
                detail.setdefault(k, v)
        body = _read_body(Path(fm["_path"]))
        # —— 从 intake 正文提炼 detail 字段（资质/人员/信用/财务/质量/联合体等），
        #    推送到　detail_fields，供发布卡展示（有值才显示）。不覆盖前端已有值。——
        detail = _apply_intake_distill(detail, body)
        # content_hash：优先正文快照字段，否则 intake 摘录；记录输入来源
        snapshot = fm.get("content_snapshot")
        if snapshot:
            hash_input, hash_src = str(snapshot), "notice_fulltext_snapshot"
        else:
            hash_input, hash_src = body, "raw_intake_extract"
        ch = DD.content_hash(hash_input, dk_len)
        tenderee = detail.get("tenderee", "")
        dk = DD.dedup_key(fm.get("title") or "", fm.get("announcement_type", ""),
                          tenderee, pt, hash_input, dk_len)

        ev_id = _event_id_for(fm, pt)
        ev_path = M.event_path(paths["events_dir"], ev_id)
        ev = existing.get(ev_id)

        # 跨平台归并：同 dedup_key 的已有事件 → 追加 source_link，不新建
        if dk and ev is None and dk in by_dedup:
            target = by_dedup[dk]
            target.source_links.append({
                "source_id": ev_id,
                "source_name": fm.get("source_platform", ""),
                "url": fm.get("source_url", ""),
                "publish_time": tu.iso(pt),
            })
            target.updated_at = now_iso
            if not dry_run:
                M.save(target)
            result["merged"].append(f"{ev_id} → {target.id}")
            continue

        fields = dict(
            project_id=project_id,
            announcement_type=fm.get("announcement_type", "招标公告"),
            title=fm.get("title", ""),
            publish_time=tu.iso(pt),
            primary_source_id=fm.get("source_url", ""),
            primary_source_url=fm.get("source_url", ""),
            content_hash=ch,
            dedup_key=dk,
            content_hash_source=hash_src,
            discovered_at=tu.iso(tu.parse_dt(fm.get("intake_date"))) or now_iso,
            fact_gate_status="passed",
            project_name=fm.get("title", ""),
            tenderee=tenderee,
            region=detail.get("region", ""),
            deadline_signup=detail.get("deadline_signup"),
            deadline_bid=detail.get("deadline_bid"),
            budget_amount=detail.get("budget_amount"),
            ceiling_price=detail.get("ceiling_price"),
            bid_bond_amount=detail.get("bid_bond_amount"),
            tender_doc_method=detail.get("tender_doc_method"),
            tender_doc_window=detail.get("tender_doc_window"),
            tender_doc_link=detail.get("tender_doc_link"),
            detail_fields=detail,
            source_platform=fm.get("source_platform", ""),
            confidence=detail.get("confidence", ""),
        )

        if ev is not None:
            # 事件已存在：只同步上游事实字段，不覆盖发布层状态
            changed = []
            for k, v in fields.items():
                if getattr(ev, k) != v:
                    setattr(ev, k, v)
                    changed.append(k)
            if changed:
                ev.updated_at = now_iso
                if not dry_run:
                    M.save(ev)
                result["updated"].append(f"{ev_id}（{','.join(changed)}）")
            else:
                result["unchanged"].append(ev_id)
            continue

        ev = M.AnnouncementEvent(
            id=ev_id, **fields,
            delivery_status="eligible", created_at=now_iso, updated_at=now_iso,
        )
        ev._path = str(ev_path)
        if not dry_run:
            M.save(ev)
        result["created"].append(ev_id)
        if dk:
            by_dedup.setdefault(dk, ev)

    return result


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="同步 AnnouncementEvent（G3 后调用，幂等；门禁硬校验）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写入")
    ap.add_argument("--force", action="store_true",
                    help="跳过 G3 门禁硬校验（缺事实卡/主卡的 intake 也同步；仅人工确认场景，结果记入审计）")
    args = ap.parse_args(argv)
    cfg = load_config()
    r = sync_events(cfg, dry_run=args.dry_run, skip_gate_check=args.force)
    print(f"[事件同步] 新增 {len(r['created'])} ｜ 更新 {len(r['updated'])} ｜ "
          f"归并 {len(r['merged'])} ｜ 跳过 {len(r['skipped'])} ｜ 无变化 {len(r['unchanged'])}"
          f"｜ 门禁拦截 {len(r['gate_blocked'])}")
    for x in r["created"]:
        print(f"  + {x}")
    for x in r["updated"]:
        print(f"  ~ {x}")
    for x in r["merged"]:
        print(f"  ⤷ {x}")
    for x in r["skipped"]:
        print(f"  - {x}")
    for x in r["gate_blocked"]:
        print(f"  🚫 {x}")
    if r["gate_blocked"]:
        print("⚠️ 有 intake 未过 G3 门禁（缺事实卡/主卡）。请先执行 G1 清洗 → G2 事实卡 → G3 主卡，"
              "再重跑本命令；如确需强制同步用 --force（记录审计）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
