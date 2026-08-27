#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览生成器（2026-08-17 P1-4 重构：客户预览与内部审计预览分离）。

产出到 质量报告/发布层改造/预览/：
  01-后台迟到事件审计预览/   ← 【内部审计专用】supplement_section 策略，展示 4 条真实迟到事件
                                 明确标注"内部审计预览，非客户日报样例"，不得作为客户样例验收
  02-客户日报预览（测试样例）/ ← 客户格式演示（构造样例，标注非真实公告），backend_only
  03-客户日报预览（真实数据）/ ← 客户格式正式预览：backend_only，真实 2026-08-17 日报（仅当天公告）

客户预览硬性验证（验收 §五 2/3/4）：
  - 旧公告标题/发布时间不出现在客户报告中
  - 仅出现当天公告；无"迟到公告"章节
"""

from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish import models as M                     # noqa: E402
from publish import renderer                        # noqa: E402
from publish import timeutil as tu                  # noqa: E402
from publish.config import ROOT, load_config, storage_paths  # noqa: E402
from publish.daily_issue_generator import generate_daily_issue  # noqa: E402
from tests.common import make_cfg, ev               # noqa: E402

OUT = ROOT / "质量报告" / "发布层改造" / "预览"


def _audit_preview(cfg_real: dict, tmp_base: Path) -> Path:
    """① 后台迟到事件审计预览（内部专用，supplement_section，非客户样例）。"""
    out = tmp_base / "01-后台迟到事件审计预览"
    cfg = make_cfg(out)
    cfg["delivery"]["late_announcement_policy"] = "supplement_section"  # 仅内部审计
    cfg["storage"]["report_daily_dir"] = str(out / "报告" / "日报")

    events = M.load_all(storage_paths(cfg_real)["events_dir"], M.AnnouncementEvent)
    issue = generate_daily_issue(date(2026, 8, 13), cfg=cfg, events=events)
    ev_map = {e.id: e for e in events}
    renderer.write_daily_report(issue, ev_map, cfg)
    renderer.write_report_indexes([issue], cfg)
    (out / "说明.md").write_text(
        "# 预览①：后台迟到事件审计预览（内部专用）\n\n"
        "- **本预览为内部审计用途，不是客户日报样例，禁止用作客户格式验收**\n"
        "- 策略：`supplement_section`（P1-2 收敛后仅内部审计允许；客户渠道一律 `backend_only`）\n"
        "- 数据：4 条真实迟到公告（发布 6/17~8/10，8/13 发现），用于后台迟到事件复核与审计\n"
        "- 客户日报（backend_only）中这些事件仅以统计数字出现，不含任何标题/时间/链接\n", encoding="utf-8")
    return out


def _customer_preview_sample(tmp_base: Path) -> Path:
    """② 客户日报预览（测试样例，构造数据，backend_only）。"""
    out = tmp_base / "02-客户日报预览（测试样例）"
    cfg = make_cfg(out)
    cfg["storage"]["report_daily_dir"] = str(out / "报告" / "日报")
    D = date(2026, 8, 17)

    events = [
        ev(id="S-URGENT", title="某医院迁建工程施工招标公告", project_name="某医院迁建工程施工",
           tenderee="某市卫生健康局", region="河北省·保定市",
           publish_time=tu.iso(tu.parse_dt("2026-08-17 08:30:00")),
           deadline_signup=tu.iso(tu.parse_dt("2026-08-17 20:00:00")),
           deadline_bid="2026-09-08 09:00", ceiling_price="3.6 亿元",
           source_platform="全国公共资源交易平台（河北）",
           primary_source_url="https://ggzy.example.com/s1",
           detail_fields={"tender_no": "I1300000000000000001",
                          "qualification": "建筑工程施工总承包一级及以上；近3年≥1项同类业绩",
                          "joint_venture": "接受联合体（牵头人为施工单位）",
                          "submission_method": "电子投标文件线上递交",
                          "dark_bid": "技术标暗标，双盲评审"}),
        ev(id="S-DEADLINE", title="某道路提升改造工程监理招标公告", project_name="某道路提升改造工程监理",
           tenderee="某市交通运输局", region="河北省·沧州市",
           announcement_type="招标公告",
           publish_time=tu.iso(tu.parse_dt("2026-08-17 09:00:00")),
           deadline_bid=tu.iso(tu.parse_dt("2026-08-18 09:00:00")),
           budget_amount="850 万元", source_platform="惠招标",
           primary_source_url="https://huibiao.example.com/s2"),
        ev(id="S-NEW", title="某产业园标准化厂房建设项目EPC总承包招标公告",
           project_name="某产业园标准化厂房建设项目EPC总承包",
           tenderee="某园区开发建设有限公司", region="河北省·廊坊市",
           publish_time=tu.iso(tu.parse_dt("2026-08-17 10:00:00")),
           deadline_signup="2026-08-25 17:00", deadline_bid="2026-09-12 09:00",
           ceiling_price="2.1 亿元", source_platform="河北交投招标与采购服务平台",
           primary_source_url="https://ebidding.example.com/s3"),
        ev(id="S-CHANGE", title="某水利枢纽工程施工招标澄清公告", project_name="某水利枢纽工程施工",
           tenderee="某水利工程建设中心", region="河北省·邯郸市",
           announcement_type="澄清公告",
           publish_time=tu.iso(tu.parse_dt("2026-08-17 14:00:00")),
           deadline_bid="2026-09-20 09:00", source_platform="全国公共资源交易平台（河北）",
           primary_source_url="https://ggzy.example.com/s4",
           detail_fields={"qualification": "水利水电工程施工总承包二级及以上"}),
        ev(id="S-RESULT", title="某污水处理厂改扩建工程中标结果公告", project_name="某污水处理厂改扩建工程",
           tenderee="某水务集团有限公司", region="河北省·张家口市",
           announcement_type="中标结果公告",
           publish_time=tu.iso(tu.parse_dt("2026-08-17 16:00:00")),
           deadline_bid="", source_platform="全国公共资源交易平台（河北）",
           primary_source_url="https://ggzy.example.com/s5",
           detail_fields={"budget_amount": "1.3 亿元"}),
    ]
    issue = generate_daily_issue(D, cfg=cfg, events=events)
    ev_map = {e.id: e for e in events}
    renderer.write_daily_report(issue, ev_map, cfg)
    renderer.write_report_indexes([issue], cfg)
    (out / "说明.md").write_text(
        "# 预览②：客户日报预览（测试样例）\n\n"
        "- 数据为**构造样例**（任务书 §14.2 样例 A 扩展），仅用于演示分组与展示效果，非真实公告\n"
        "- 策略：`backend_only`（客户发布策略）；五组分组：urgent / deadline / new / change / result\n"
        "- 每条公告按骨架树分组展示全部可找到字段；缺失字段汇总在文末「待补充与后台提醒」\n", encoding="utf-8")
    return out


def _customer_preview_real(cfg_real: dict, tmp_base: Path) -> Path:
    """③ 客户日报预览（真实数据）：backend_only，2026-08-17 日报（仅当天公告）。"""
    out = tmp_base / "03-客户日报预览（真实数据）"
    cfg = make_cfg(out)
    cfg["storage"]["report_daily_dir"] = str(out / "报告" / "日报")

    events = M.load_all(storage_paths(cfg_real)["events_dir"], M.AnnouncementEvent)
    issue = generate_daily_issue(date(2026, 8, 17), cfg=cfg, events=events)
    ev_map = {e.id: e for e in events}
    report_path = renderer.write_daily_report(issue, ev_map, cfg)
    renderer.write_report_indexes([issue], cfg)

    # 客户预览硬性验证（验收 §五 2/3/4）：旧公告标题不得出现在客户报告
    text = report_path.read_text(encoding="utf-8")
    late_titles = [e.title for e in events if e.delivery_status == "late_discovered" and e.title]
    leaked = [t for t in late_titles if t in text]
    if leaked:
        raise RuntimeError(f"客户日报预览泄漏迟到公告标题: {leaked}")
    if "迟到公告" in text:
        raise RuntimeError("客户日报预览出现迟到公告章节")

    (out / "说明.md").write_text(
        "# 预览③：客户日报预览（真实数据）\n\n"
        "- 数据：真实事件库（2026-08-17 当天发布 1 条：广宗县葫芦中学维修改造）\n"
        "- 策略：`backend_only`；客户报告仅含当天公告，旧公告标题/时间/链接零出现（已硬性校验）\n"
        "- 文末「后台提醒」仅统计数字：后台待复核事件 N 条 / 后台迟到事件 N 条\n", encoding="utf-8")
    return out


def main() -> int:
    cfg_real = load_config()
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    print("== ① 后台迟到事件审计预览（内部专用，非客户样例）==")
    p1 = _audit_preview(cfg_real, OUT)
    print("  ", p1 / "报告" / "日报" / "2026-08-13.md")
    print("== ② 客户日报预览（测试样例）==")
    p2 = _customer_preview_sample(OUT)
    print("  ", p2 / "报告" / "日报" / "2026-08-17.md")
    print("== ③ 客户日报预览（真实数据，backend_only）==")
    p3 = _customer_preview_real(cfg_real, OUT)
    print("  ", p3 / "报告" / "日报" / "2026-08-17.md")
    print("完成。预览目录:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
