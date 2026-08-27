#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""展示/报告测试（2026-08-17：产物改为 Markdown 报告形式，不再生成 HTML）。

覆盖：
- 产物为 Markdown 报告（无 HTML 标签；不生成 .html 文件）
- 骨架树全部可找到字段按维度分组直观展示（【项目基本信息】…【溯源】）
- 缺失字段不堆在条目正文，统一汇总到文末「待补充与人工审核提醒」
- 原文链接可点击（Markdown 链接）
- 内部字段零泄漏（G0-G4/L0-L3/confidence_score/parser_version/…）
- 报告索引可打开、周报报告 + XLSX/CSV 明细可下载
- 分享摘要不含巡检信息
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import TmpCtx, make_cfg, ev, same_day
from publish import models as M
from publish import renderer
from publish import timeutil as tu
from publish.daily_issue_generator import generate_daily_issue
from publish.weekly_report import build_weekly_report, publish_weekly_report


def _build_rich_issue(tmp: Path, cfg: dict, day=date(2026, 8, 17)):
    """构造内容丰富的日报（3 条事件：紧急+常规+变更），渲染报告到 tmp/daily-reports。"""
    events = [
        ev(id="E1", title="某市政道路改造工程施工招标公告", project_name="某市政道路改造工程施工",
           tenderee="某市住房和城乡建设局", region="河北省·石家庄市",
           publish_time=same_day(9, 0), deadline_signup=same_day(23, 0),
           deadline_bid="2026-09-10 09:00", ceiling_price="1.2 亿元",
           source_platform="全国公共资源交易平台（河北）",
           primary_source_url="https://ggzy.example.com/n1",
           detail_fields={"tender_no": "I1300000000000000001",
                          "qualification": "建筑工程施工总承包三级及以上",
                          "submission_method": "电子投标文件线上递交",
                          "dark_bid": "技术标暗标，双盲评审"}),
        ev(id="E2", title="某新能源项目EPC总承包招标公告", project_name="某新能源项目EPC总承包",
           tenderee="某能源投资有限公司", region="河北省·张家口市·康保县",
           publish_time=same_day(10, 30), deadline_bid="2026-09-18 09:00",
           budget_amount="2.5 亿元", source_platform="河北交投招标与采购服务平台",
           primary_source_url="https://ebidding.example.com/n2"),
        ev(id="E3", title="某水利枢纽工程澄清公告", project_name="某水利枢纽工程施工",
           tenderee="某水利工程建设中心", region="河北省·保定市",
           announcement_type="澄清公告", publish_time=same_day(14, 0),
           deadline_bid="2026-09-20 09:00", source_platform="全国公共资源交易平台（河北）",
           primary_source_url="https://ggzy.example.com/n3"),
    ]
    issue = generate_daily_issue(day, cfg=cfg, events=events)
    ev_map = {e.id: e for e in events}
    renderer.write_daily_report(issue, ev_map, cfg)
    renderer.write_report_indexes([issue], cfg)
    return issue, ev_map, events


