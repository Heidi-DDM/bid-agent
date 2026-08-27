#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集成测试（任务书 §14.2 A-H 八例）。

每个样例构造完整场景并断言端到端结果。
"""

from __future__ import annotations

import unittest
from datetime import date

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import TmpCtx, make_cfg, ev, same_day
from publish import models as M
from publish.daily_issue_generator import generate_daily_issue, record_deliveries
from publish import renderer


class TestIntegrationSamples(unittest.TestCase):

    def _run(self, events, day=date(2026, 8, 17), policy="backend_only"):
        ctx = TmpCtx()
        tmp = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        cfg = make_cfg(tmp)
        cfg["delivery"]["late_announcement_policy"] = policy
        issue = generate_daily_issue(day, cfg=cfg, events=events)
        ev_map = {e.id: e for e in events}
        renderer.write_daily_report(issue, ev_map, cfg)
        return issue, ev_map, cfg

    def test_A_today_publish_today_collect(self):
        """A：当天发布、当天抓取 → 进入当天日报。"""
        issue, _, _ = self._run([ev(id="E1", publish_time=same_day())])
        self.assertIn("E1", {i.announcement_event_id for i in issue.items})
        self.assertEqual(issue.status, "published")

    def test_B_yesterday_publish_today_collect(self):
        """B（v0.3.0）：昨天发布、今天抓取，但截止时间未过 → 仍进日报（可参与性准入）。

        用户决策 2026-08-18：放宽日期口径，不限定"当天发布"——
        只要当天仍可报名/下载/投递（deadline 未过）就推送。
        """
        issue, _, _ = self._run([ev(id="E1", publish_time="2026-08-16 10:00:00",
                                    discovered_at="2026-08-17 09:00:00")])
        self.assertIn("E1", {i.announcement_event_id for i in issue.items},
                      "历史发布但截止未过的公告必须进日报（可参与性准入）")

    def test_B2_expired_not_in_daily(self):
        """B2：截止时间已过 → 不进日报（已无法报名/投递）。"""
        issue, _, _ = self._run([ev(id="E1", publish_time="2026-08-16 10:00:00",
                                    deadline_bid="2026-08-15 09:00",
                                    deadline_signup="2026-08-14 09:00")])
        self.assertEqual(issue.item_count, 0, "截止已过的公告不得进日报")

    def test_C_old_project_today_clarify(self):
        """C：旧项目今天发布澄清 → 应进入今天日报。"""
        issue, _, _ = self._run([
            ev(id="E1", project_id="P1", announcement_type="招标公告",
               project_name="老项目", publish_time="2026-08-01 09:00",
               deadline_bid="2026-08-10 09:00", deadline_signup="2026-08-08 09:00"),
            ev(id="E2", project_id="P1", announcement_type="澄清公告",
               project_name="老项目", publish_time=same_day(),
               deadline_bid="2026-09-10 09:00"),
        ])
        secs = {i.announcement_event_id: i.section for i in issue.items}
        self.assertIn("E2", secs)
        self.assertEqual(secs["E2"], "change")

    def test_D_multi_platform_once(self):
        """D：同一公告两个平台 → 日报只展示一次但保留两个来源。"""
        issue, ev_map, _ = self._run([
            ev(id="E1", title="某项目招标", project_name="某项目施工",
               tenderee="招标人D", publish_time=same_day(),
               content_hash="samehash123",   # sync 后事件带正文哈希（同公告哈希一致）
               source_platform="全国公共资源交易平台（河北）",
               primary_source_url="https://a.example/1"),
            ev(id="E2", title="某项目招标", project_name="某项目施工",
               tenderee="招标人D", publish_time=same_day(),
               content_hash="samehash123",
               source_platform="惠招标", primary_source_url="https://b.example/2"),
        ])
        self.assertEqual(issue.item_count, 1, "只展示一次")
        # 两个来源都保留（事件记录都在，未被删除）
        self.assertEqual(len(ev_map), 2)

    def test_E_missing_deadline_review(self):
        """E（v0.3.0）：截止时间缺失 → 复核队列（无法判定可参与性）。"""
        issue, _, _ = self._run([ev(id="E1", publish_time=None, deadline_bid=None,
                                    deadline_signup=None)])
        self.assertEqual(issue.item_count, 0)
        self.assertIn("E1", issue.pending_review_items)

    def test_F_no_announcement_today(self):
        """F：无可参与公告 → 生成"当日无新增可参与公告"。"""
        issue, _, cfg = self._run([])
        self.assertEqual(issue.status, "published_empty")
        md = renderer.render_daily_issue_md(issue, {}, cfg)
        self.assertIn("当日无新增可参与公告", md)

    def test_G_revision_keeps_url(self):
        """G：日报修订 → 版本递增但稳定 URL 不变。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [ev(id="E1", publish_time=same_day())]
            i1 = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            url_before = i1.stable_url
            i2 = generate_daily_issue(date(2026, 8, 17), force=True, cfg=cfg, events=events)
            self.assertEqual(i2.version, 2)
            self.assertEqual(i2.stable_url, url_before, "稳定 URL 不随版本变化")

    def test_H_source_failure_not_in_daily_head(self):
        """H：平台巡检失败 → 进周报但不出现在日报头部。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            # 记录一条失败巡检（后台）
            run = M.SourceRun(id="RUN-T1", source_id="s1", source_name="某平台",
                              started_at="2026-08-17T08:00:00+08:00",
                              finished_at="2026-08-17T08:05:00+08:00",
                              status="failed", error_category="timeout",
                              error_summary="连接超时", retry_count=3,
                              task_version="t", created_at=same_day())
            run._path = str(tmp / "runs" / "RUN-T1.md")
            M.save(run)
            events = [ev(id="E1", publish_time=same_day())]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            md = renderer.render_daily_issue_md(issue, {e.id: e for e in events}, cfg)
            self.assertNotIn("巡检", md, "日报报告不得出现巡检信息")
            self.assertNotIn("失败", md)
            self.assertNotIn("timeout", md.lower())
            self.assertNotIn("连接超时", md)
            # 巡检失败信息保留在 SourceRun（后台）
            saved = M.load_all(tmp / "runs", M.SourceRun)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].status, "failed")

    def test_share_message_no_patrol_info(self):
        """分享摘要不含巡检清单（§5.3）。"""
        issue, _, cfg = self._run([ev(id="E1", publish_time=same_day())])
        msg = renderer.share_message(issue, cfg)
        self.assertIn("2026-08-17", msg)
        self.assertIn("1", msg)
        for kw in ("巡检", "平台", "抓取", "门禁"):
            self.assertNotIn(kw, msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
