#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_events 同步器测试（事件自动创建 + content_hash 修复）。

覆盖：
- 采集后同步：新 intake → 自动创建事件（content_hash/dedup_key 非空）
- 幂等：重复同步不重复建事件
- 跨平台归并：同 dedup_key 两 intake → 1 事件 + source_links 追加（来源只增不删）
- 发布层状态保护：sync 不覆盖 delivery_status/exclusion_reason/late_discovered_at
- 缺 publish_time：跳过不推测
- content_hash 空值兜底：dedupe_for_daily 对空哈希事件保守不合并
- 集成：多平台同公告 → 归并后日报只展示 1 条
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import TmpCtx, make_cfg, ev, same_day
from publish import models as M
from publish import dedup as DD
from publish.daily_issue_generator import generate_daily_issue
from publish.sync_events import sync_events


def _intake_fm(tmp: Path, intake_id: str, title: str, publish_date: str,
               source_url: str = "https://example.com/n1",
               source_platform: str = "全国公共资源交易平台（河北）",
               announcement_type: str = "招标公告",
               body: str = "项目名称：测试项目\n招标人：测试招标人有限公司\n资格要求：三级及以上\n",
               intake_date: str = "2026-08-17",
               pipeline_status: str = "intel",
               deadline_bid: str | None = "2026-09-10 09:00") -> dict:
    p = tmp / "intake" / f"{intake_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "intake_id": intake_id, "title": title, "publish_date": publish_date,
        "intake_date": intake_date, "source_url": source_url,
        "source_platform": source_platform, "announcement_type": announcement_type,
        "pipeline_status": pipeline_status,   # C 方案门禁：默认已过 G3（intel）
        "deadline_bid": deadline_bid,          # v0.3.0 可参与性准入：默认截止未过
        "_path": str(p), "_dropped": False,
    }
    # 写一份与 _read_body 兼容的 intake 文档（frontmatter + body）
    import yaml
    p.write_text("---\n" + yaml.safe_dump({k: v for k, v in fm.items() if not k.startswith("_")},
                                          allow_unicode=True) + "---\n\n" + body, encoding="utf-8")
    return fm


