#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""招投标 Pipeline 门禁检查器（G0-G6′）— 对应 schema §6.4/6.5。

用途：读取 schema/.pipeline_state.yml，逐门禁检查状态，输出下一步动作。
设计原则（复用方案 §三 复用清单 #6/#10）：
  - 门禁序列按投标流程定义：G0 采集 → G1 清洗 → G2 事实卡 → G3 情报库 → G4 推送 → G5 归档 → G6′ 校准
  - auto_action 内嵌于 schema，本脚本只读状态机 + 输出"该执行哪个门禁"，不越过 Agent 执行
  - 只输出结果，不打断流程（汇报但不询问）

用法：
  python3 scripts/pipeline_gates.py            # 检查全部门禁，输出待执行列表
  python3 scripts/pipeline_gates.py --json     # JSON 输出（供调度集成）
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "schema" / ".pipeline_state.yml"


def urgency_level(signup_deadline: str | None, bid_deadline: str | None = None) -> str:
    """时效优先级分级（schema §2.2.7）。

    基准 = 报名/文件获取截止时间 deadline_signup（非投标截止）：
      - P0 最高: 报名截止 ≤ 3 个工作日内（含当天）
      - P1 次高: 报名截止 ≤ 7 天
      - P2 常规: 报名窗口 > 7 天
      - 不予采纳: 报名已截止（或投标已截止）
    缺失 deadline_signup 时以 deadline_bid 前推 7 天估算。
    返回: P0 / P1 / P2 / REJECT（REJECT = 不予采纳，G0 丢弃）
    """
    today = date.today()

    def parse(d: str | None) -> date | None:
        if not d:
            return None
        d = d.strip()
        # 支持区间格式："2026-07-30 00:00 至 2026-08-04 23:59" → 取截止端
        if "至" in d:
            d = d.split("至")[-1].strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(d, fmt).date()
            except ValueError:
                continue
        return None

    s = parse(signup_deadline)
    b = parse(bid_deadline)
    if s is None:  # 缺失 → 估算（投标截止前 7 天）
        if b is None:
            return "P2"  # 均缺失 → P2 + 时效待核（schema §2.2.7 规则4）
        s = b - __import__("datetime").timedelta(days=7)

    if today > s:
        return "REJECT"   # 报名已截止 → 不予采纳
    if b and today > b:
        return "REJECT"   # 投标已截止 → 不予采纳

    days_left = (s - today).days
    if days_left <= 3:
        return "P0"
    if days_left <= 7:
        return "P1"
    return "P2"

# schema §6.4 门禁定义（与 schema.md / .pipeline_state.yml 保持一致）
GATES = [
    {
        "id": "G0",
        "name": "采集门禁",
        "state_key": "phase_b.step_01_collect",
        "rule": "来源 L0-L2、robots/频率合规、失败熔断",
        "auto_action": "跳过 L3 源 → 校验 robots 与频率 → 对失败源暂停并告警 → 重试 ≤2 次 → 验证通过后更新状态文件",
        "fallback": "持续失败 → 标记 source_suspended，等人工介入",
        "check": "raw/intake/ 下至少 1 条 intake 文件，source_level ∈ {L0,L1,L2}",
    },
    {
        "id": "G1",
        "name": "清洗门禁",
        "state_key": "phase_b.step_02_clean",
        "rule": "字段标准化、L1/L2 去重、缺字段标待补",
        "auto_action": "执行字段映射修复 → 执行 L1/L2 去重 → 补待补标记 → 验证 → 更新状态",
        "fallback": None,
        "check": "raw/_清洗报告.md 存在，重复项已合并",
    },
    {
        "id": "G2",
        "name": "事实卡门禁",
        "state_key": "phase_b.step_03_factcard",
        "rule": "必填字段齐全（card_id/project_id/project_name/tenderee/region/project_type/deadline_bid/source_links/confidence/status）、可溯源、纯事实红线零命中",
        "auto_action": "缺字段 → 回源补采 → 重新提取 → 验证 → 更新状态",
        "fallback": "原文 404 → 标记 source_dead，人工确认",
        "check": "raw/cards/ 下事实卡齐备，无分析断语（红线词扫描）",
    },
    {
        "id": "G3",
        "name": "情报库门禁",
        "state_key": "phase_b.step_04_intel_lib",
        "rule": "主卡+子卡齐备、状态流转正确、多源合并完成",
        "auto_action": "生成主卡 → 挂接子卡 → 多源引用合并 → 验证 → 更新状态",
        "fallback": None,
        "check": "情报库/ 下主卡（T-CARD-*.md）齐全，子卡回链",
    },
    {
        "id": "G4",
        "name": "推送门禁",
        "state_key": "phase_b.step_05_push",
        "rule": "匹配规则命中、个人信息已脱敏、链接浏览器级可达性复检通过（check_links_browser.py Chrome headless 实测，schema §6.4.2）、卡片格式合规（templates/推送卡片模板.md，无模板外段落、project_type 用枚举）",
        "auto_action": "重新校验链接（浏览器级）→ 附备选入口或标注「链接待人工验证」→ 补脱敏 → 重发推送 → 验证 → 更新状态",
        "fallback": None,
        "check": "推送日志/ 有记录，链接复检非 ❌ unreachable（⚠️ blocked 须附备选入口），联系人已打码",
    },
    {
        "id": "G5",
        "name": "归档门禁",
        "state_key": "phase_b.step_06_archive",
        "rule": "流标/中标/过期项目 → 状态 closed/awarded → 归档，保留审计",
        "auto_action": "状态流转 → 移入归档目录 → 写审计日志 → 验证 → 更新状态",
        "fallback": None,
        "check": "情报库/归档/ 存在，审计日志完整",
    },
    {
        "id": "G6′",
        "name": "校准门禁",
        "state_key": "phase_c.step_01_calibrate",
        "rule": "月度人工抽查 ≥100 条回标；阈值校准",
        "auto_action": "生成抽查清单 → 等待人工回标 → 计算召回率/误报率 → 调阈值 → 写月度质量报告",
        "fallback": None,
        "check": "质量报告/YYYY-MM_质量报告.md 存在",
    },
]