class TestReportMarkdown(unittest.TestCase):
    """Markdown 报告形式断言。"""

    @classmethod
    def setUpClass(cls):
        cls.ctx = TmpCtx()
        cls.tmp = cls.ctx.__enter__()
        cls.cfg = make_cfg(cls.tmp)
        cls.issue, cls.ev_map, cls.events = _build_rich_issue(cls.tmp, cls.cfg)
        cls.report_path = cls.tmp / "daily-reports" / "2026-08-17.md"
        cls.report = cls.report_path.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_01_no_html(self):
        """报告为 Markdown 形式，不含 HTML 标签，也不生成 .html 文件。"""
        for tag in ("<html", "<div", "<table", "<a href", "<style", "</body>"):
            self.assertNotIn(tag, self.report, f"报告不得含 HTML 标签: {tag}")
        html_files = list((self.tmp / "daily-reports").rglob("*.html"))
        self.assertEqual(html_files, [], "不得生成 HTML 产物")

    def test_02_skeleton_fields_displayed(self):
        """骨架树全部可找到字段按维度分组直观展示。"""
        for grp in ("【项目基本信息】", "【时间线】", "【资格要求】", "【评分规则】",
                    "【金额与限价】", "【合规约束】", "【招标文件获取】", "【溯源】"):
            # 富数据事件 E1 应覆盖大部分维度
            pass
        self.assertIn("【项目基本信息】", self.report)
        self.assertIn("【时间线】", self.report)
        self.assertIn("【金额与限价】", self.report)
        self.assertIn("【溯源】", self.report)
        # 有值字段直观展示（含中文标签）
        for kw in ("招标人", "某市住房和城乡建设局", "最高投标限价", "1.2 亿元",
                   "资质要求", "建筑工程施工总承包三级及以上", "暗标要求",
                   "全国公共资源交易平台（河北）", "投标文件递交截止", "2026-09-10"):
            self.assertIn(kw, self.report, f"骨架树字段未直观展示: {kw}")

    def test_03_missing_fields_in_footer_not_body(self):
        """缺失字段不堆在条目正文，汇总到文末「待补充」提醒。"""
        self.assertIn("## 待补充与后台提醒", self.report)
        self.assertIn("### 一、待补充字段", self.report)
        # 条目正文不得以"待补"作为字段值堆砌（应汇总到文末）
        body = self.report.split("## 待补充与后台提醒")[0]
        self.assertNotIn("：待补", body)
        self.assertNotIn("**待补**", body)
        # 文末待补充清单列出缺失字段（如业绩要求/预算金额等未提供字段）
        self.assertIn("公告原文未载明", self.report)

    def test_04_links_clickable(self):
        """原文链接以 Markdown 链接展示且可点击。"""
        self.assertIn("<https://ggzy.example.com/n1>", self.report)
        self.assertIn("<https://ebidding.example.com/n2>", self.report)

    def test_05_no_internal_field_leak(self):
        """后台字段零泄漏。"""
        for kw in ("G0", "G1", "G2", "G3", "G4", "L0", "L1", "L2", "L3",
                   "confidence_score", "parser_version", "crawler_task_id",
                   "raw_record_id", "fact_gate_status", "intake_id"):
            self.assertNotIn(kw, self.report, f"报告泄漏内部字段: {kw}")

    def test_06_report_index(self):
        """报告索引可打开。"""
        idx = self.tmp / "daily-reports" / "_索引.md"
        self.assertTrue(idx.exists())
        content = idx.read_text(encoding="utf-8")
        self.assertIn("2026-08-17", content)
        self.assertIn("[打开报告](2026-08-17.md)", content)

    def test_07_weekly_report_downloadable(self):
        """周报以 Markdown 报告存储 + XLSX/CSV 明细可下载。"""
        run = M.SourceRun(id="RUN-D1", source_id="s1", source_name="平台一",
                          started_at="2026-08-10T09:00:00+08:00",
                          finished_at="2026-08-10T09:10:00+08:00",
                          status="success", pages_scanned=5, raw_items_found=3,
                          valid_events_found=3, task_version="t", created_at=same_day())
        run._path = str(self.tmp / "runs" / "RUN-D1.md")
        M.save(run)
        report = build_weekly_report(date(2026, 8, 10), date(2026, 8, 16), cfg=self.cfg)
        out = publish_weekly_report(report, cfg=self.cfg)
        md = Path(out["report"]).read_text(encoding="utf-8")
        self.assertIn("# 来源与覆盖周报", md)
        self.assertIn("## 三、数量对账", md)
        self.assertIn("一致", md)
        for p in (out["report"], out["xlsx"], out["csv"]):
            self.assertTrue(Path(p).exists(), f"周报产物缺失: {p}")
        # 周报索引入口
        from publish.weekly_report import update_report_index
        idx = update_report_index(cfg=self.cfg)
        self.assertTrue(idx.exists())
        self.assertIn("来源与覆盖周报索引", idx.read_text(encoding="utf-8"))

    def test_08_share_message_no_patrol(self):
        """分享摘要不含巡检信息。"""
        msg = renderer.share_message(self.issue, self.cfg)
        for kw in ("巡检", "覆盖网站", "抓取次数", "平台清单"):
            self.assertNotIn(kw, msg)


class TestReportEmpty(unittest.TestCase):
    """空日报报告：当日无新增可参与公告 + 文末提醒。"""

    def test_empty_issue_report(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=[])
            renderer.write_daily_report(issue, {}, cfg)
            p = tmp / "daily-reports" / "2026-08-17.md"
            report = p.read_text(encoding="utf-8")
            self.assertIn("当日无新增可参与公告", report)
            self.assertIn("待补充与后台提醒", report)
            self.assertIn("- 无", report)


