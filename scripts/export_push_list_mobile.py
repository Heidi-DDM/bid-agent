#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出「待选池推送列表」为手机可打开的单文件 HTML（2026-09-23 用户需求）。

用途：给客户在手机上看今天的推送列表做选择；选中后由客户在公告原页
（交易平台）用 CA 锁登录下载招标文件。

口径：
  - 列表 = 待选池 pool_status=pending（与原型 pool.html 主列表同源、同排序
    publish_date DESC NULLS LAST）；字段与页面卡片一致：标题/来源/发布日期/
    投标截止/公告要点。
  - 公告解析：对每条候选按「抓取建档」同口径只读抓取详情页
    （robots 预检留痕不阻断 + 策略限频 + html_to_text 净化 +
    build_detail_summary 确定性抽取 + DeepSeek public-only 兜底），
    结果只进本 HTML，不写数据库、不建项目。
  - 缺失字段如实标「待补」，不推断（AGENTS.md 规则 1）。
  - 抓取文本缓存到 /tmp/bid_agent_push_cache/，重跑不重复抓。

用法（仓库根运行）：
  .venv/bin/python scripts/export_push_list_mobile.py [输出.html]
"""
from __future__ import annotations

import html as _html
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from runtime.core.config import database_url
from runtime.db.models import AnnouncementCandidate, SelectionPoolItem

CACHE_DIR = Path("/tmp/bid_agent_push_cache")

# 展示字段（与 pool.html HIGHLIGHT_LABELS 同口径，另加截止时间/建设地点）
FIELDS = (
    ("deadline_bid", "投标截止"),
    ("qualification", "资质要求"),
    ("budget_amount", "预算金额"),
    ("purchaser", "采购人"),
    ("agency", "代理机构"),
    ("duration", "工期"),
    ("open_location", "开标地点"),
    ("joint_venture", "联合体"),
    ("quality", "质量要求"),
    ("region", "建设地点"),
)
# 有效信息判定（2026-09-23 用户规则）：以下关键字段一条都没有 → 不推送
# （典型：采购意向公告、脚本渲染空壳详情页——对客户无决策价值）
KEY_FIELDS = ("deadline_bid", "qualification", "budget_amount",
              "purchaser", "agency", "duration")
# 纯单位/占位垃圾值（如表头「预算金额（万元）」抠出的「万元」）不算有效值
_NULLISH = {"", "null", "none", "/", "无", "待补", "万元", "元", "人民币", "－", "—", "--"}


def _summary_value(summary: dict, key: str) -> str | None:
    item = summary.get(key)
    if not isinstance(item, dict) or item.get("missing"):
        return None
    value = item.get("value")
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULLISH:
        return None
    return text[:160] + ("…" if len(text) > 160 else "")


def fetch_detail_text(candidate: AnnouncementCandidate) -> tuple[str, str | None]:
    """只读抓取公告详情并解析要点。返回 (净化文本, 失败原因)。

    单源限频按策略等待重试（最多 5 轮），不绕过限频。
    """
    cache = CACHE_DIR / f"{candidate.candidate_id}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8"), None

    from runtime.collecting.service import (
        FetchError,
        _default_fetchers,
        _policy_limiter,
        _record_fetch,
        collection_policy,
    )
    from runtime.core.compliance import ComplianceError

    policy = collection_policy()
    default_fetch, _default_robots = _default_fetchers(policy)
    if not policy.network_allowed or default_fetch is None:
        return "", f"当前采集策略 {policy.name} 不允许联网抓取"
    limiter = _policy_limiter(policy)
    page = None
    try:
        for _round in range(5):
            domain = None
            try:
                domain = (_record_fetch(candidate.url, limiter)
                          if policy.enforce_rate_limit else None)
                page = default_fetch(candidate.url)
                if not (page or "").strip():
                    raise FetchError("详情页抓取结果为空")
                if policy.enforce_rate_limit and domain is not None:
                    limiter.check(domain, record=True)
                break
            except ComplianceError as exc:
                wait = exc.retry_after_seconds
                if wait is None or _round == 4:
                    raise
                print(f"    限频等待 {wait}s 后重试…")
                time.sleep(min(wait, 35) + 1)
        assert page is not None
    except Exception as exc:  # noqa: BLE001 — 单条失败不阻断整表导出
        return "", f"{type(exc).__name__}: {exc}"[:120]

    from runtime.collecting.parsers import html_to_text

    text = html_to_text(page)
    if not text:
        return "", "详情页净化后无可见文本"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text, None


def build_summary(candidate: AnnouncementCandidate, text: str) -> dict:
    from runtime.parsing.provenance import build_detail_summary, text_sha256

    try:
        return build_detail_summary(
            candidate.title, text, content_hash=text_sha256(text))
    except Exception as exc:  # noqa: BLE001 — 抽取失败降级为空要点
        print(f"  ! 抽取失败 {candidate.candidate_id}: {exc}")
        return {}


def esc(value: str) -> str:
    return _html.escape(str(value), quote=True)


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "推送日志/某建设-招标推送列表-2026-09-23.html")
    engine = create_engine(database_url())
    S = sessionmaker(bind=engine)
    session = S()
    rows = session.execute(
        select(SelectionPoolItem, AnnouncementCandidate)
        .join(AnnouncementCandidate,
              AnnouncementCandidate.candidate_id == SelectionPoolItem.candidate_id)
        .where(SelectionPoolItem.pool_status == "pending")
        .order_by(AnnouncementCandidate.publish_date.desc().nullslast(),
                  AnnouncementCandidate.title)
    ).all()
    print(f"待选池推送条目：{len(rows)}")

    cards = []
    excluded = []
    today = time.strftime("%Y-%m-%d")
    # 2026-09-24 业主指令（最终版）：咨询/科研/监理/勘察设计/设备采购类**不排除**，
    # 标签统一显示「其他」（标题命中经营词或 AI 分类属这些类型 → 标签=其他，仍推送）。
    STRATEGY_WORDS = ("咨询", "科研", "监理", "勘察设计", "设备采购")
    OTHER_CATEGORIES = {"勘察设计咨询", "监理造价", "设备采购"}
    for i, (item, cand) in enumerate(rows, 1):
        screening = dict(item.screening or {})
        llm = screening.get("llm_assist") or {}
        print(f"[{i}/{len(rows)}] {cand.publish_date} {cand.title[:40]}")
        ai_cat = llm.get("category")
        from runtime.collecting.registry import _normalize_keyword
        title_norm = _normalize_keyword(cand.title or "")
        is_other = (ai_cat in OTHER_CATEGORIES
                    or any(_normalize_keyword(w) in title_norm for w in STRATEGY_WORDS))
        category = "其他" if is_other else (ai_cat or "—")
        text, err = fetch_detail_text(cand)
        summary = build_summary(cand, text) if text else {}

        kv = []
        for key, label in FIELDS:
            value = _summary_value(summary, key)
            kv.append((label, value))
        # 用户规则（2026-09-23）：抓取失败 / 采购意向公告 / 关键字段全缺 → 不推送
        if err:
            excluded.append((cand.publish_date, cand.title, "公告详情页打不开（抓取失败）"))
            print(f"    排除：抓取失败 {err}")
            continue
        # 2026-09-24 用户规则：投标截止日期早于今天的不再推送（截止=当天或更晚保留；
        # 截止待补/无法解析的保留不推断）
        deadline_val = dict(kv).get("投标截止")
        dm = re.search(r"(\d{4})[-年/\.](\d{1,2})[-月/\.](\d{1,2})", deadline_val or "")
        if dm:
            try:
                dl = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
                if dl < date.today():
                    excluded.append((cand.publish_date, cand.title,
                                     f"投标截止已过（{dm.group(0)}）"))
                    print(f"    排除：投标截止已过 {dm.group(0)}")
                    continue
            except ValueError:
                pass
        if "采购意向" in text or "采购意向" in (cand.title or ""):
            excluded.append((cand.publish_date, cand.title,
                             "采购意向公告（尚未正式招标，无投标实质内容）"))
            print("    排除：采购意向公告")
            continue
        if not any(v for _, v in kv[:len(KEY_FIELDS)]):
            excluded.append((cand.publish_date, cand.title,
                             "公告无有效投标信息（采购意向或空壳详情页）"))
            print("    排除：关键字段全缺（无有效信息）")
            continue
        cards.append({
            "title": cand.title,
            "url": cand.url,
            "source": cand.source_name,
            "publish": cand.publish_date.isoformat() if cand.publish_date else "待确认",
            "category": category,
            "kv": kv,
            "fetch_error": err,
        })
    session.close()

    cards_html = []
    for i, c in enumerate(cards, 1):
        rows_html = "".join(
            f'<div class="kv"><span class="k">{esc(k)}</span>'
            + (f'<span class="v">{esc(v)}</span>' if v
               else '<span class="v miss">待补（公告未载明或未解析出）</span>')
            + "</div>"
            for k, v in c["kv"])
        err_html = (f'<div class="err">公告解析未能生成：{esc(c["fetch_error"])}</div>'
                    if c["fetch_error"] else "")
        cards_html.append(f"""
      <div class="card">
        <div class="idx">{i}</div>
        <div class="body">
          <div class="title"><a href="{esc(c["url"])}" target="_blank" rel="noopener">{esc(c["title"])}</a></div>
          <div class="meta">
            <span class="chip">{esc(c["source"])}</span>
            <span class="chip date">发布 {esc(c["publish"])}</span>
            <span class="chip cat">AI 分类：{esc(c["category"])}</span>
          </div>
          <div class="actions">
            <a class="btn primary" href="{esc(c["url"])}" target="_blank" rel="noopener">查看公告原文</a>
            <button class="btn" onclick="var d=this.nextElementSibling;d.style.display=d.style.display==='none'?'block':'none'">公告解析</button>
          </div>
          <div class="detail" style="display:none">{err_html or rows_html}</div>
        </div>
      </div>""")

    page = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>某建设 · 招标推送列表 __DATE__</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f2f4f8; color: #1f2d3d; padding: 12px 12px 40px; }
  header { background: #14335c; color: #fff; border-radius: 12px; padding: 16px 14px; margin-bottom: 12px; }
  header h1 { font-size: 18px; margin-bottom: 6px; }
  header p { font-size: 13px; opacity: .85; line-height: 1.6; }
  .card { background: #fff; border-radius: 12px; padding: 12px 12px 12px 44px; margin-bottom: 10px;
          position: relative; box-shadow: 0 1px 3px rgba(16,35,67,.08); }
  .idx { position: absolute; left: 10px; top: 14px; width: 24px; height: 24px; border-radius: 50%;
         background: #14335c; color: #fff; font-size: 13px; display: flex; align-items: center; justify-content: center; }
  .title { font-size: 15px; font-weight: 600; line-height: 1.5; margin-bottom: 6px; }
  .title a { color: #1f2d3d; text-decoration: none; }
  .meta { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .chip { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #eef2f7; color: #4a5b70; }
  .chip.date { background: #e8f1fb; color: #1c5aa0; }
  .chip.cat { background: #edf7ee; color: #217a3c; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .btn { flex: 1 1 120px; text-align: center; padding: 9px 10px; border-radius: 9px; font-size: 14px;
         border: 1px solid #c6d2e0; background: #fff; color: #14335c; cursor: pointer; }
  .btn.primary { background: #14335c; border-color: #14335c; color: #fff; text-decoration: none; display: block; }
  .detail { margin-top: 10px; border-top: 1px dashed #d8e0ea; padding-top: 8px; }
  .kv { display: flex; gap: 8px; padding: 5px 0; font-size: 13px; line-height: 1.55; }
  .kv .k { flex: 0 0 64px; color: #7a8aa0; }
  .kv .v { flex: 1; font-weight: 600; word-break: break-all; }
  .kv .v.miss { font-weight: 400; color: #a3adbb; }
  .err { font-size: 13px; color: #b03a2e; padding: 4px 0; }
  footer { font-size: 12px; color: #7a8aa0; line-height: 1.8; margin-top: 16px; }
</style>
</head>
<body>
<header>
  <h1>某建设 · 招标信息推送列表</h1>
  <p>生成日期：__DATE__ ｜ 共 __COUNT__ 条有效推送（已自动滤除 __FILTERED__ 条无有效信息公告）<br>
  每条均可：① 点「查看公告原文」进入交易平台公告页（用 CA 锁登录即可下载招标文件）；
  ② 点「公告解析」查看系统解析出的要点（资质、金额、截止时间等）。</p>
</header>
__CARDS__
<footer>
  · 推送来源：河北省公共资源交易服务平台、各市公共资源交易中心、惠招标（河北交投）、河北省政府采购网等公开平台。<br>
  · 「待补」= 公告原文未载明或本次未解析出，未做任何推断；以公告原文为准。<br>
  · 已滤除公告：采购意向类（无投标实质内容）、详情页打不开或为空壳页的公告，共 __FILTERED__ 条，不占用您的时间。<br>
  · 本列表只呈现公告事实，不含任何投标建议；是否投标由贵司决策。
</footer>
</body>
</html>
"""
    page = (page.replace("__DATE__", today)
                .replace("__COUNT__", str(len(cards)))
                .replace("__FILTERED__", str(len(excluded)))
                .replace("__CARDS__", "".join(cards_html)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    ok = sum(1 for c in cards if not c["fetch_error"])
    print(f"已生成：{out_path.resolve()}（有效推送 {len(cards)} 条，公告解析可用 {ok} 条，滤除 {len(excluded)} 条）")
    for d, t, why in excluded:
        print(f"  [滤除] {d} {t[:38]} —— {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