def load_state() -> dict:
    """读取 .pipeline_state.yml（缩进感知解析：支持嵌套 dict，不引入 yaml 依赖）。

    解析规则：用行首缩进构建嵌套结构；`key: value` 存标量；`key:` 开新层级。
    只保留 status/count 等标量字段与层级路径，供 gate_status 查询。
    """
    state: dict = {}
    # 用 (indent, name) 栈记录当前层级路径
    path_stack: list[tuple[int, str]] = []
    for raw in STATE_FILE.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # 弹出比当前缩进更深的层级
        while path_stack and indent <= path_stack[-1][0]:
            path_stack.pop()
        if val:  # 标量行：写入状态
            node = state
            for _, k in path_stack:
                node = node.setdefault(k, {})
            node[key] = val
        else:  # 新层级
            path_stack.append((indent, key))
            node = state
            for _, k in path_stack[:-1]:
                node = node.setdefault(k, {})
            node.setdefault(key, {})
    return state


def gate_status(state: dict, gate: dict) -> str:
    """返回门禁状态：MISSING（未执行）/ DONE（已通过）。"""
    key = gate["state_key"]
    # state_key 形如 phase_b.step_01_collect，逐级取
    parts = key.split(".")
    node = state
    for p in parts:
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return "MISSING"
    return node.get("status", "MISSING")


def intake_gate_status(intake_id: str) -> dict:
    """单条 intake 的门禁链状态（C 方案，2026-08-17）。

    检查：intake 存在 → 事实卡（G2）→ 主卡（G3）→ 事件卡（sync_events）。
    返回 {intake_id, intake_exists, pipeline_status, fact_cards, main_card, event, gates:{...}}。
    """
    import glob
    import re

    intake_dir = ROOT / "raw" / "intake"
    intake_path = intake_dir / f"{intake_id}.md"
    out = {"intake_id": intake_id, "intake_exists": intake_path.exists(),
           "pipeline_status": None, "fact_cards": [], "main_card": None,
           "event": None, "gates": {}}

    # intake 的 pipeline_status（raw/carded/intel/synced）
    if intake_path.exists():
        text = intake_path.read_text(encoding="utf-8")
        m = re.search(r"^pipeline_status:\s*(.+)$", text, re.M)
        out["pipeline_status"] = m.group(1).strip() if m else "raw"
        m_conf = re.search(r"^confidence:\s*(.+)$", text, re.M)
        out["confidence"] = m_conf.group(1).strip() if m_conf else None

    project_id = f"T-PROJ-{intake_id.replace('zb-', '')}"
    # G2 事实卡
    cards_dir = ROOT / "raw" / "cards"
    if cards_dir.exists():
        out["fact_cards"] = sorted(str(p.name) for p in cards_dir.glob(f"{intake_id}-F*.md"))
    # G3 主卡
    intel_dir = ROOT / "情报库"
    for d in (intel_dir, intel_dir / "归档"):
        if not d.exists():
            continue
        for p in d.glob("T-CARD-*.md"):
            fm = _read_fm(p)
            if fm.get("project_id") == project_id or fm.get("card_id") == project_id:
                out["main_card"] = str(p.relative_to(ROOT))
                break
        if out["main_card"]:
            break
    # 事件卡
    events_dir = ROOT / "data" / "announcement_events"
    if events_dir.exists():
        for p in sorted(events_dir.glob("*.md")):
            fm = _read_fm(p)
            if fm.get("project_id") == project_id:
                out["event"] = str(p.relative_to(ROOT))
                break

    out["gates"] = {
        "G0 采集": "✅" if out["intake_exists"] else "❌",
        "G2 事实卡": f"✅ {len(out['fact_cards'])} 卡" if out["fact_cards"] else "❌",
        "G3 主卡": "✅" if out["main_card"] else "❌",
        "事件同步": "✅" if out["event"] else "❌",
    }
    return out