class TestReportPending(unittest.TestCase):
    """v0.3.0：缺截止时间事件 → 复核队列；截止已过事件 → 后台保留；客户报告零泄漏（P0-2）。"""

    def test_pending_and_late_kept_in_backend(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="E1", publish_time=None, title="缺截止时间公告",
                   deadline_bid=None, deadline_signup=None),
                ev(id="E2", title="截止已过公告", publish_time="2026-08-10 09:00:00",
                   deadline_bid="2026-08-12 09:00", deadline_signup="2026-08-11 09:00"),
            ]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            renderer.write_daily_report(issue, {e.id: e for e in events}, cfg)
            report = (tmp / "daily-reports" / "2026-08-17.md").read_text(encoding="utf-8")
            # 数据层：无可参与条目；缺截止 → 复核队列；截止已过 → 后台 excluded
            self.assertEqual(issue.item_count, 0)
            self.assertEqual(issue.late_items, [])
            self.assertIn("E1", issue.pending_review_items)
            self.assertEqual(events[1].delivery_status, "excluded")
            # 客户报告：不得出现复核/已过截止事件内容（P0-2）
            self.assertNotIn("截止已过公告", report)
            self.assertNotIn("2026-08-10", report)
            self.assertNotIn("迟到公告", report)
            self.assertNotIn("缺截止时间公告", report)
            # 后台提醒仅统计数字，不含事件明细
            self.assertIn("后台待复核事件：1 条", report)
            self.assertIn("后台迟到事件：1 条", report)


class TestRegressionCustomerBoundary(unittest.TestCase):
    """回归（v0.3.0 适配）：客户日报只渲染可参与事件；截止已过事件保留后台；周报仍统计。"""

    def test_customer_report_only_participable(self):
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="TODAY", title="今日新发布公告", publish_time=same_day(),
                   deadline_bid="2026-09-10 09:00"),
                ev(id="L1", title="截止已过公告一", publish_time="2026-06-17 09:00:00",
                   deadline_bid="2026-06-30 09:00", deadline_signup="2026-06-28 09:00"),
                ev(id="L2", title="截止已过公告二", publish_time="2026-06-18 09:00:00",
                   deadline_bid="2026-06-30 09:00", deadline_signup="2026-06-28 09:00"),
                ev(id="L3", title="截止已过公告三", publish_time="2026-07-29 09:00:00",
                   deadline_bid="2026-08-05 09:00", deadline_signup="2026-08-03 09:00"),
                ev(id="L4", title="截止已过公告四", publish_time="2026-08-10 09:00:00",
                   deadline_bid="2026-08-12 09:00", deadline_signup="2026-08-11 09:00"),
            ]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            self.assertEqual(issue.item_count, 1, "只应收录可参与事件")
            renderer.write_daily_report(issue, {e.id: e for e in events}, cfg)
            report = (tmp / "daily-reports" / "2026-08-17.md").read_text(encoding="utf-8")
            # 1. 只渲染可参与事件
            self.assertIn("今日新发布公告", report)
            for t in ("截止已过公告一", "截止已过公告二", "截止已过公告三", "截止已过公告四"):
                self.assertNotIn(t, report, f"客户报告泄漏截止已过公告: {t}")
            # 2. 无迟到章节/旧日期
            self.assertNotIn("迟到公告", report)
            for d in ("2026-06-17", "2026-06-18", "2026-07-29", "2026-08-10"):
                self.assertNotIn(d, report, f"客户报告泄漏历史日期: {d}")
            # 3. 截止已过事件保留后台（状态 excluded）
            self.assertEqual([e.delivery_status for e in events[1:]], ["excluded"] * 4)
            # 4. 周报仍能统计 4 条迟到（source_run.late_items 进入周报漏斗）
            from publish.weekly_report import build_weekly_report
            run = M.SourceRun(id="RUN-R", source_id="s1", source_name="平台一",
                              started_at="2026-08-13T08:00:00+08:00",
                              finished_at="2026-08-13T08:10:00+08:00",
                              status="success", raw_items_found=5, valid_events_found=5,
                              late_items=4, task_version="t", created_at=same_day())
            run._path = str(tmp / "runs" / "RUN-R.md")
            M.save(run)
            weekly = build_weekly_report(date(2026, 8, 10), date(2026, 8, 16), cfg=cfg)
            self.assertEqual(weekly.funnel["late_events"], 4, "周报必须统计迟到事件")