class TestSyncEvents(unittest.TestCase):

    def test_create_event_with_hash(self):
        """新 intake 同步后自动创建事件，content_hash/dedup_key 非空。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            intakes = [_intake_fm(tmp, "zb-20261201-001", "某项目招标公告", "2026-08-17")]
            r = sync_events(cfg, intake_files=intakes)
            self.assertEqual(len(r["created"]), 1)
            evs = M.load_all(tmp / "events", M.AnnouncementEvent)
            self.assertEqual(len(evs), 1)
            e = evs[0]
            self.assertTrue(e.content_hash, "content_hash 必须非空")
            self.assertEqual(e.content_hash_source, "raw_intake_extract")
            self.assertTrue(e.dedup_key)
            self.assertIn(e.content_hash, e.dedup_key)

    def test_idempotent(self):
        """重复同步不重复创建事件。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            intakes = [_intake_fm(tmp, "zb-20261201-001", "某项目招标公告", "2026-08-17")]
            sync_events(cfg, intake_files=intakes)
            r2 = sync_events(cfg, intake_files=intakes)
            self.assertEqual(len(r2["created"]), 0)
            self.assertEqual(len(r2["unchanged"]), 1)
            self.assertEqual(len(M.load_all(tmp / "events", M.AnnouncementEvent)), 1)

    def test_cross_platform_merge(self):
        """同 dedup_key 两平台 → 1 事件 + source_links 追加（来源只增不删）。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            body = "项目名称：某跨平台项目\n招标人：同一招标人有限公司\n资格要求：一级及以上\n"
            intakes = [
                _intake_fm(tmp, "zb-20261201-001", "某跨平台项目招标公告", "2026-08-17",
                           source_url="https://ggzy.example.com/1",
                           source_platform="全国公共资源交易平台（河北）", body=body),
                _intake_fm(tmp, "zb-20261201-002", "某跨平台项目招标公告", "2026-08-17",
                           source_url="https://huibiao.example.com/2",
                           source_platform="惠招标", body=body),
            ]
            r = sync_events(cfg, intake_files=intakes)
            self.assertEqual(len(r["created"]), 1, "第一平台创建事件")
            self.assertEqual(len(r["merged"]), 1, "第二平台归并进同一事件")
            evs = M.load_all(tmp / "events", M.AnnouncementEvent)
            self.assertEqual(len(evs), 1, "只保留一个事件")
            self.assertEqual(len(evs[0].source_links), 1, "备用来源已追加")
            self.assertEqual(evs[0].source_links[0]["source_name"], "惠招标")
            self.assertEqual(evs[0].source_links[0]["url"], "https://huibiao.example.com/2")

    def test_preserve_delivery_status(self):
        """sync 不覆盖发布层状态（v0.3.0：无截止 → pending_review）。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            intakes = [_intake_fm(tmp, "zb-20261201-001", "某项目招标公告", "2026-08-16",
                                  deadline_bid=None)]
            sync_events(cfg, intake_files=intakes)
            evs = M.load_all(tmp / "events", M.AnnouncementEvent)
            e = evs[0]
            # 先让日报生成器标记为复核（无截止时间，发布层状态）
            generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=evs)
            self.assertEqual(e.delivery_status, "pending_review")
            self.assertEqual(e.exclusion_reason, "missing_deadline")
            # 再同步 → 状态必须保持
            sync_events(cfg, intake_files=intakes)
            e2 = M.load_all(tmp / "events", M.AnnouncementEvent)[0]
            self.assertEqual(e2.delivery_status, "pending_review", "sync 不得覆盖发布层状态")
            self.assertEqual(e2.exclusion_reason, "missing_deadline")

    def test_skip_missing_publish_time(self):
        """缺 publish_date 的 intake 跳过，不推测。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            intakes = [_intake_fm(tmp, "zb-20261201-001", "某项目招标公告", None)]
            r = sync_events(cfg, intake_files=intakes)
            self.assertEqual(len(r["skipped"]), 1)
            self.assertEqual(len(M.load_all(tmp / "events", M.AnnouncementEvent)), 0)

    def test_no_hash_conservative_no_merge(self):
        """空 content_hash 事件在日报去重中保守不合并。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            e1 = ev(id="E1", title="某项目招标公告", project_name="某项目施工",
                    tenderee="招标人X", publish_time=same_day(), content_hash="",
                    dedup_key="", source_platform="平台A")
            e2 = ev(id="E2", title="某项目招标公告", project_name="某项目施工",
                    tenderee="招标人X", publish_time=same_day(), content_hash="",
                    dedup_key="", source_platform="平台B")
            groups = DD.dedupe_for_daily([e1, e2], cfg["dedup"]["primary_source_priority"])
            self.assertEqual(len(groups), 2, "空哈希事件不得合并（保守）")


class TestSyncIntegration(unittest.TestCase):
    """集成：多平台同公告 → sync 归并 → 日报只展示 1 条。"""

    def test_merge_then_daily_once(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            body = "项目名称：某跨平台项目\n招标人：同一招标人有限公司\n资格要求：一级及以上\n"
            intakes = [
                _intake_fm(tmp, "zb-20261201-001", "某跨平台项目招标公告", "2026-08-17",
                           source_url="https://ggzy.example.com/1", body=body),
                _intake_fm(tmp, "zb-20261201-002", "某跨平台项目招标公告", "2026-08-17",
                           source_url="https://huibiao.example.com/2",
                           source_platform="惠招标", body=body),
            ]
            sync_events(cfg, intake_files=intakes)
            evs = M.load_all(tmp / "events", M.AnnouncementEvent)
            self.assertEqual(len(evs), 1)
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=evs)
            self.assertEqual(issue.item_count, 1, "日报只展示一条")


if __name__ == "__main__":
    unittest.main(verbosity=2)