def _read_fm(path: Path) -> dict:
    import yaml
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def main() -> int:
    if not STATE_FILE.exists():
        print("❌ 状态文件缺失：schema/.pipeline_state.yml 不存在（先运行 ⓪.1.5 初始化）")
        return 1

    state = load_state()
    pending = []
    for gate in GATES:
        st = gate_status(state, gate)
        if st == "MISSING":
            pending.append(gate)
        else:
            print(f"✅ {gate['id']} {gate['name']}：已通过")

    if not pending:
        print("🎉 全部门禁通过，流水线就绪。")
        return 0

    print(f"\n🔧 待执行门禁（{len(pending)} 道，按序）：")
    for gate in pending:
        print(f"  - {gate['id']} {gate['name']}")
        print(f"    rule: {gate['rule']}")
        print(f"    auto_action: {gate['auto_action']}")
        if gate["fallback"]:
            print(f"    fallback: {gate['fallback']}")
        print(f"    check: {gate['check']}")

    print(f"\n下次任务从 {pending[0]['id']} 开始执行（schema §6.5 自动执行算法）。")
    return 0


if __name__ == "__main__":
    if "--intake" in sys.argv:
        # 单条 intake 门禁链检查：python3 pipeline_gates.py --intake zb-20260817-001
        idx = sys.argv.index("--intake")
        if idx + 1 < len(sys.argv):
            intake_id = sys.argv[idx + 1]
            r = intake_gate_status(intake_id)
            print(f"=== intake 门禁链：{intake_id} ===")
            print(f"intake 文件：{'✅' if r['intake_exists'] else '❌'} ｜ "
                  f"pipeline_status={r['pipeline_status']} ｜ confidence={r['confidence']}")
            for g, st in r["gates"].items():
                print(f"  {g}：{st}")
            print(f"事实卡：{r['fact_cards'] or '无'}")
            print(f"主卡：{r['main_card'] or '无'}")
            print(f"事件卡：{r['event'] or '无'}")
            blocked = [g for g, st in r["gates"].items() if st.startswith("❌")]
            if blocked:
                print(f"⚠️ 未过门禁：{', '.join(blocked)} —— sync_events 将拒绝同步该 intake（--force 可绕过）")
            else:
                print("✅ 门禁链完整，可同步。")
            sys.exit(0)
        print("用法: python3 pipeline_gates.py --intake <intake_id>（如 zb-20260817-001）")
        sys.exit(2)
    elif "--json" in sys.argv:
        state = load_state()
        out = {
            "checked_date": date.today().isoformat(),
            "gates": [
                {"id": g["id"], "name": g["name"], "status": gate_status(state, g)}
                for g in GATES
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif "--urgency" in sys.argv:
        # 时效优先级批量判定：读 raw/intake/*.md 的 YAML 头
        import glob
        import re

        intake_dir = ROOT / "raw" / "intake"
        print(f"=== 时效优先级判定（schema §2.2.7，基准日 {date.today()}）===")
        for f in sorted(glob.glob(str(intake_dir / "*.md"))):
            text = open(f, encoding="utf-8").read()
            m_signup = re.search(r"^deadline_signup:\s*(.+)$", text, re.M)
            m_bid = re.search(r"^deadline_bid:\s*(.+)$", text, re.M)
            signup = m_signup.group(1).strip() if m_signup else None
            bid = m_bid.group(1).strip() if m_bid else None
            lvl = urgency_level(signup, bid)
            title = re.search(r"^title:\s*(.+)$", text, re.M)
            name = title.group(1).strip() if title else Path(f).name
            flag = {"P0": "⏰ 报名即将截止", "P1": "📅 报名窗口有限", "P2": "常规商机", "REJECT": "✗ 不予采纳（已截止）"}[lvl]
            print(f"  [{lvl}] {flag} | {name}")
    else:
        sys.exit(main())