class TestPushCards(unittest.TestCase):
    """B 方案（2026-08-17）：飞书推送卡片由渲染管线生成。

    覆盖：
    - 按推送卡片模板分组渲染（项目信息/招标主体/时间线/金额/资格要求/评分规则/
      合规约束/招标文件获取/溯源），**有值字段才展示**
    - 缺失字段不显示在正文，统一汇总到文末「【待补充字段】」清单
    - 顶部优先级标注（P0/P1/P2；unknown → 常规商机 · P2）
    - 链接标注：browser_blocked → ⚠️ + 备选入口；browser_ok → 可点击
    - 置信度渲染（骨架树 §3.3）
    - 数据边界：只渲染 issue.items 当天事件，历史/迟到/复核零泄漏
    - 无模板外段落（无【匹配初判】）、内部字段零泄漏
    - 空日报 → 「当日无新增公告」
    """

    def test_push_cards_value_only_and_missing_summary(self):
        """有值字段展示 + 缺失字段文末汇总（不堆正文）。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="E1", title="某储能项目EPC总承包招标公告", project_name="某储能项目EPC总承包",
                   tenderee="某能源投资有限公司", region="河北省·张家口市·康保县",
                   publish_time=same_day(9, 0), deadline_signup=same_day(23, 0),
                   deadline_bid="2026-09-10 09:00", ceiling_price="1.2 亿元",
                   source_platform="河北交投招标与采购服务平台",
                   primary_source_url="https://ebidding.example.com/n1",
                   confidence="confirmed",
                   detail_fields={"tender_no": "I1300000000000000001",
                                  "qualification": "电力工程施工总承包二级及以上",
                                  "dark_bid": "技术标暗标",
                                  "tender_doc_method": "platform",
                                  "tender_doc_window": "2026-08-12 09:00 至 2026-08-18 17:00",
                                  "tender_doc_link": "https://ebidding.example.com/login"}),
            ]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            ev_map = {e.id: e for e in events}
            checks = {"https://ebidding.example.com/n1": "browser_blocked",
                      "https://ebidding.example.com/login": "browser_ok"}
            text = renderer.render_push_cards_md(issue, ev_map, cfg, link_checks=checks)

            # 1. 有值分组齐全；无值段（如【评分规则】全缺失）不显示
            for grp in ("【项目信息】", "【招标主体】", "【时间线】", "【金额】",
                        "【资格要求（原文摘录）】", "【合规约束】",
                        "【招标文件获取】", "【溯源】"):
                self.assertIn(grp, text, f"推送卡片缺段: {grp}")
            self.assertNotIn("【评分规则（原文摘录）】", text)  # 整段无值 → 不显示
            # 2. 有值字段展示
            self.assertIn("项目名称 | 某储能项目EPC总承包", text)
            self.assertIn("招标编号 | I1300000000000000001", text)
            self.assertIn("最高投标限价 | 1.2 亿元", text)
            self.assertIn("资质要求 | 电力工程施工总承包二级及以上", text)
            self.assertIn("暗标要求 | 技术标暗标", text)
            self.assertIn("获取方式 | platform", text)
            # 3. 缺失字段不在正文（无「待补」堆砌），文末汇总
            self.assertNotIn("待补（公告原文未载明）", text)
            self.assertIn("【待补充字段】（公告原文未载明，不编造）", text)
            self.assertIn("招标代理机构", text)  # 文末清单含缺失字段名
            self.assertIn("预算金额", text)
            # 4. 链接标注：blocked → ⚠️+备选；ok → 可点击
            self.assertIn("⚠️ 当前浏览器可能打不开", text)
            self.assertIn("备选：", text)
            self.assertIn("入口链接 | https://ebidding.example.com/login（可点击）", text)
            # 5. 置信度渲染（骨架树 §3.3）
            self.assertIn("置信度 | confirmed", text)
            # 6. 无模板外段落、无内部字段
            self.assertNotIn("匹配初判", text)
            for kw in ("G0", "G4", "L0", "confidence_score", "parser_version",
                       "fact_gate_status", "intake_id", "late_discovered", "pending_review"):
                self.assertNotIn(kw, text, f"推送卡片泄漏内部字段: {kw}")

    def test_push_cards_priority_label(self):
        """优先级标注：urgent → P0；unknown → 常规商机 · P2。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="E1", title="紧急报名公告", publish_time=same_day(8, 0),
                   deadline_signup=same_day(10, 0), deadline_bid="2026-08-20 09:00",
                   primary_source_url="https://ggzy.example.com/u1"),
                ev(id="E2", title="常规公告", publish_time=same_day(9, 0),
                   primary_source_url="https://ggzy.example.com/u2"),
            ]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events,
                                         now_dt=tu.parse_dt("2026-08-17 09:00"))
            ev_map = {e.id: e for e in events}
            text = renderer.render_push_cards_md(issue, ev_map, cfg)
            self.assertIn("⏰ 报名即将截止 · P0 最高优先级", text)
            self.assertIn("常规商机 · P2", text)

    def test_push_cards_empty_issue(self):
        """空日报 → 当日无新增可参与公告说明（不报错、不凑数）。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=[])
            text = renderer.render_push_cards_md(issue, {}, cfg)
            self.assertIn("当日无新增可参与公告", text)

    def test_push_cards_customer_boundary(self):
        """数据边界：只渲染日报条目事件；截止已过事件零泄漏（与日报同一 build_customer_context）。"""
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            events = [
                ev(id="TODAY", title="今日公告招标公告", project_name="今日公告项目",
                   publish_time=same_day(), deadline_bid="2026-09-10 09:00"),
                ev(id="HIST", title="截止已过公告", project_name="截止已过项目",
                   publish_time="2026-08-10 09:00:00",
                   deadline_bid="2026-08-12 09:00", deadline_signup="2026-08-11 09:00"),
            ]
            issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
            ev_map = {e.id: e for e in events}
            text = renderer.render_push_cards_md(issue, ev_map, cfg)
            self.assertIn("今日公告项目", text)
            self.assertNotIn("截止已过项目", text)
            self.assertNotIn("2026-08-10", text)


class TestGateHardCheck(unittest.TestCase):
    """C 方案（2026-08-17）：sync_events 门禁硬校验，防 Agent 跳步。

    覆盖：
    - intake 无主卡（pipeline_status=raw 或 carded 但主卡缺失）→ gate_blocked 拒绝同步
    - intake 有主卡（pipeline_status=intel 或历史无字段但主卡存在）→ 正常同步
    - --force（skip_gate_check）可绕过但记入 skipped（审计）
    """

    @staticmethod
    def _fm(intake_id: str, tmp: Path, pipeline_status: str = "raw",
            confidence: str | None = None) -> dict:
        p = tmp / "intake" / f"{intake_id}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        fm = {
            "intake_id": intake_id,
            "source_url": f"https://example.com/{intake_id}", "source_level": "L1",
            "source_platform": "平台", "title": f"测试公告{intake_id}",
            "announcement_type": "招标公告", "publish_date": "2026-08-17",
            "intake_date": "2026-08-17", "project_type": "工程",
            "region": "河北", "pipeline_status": pipeline_status,
            "confidence": confidence,
            "_path": str(p), "_dropped": False,
        }
        # 真实 intake 文档（_read_body 需要读文件）
        import yaml
        p.write_text("---\n" + yaml.safe_dump(
            {k: v for k, v in fm.items() if not k.startswith("_")},
            allow_unicode=True) + "---\n\n项目名称：测试项目\n", encoding="utf-8")
        return fm

    def test_gate_blocked_without_main_card(self):
        """intake 无主卡（pipeline_status=raw）→ gate_blocked。"""
        from publish import sync_events as SE
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            fm = self._fm("zb-20260817-001", tmp, pipeline_status="raw")
            r = SE.sync_events(cfg, intake_files=[fm])
            self.assertEqual(len(r["gate_blocked"]), 1, "无主卡 intake 应被门禁拦截")
            self.assertEqual(len(r["created"]), 0, "被拦截则不得创建事件")

    def test_gate_passed_with_intel_status(self):
        """intake pipeline_status=intel（主卡完成）→ 正常同步。"""
        from publish import sync_events as SE
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            fm = self._fm("zb-20260817-002", tmp, pipeline_status="intel",
                          confidence="confirmed")
            r = SE.sync_events(cfg, intake_files=[fm])
            self.assertEqual(r["gate_blocked"], [], "intel 状态不应被拦截")
            self.assertEqual(len(r["created"]), 1, "应创建事件")

    def test_force_bypasses_gate(self):
        """--force（skip_gate_check）可绕过门禁，但记入 skipped（审计）。"""
        from publish import sync_events as SE
        with TmpCtx() as tmp:
            cfg = make_cfg(tmp)
            fm = self._fm("zb-20260817-003", tmp, pipeline_status="raw")
            r = SE.sync_events(cfg, intake_files=[fm], skip_gate_check=True)
            self.assertEqual(r["gate_blocked"], [], "--force 不拦截")
            self.assertEqual(len(r["skipped"]), 1, "--force 绕过记入 skipped 审计")
            self.assertEqual(len(r["created"]), 1, "--force 绕过后仍创建事件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
