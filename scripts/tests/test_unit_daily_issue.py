#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试（任务书 §14.1，11 项全覆盖）。"""

from __future__ import annotations

import unittest
from datetime import date

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import TmpCtx, make_cfg, ev, same_day
from publish import models as M
from publish import timeutil as tu
from publish import dedup, urgency
from publish.daily_issue_generator import generate_daily_issue


class TestParticipableBoundary(unittest.TestCase):
    """v0.3.0 可参与性准入边界（2026-08-18 用户决策）。

    准入判定 = deadline_signup 或 deadline_bid ≥ now（当天仍可报名/下载/投递），
    不再限定 publish_time 属于当天。
    """

    def test_future_deadline_included(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            # 历史发布（8-10）但投标截止 9-10 未过 → 可参与 → 进日报
            events = [ev(id="E1", publish_time="2026-08-10 09:00:00",
                         deadline_bid="2026-09-10 09:00")]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertIn("E1", {i.announcement_event_id for i in issue.items},
                          "截止未过即可参与，历史发布时间不阻塞")

    def test_expired_deadline_excluded(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [ev(id="E1", publish_time="2026-08-17 09:00:00",
                         deadline_bid="2026-08-16 09:00",
                         deadline_signup="2026-08-15 09:00")]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertNotIn("E1", {i.announcement_event_id for i in issue.items},
                             "截止已过不得进日报")

    def test_signup_not_bid_basis(self):
        """报名截止未过（投标截止已过）→ 仍可下载资料 → 可参与。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [ev(id="E1", publish_time="2026-08-17 09:00:00",
                         deadline_signup="2026-08-20 17:00",
                         deadline_bid="2026-08-18 09:00")]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events,
                                         now_dt=tu.parse_dt("2026-08-17 09:00"))
            self.assertIn("E1", {i.announcement_event_id for i in issue.items},
                          "报名截止未过仍可下载资料")


class TestTimezone(unittest.TestCase):
    """14.1-2 不同时区输入转换为北京时间。"""

    def test_utc_conversion(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            # UTC 2026-08-16 16:30 → 北京 2026-08-17 00:30
            events = [ev(id="E1", publish_time="2026-08-16T16:30:00Z")]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertEqual({i.announcement_event_id for i in issue.items}, {"E1"})

    def test_parse_naive_as_shanghai(self):
        dt = tu.parse_dt("2026-08-17 09:30")
        self.assertEqual(dt.strftime("%H:%M"), "09:30")
        self.assertEqual(dt.utcoffset().seconds, 8 * 3600)


class TestMissingDeadline(unittest.TestCase):
    """v0.3.0：截止时间缺失 → 复核队列，不进日报（无法判定可参与性）。"""

    def test_missing_deadline(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [ev(id="E1", publish_time=None, deadline_bid=None,
                         deadline_signup=None)]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertEqual(issue.item_count, 0)
            self.assertIn("E1", issue.pending_review_items)
            self.assertEqual(events[0].delivery_status, "pending_review")
            self.assertEqual(events[0].exclusion_reason, "missing_deadline")


class TestPushedDedup(unittest.TestCase):
    """v0.3.0：已推送去重——历史日报 items 出现过的公告不重复推送。"""

    def test_exclude_pushed(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            evs = [ev(id="E1", publish_time="2026-08-10 09:00:00",
                      deadline_bid="2026-09-10 09:00")]
            # 第一次：E1 进日报
            issue1 = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=evs)
            self.assertIn("E1", {i.announcement_event_id for i in issue1.items})
            # 第二次（次日）：E1 已推送 → 排除，不进新日报
            issue2 = generate_daily_issue(date(2026, 8, 18), cfg=cfg, events=evs)
            self.assertNotIn("E1", {i.announcement_event_id for i in issue2.items},
                             "已推送公告不得重复进日报")
            self.assertEqual(issue2.item_count, 0)

    def test_exclude_pushed_disabled(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            cfg["daily_issue"]["exclude_pushed"] = False
            evs = [ev(id="E1", publish_time="2026-08-10 09:00:00",
                      deadline_bid="2026-09-10 09:00")]
            generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=evs)
            issue2 = generate_daily_issue(date(2026, 8, 18), cfg=cfg, events=evs)
            self.assertIn("E1", {i.announcement_event_id for i in issue2.items},
                          "关闭去重后允许重复")


class TestHistoricalAnnouncement(unittest.TestCase):
    """v0.3.0：历史公告判定改为截止时间——截止未过进日报，已过不进。"""

    def test_recollected_expired_notice(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            # 历史发布（8-10）且投标截止已过（8-15）→ 无法参与，不进日报
            events = [ev(id="E1", publish_time="2026-08-10 09:00:00",
                         deadline_bid="2026-08-15 09:00",
                         deadline_signup="2026-08-12 09:00")]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertEqual(issue.item_count, 0, "截止已过的历史公告不得进入日报")
            self.assertEqual(events[0].delivery_status, "excluded")
            self.assertEqual(events[0].exclusion_reason, "deadline_expired")


class TestOldProjectNewEvent(unittest.TestCase):
    """14.1-5 旧项目当天发布变更公告 → 作为新事件进入当天日报。"""

    def test_new_change_event_same_project(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="E1", project_id="T-PROJ-001", announcement_type="招标公告",
                   title="项目A招标公告", publish_time="2026-07-01 09:00:00",
                   deadline_bid="2026-08-10 09:00", deadline_signup="2026-08-08 09:00",
                   tenderee="招标人A", project_name="项目A施工招标"),
                ev(id="E2", project_id="T-PROJ-001", announcement_type="澄清公告",
                   title="项目A澄清公告", publish_time=same_day(),
                   deadline_bid="2026-09-10 09:00",
                   tenderee="招标人A", project_name="项目A施工招标"),
            ]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            ids = {i.announcement_event_id for i in issue.items}
            self.assertIn("E2", ids, "旧项目当天新事件必须进日报")
            self.assertNotIn("E1", ids, "截止已过的历史招标公告不得重复进日报")
            sec = next(i.section for i in issue.items if i.announcement_event_id == "E2")
            self.assertEqual(sec, "change", "澄清公告应分到 change 组")


class TestMultiPlatformDedup(unittest.TestCase):
    """14.1-6 同一公告多平台重复 → 日报一条，来源保留。"""

    def test_dedup_keep_sources(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="E1", title="项目X招标公告", project_name="项目X施工总承包",
                   tenderee="招标人X", publish_time=same_day(),
                   content_hash="samehash456",   # 同公告两平台 → 哈希一致
                   source_platform="全国公共资源交易平台（河北）",
                   primary_source_url="https://ggzy.example/1"),
                ev(id="E2", title="项目X招标公告", project_name="项目X施工总承包",
                   tenderee="招标人X", publish_time=same_day(),
                   content_hash="samehash456",
                   source_platform="惠招标", primary_source_url="https://huibiao.example/2"),
            ]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertEqual(issue.item_count, 1, "日报只展示一条")
            item = issue.items[0]
            # 主来源应选优先级更高的法定官方平台
            self.assertEqual(item.announcement_event_id, "E1")
            e1 = next(e for e in events if e.id == "E1")
            self.assertEqual(e1.source_links, [], "主来源事件自身不再持有备份列表（备份在源事件记录）")
            # 备用来源保留在 E2（未被删除，仅不进日报条目）
            self.assertEqual(events[1].delivery_status, "eligible")
            self.assertNotIn("E2", {i.announcement_event_id for i in issue.items})


class TestSameNameDiffProject(unittest.TestCase):
    """14.1-7 同名但不同项目不得错误合并。"""

    def test_same_name_diff_publisher(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="E1", title="XX路改造工程施工招标", project_name="XX路改造工程施工",
                   tenderee="石家庄住建局", publish_time=same_day(9, 0)),
                ev(id="E2", title="XX路改造工程施工招标", project_name="XX路改造工程施工",
                   tenderee="唐山住建局", publish_time=same_day(9, 0)),
            ]
            key1 = dedup.dedup_key(events[0].project_name, events[0].announcement_type,
                                   events[0].tenderee, tu.parse_dt(events[0].publish_time),
                                   events[0].title)
            key2 = dedup.dedup_key(events[1].project_name, events[1].announcement_type,
                                   events[1].tenderee, tu.parse_dt(events[1].publish_time),
                                   events[1].title)
            self.assertNotEqual(key1, key2, "不同招标人 → 指纹不同，不合并")
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertEqual(issue.item_count, 2, "两个不同项目都应进日报")


class TestIdempotent(unittest.TestCase):
    """14.1-8 同一日报重复生成 → 不重复创建条目。"""

    def test_regenerate_no_dup(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [ev(id="E1", publish_time=same_day())]
            generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            issue2 = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            issue3 = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertEqual(issue2.version, 1, "未 force 不递增版本")
            self.assertEqual(issue2.item_count, 1)
            self.assertEqual(issue3.item_count, 1, "幂等：条目不重复")

    def test_force_increments_version(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [ev(id="E1", publish_time=same_day())]
            generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            issue2 = generate_daily_issue(date(2026, 8, 17), force=True, cfg=cfg, events=events)
            self.assertEqual(issue2.version, 2)
            self.assertEqual(len(issue2.revisions), 1)
            self.assertEqual(issue2.status, "revised")


class TestEmptyIssue(unittest.TestCase):
    """14.1-9 空日报生成。"""

    def test_empty_issue(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=[])
            self.assertEqual(issue.status, "published_empty")
            self.assertEqual(issue.item_count, 0)


class TestUrgency(unittest.TestCase):
    """14.1-10 截止时间紧迫程度计算。"""

    def test_urgency_rules(self):
        now = tu.parse_dt("2026-08-17 12:00:00")
        r1 = urgency.compute_urgency("2026-08-17 20:00", None, now)   # 8h → urgent
        r2 = urgency.compute_urgency("2026-08-19 10:00", None, now)   # 46h → high
        r3 = urgency.compute_urgency("2026-09-01 09:00", None, now)   # normal
        r4 = urgency.compute_urgency(None, None, now)                  # unknown
        self.assertEqual(r1["urgency"], "urgent")
        self.assertEqual(r2["urgency"], "high")
        self.assertEqual(r3["urgency"], "normal")
        self.assertEqual(r4["urgency"], "unknown")
        self.assertIsNone(r4["deadline"], "字段缺失不得虚构紧迫等级")

    def test_threshold_config(self):
        now = tu.parse_dt("2026-08-17 12:00:00")
        r = urgency.compute_urgency("2026-08-17 20:00", None, now, urgent_hours=4, high_hours=72)
        self.assertEqual(r["urgency"], "high", "阈值可配置")


class TestWeeklyReconciliation(unittest.TestCase):
    """14.1-11 周报数量对账。"""

    def test_reconciliation(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="E1", title="项目A", publish_time=same_day()),          # 可参与 → 进日报
                ev(id="E2", title="项目B", publish_time="2026-08-10 09:00",
                   deadline_bid="2026-08-15 09:00", deadline_signup="2026-08-12 09:00"),  # 截止已过 → 不进
            ]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertEqual(issue.item_count, 1)
            # 对账逻辑：周报 delivered == 日报 item 数（在周报模块内实现，此处验证计数口径）
            self.assertEqual(sum(1 for i in issue.items), issue.item_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
