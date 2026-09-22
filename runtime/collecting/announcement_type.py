# R004/F004 扩展：公告类型分类 + 项目去重（纯函数，无网络/DB 依赖）
#
# 解决验收发现的两个根因：
#   1. 无公告类型识别（把《…暂行办法/…令》当招标机会；采购/中标公示混入候选）
#   2. 无跨项目去重（同一项目以"招标公告/中标候选人公示/中标结果公告"多形态重复）
#
# 口径（纯事实红线）：列表页只可确证「标题」。一切按确定性标题规则分类/归并，
# 不引入 LLM、不推断。**排除只针对"明确命中非招标/类型词"的标题**；对无法确证否的，
# 一律保守保留为招标（符合 AGENTS.md「不能确证为非标才不排除」——避免误删真标）。
from __future__ import annotations

import re

from runtime.collecting.registry import _normalize_keyword

# ── 公告类型（枚举常量） ────────────────────────────────
TENDER = "tender"        # 招标公告（含施工/EPC/设计/服务招标）
PREQUAL = "prequal"      # 资格预审公告（属招标）
WIN = "win"              # 中标结果公告 / 中标候选人公示 / 中标公告
PROCURE = "procure"      # 采购 / 询价 / 竞争性磋商 / 谈判 / 比选 / 竞价 / 单一来源
CHANGE = "change"        # 变更 / 更正 / 澄清 / 答疑 / 延期
LEASE = "lease"          # 招租 / 出租 / 资产处置 / 产权交易（ADR-006：非投标机会）
OTHER = "other"          # 规章 / 办法 / 令 / 通知 等非投标机会

TYPE_LABELS: dict[str, str] = {
    TENDER: "招标公告",
    PREQUAL: "资格预审",
    WIN: "中标/成交公示",
    PROCURE: "采购/磋商/询价",
    CHANGE: "变更公告",
    LEASE: "招租/租赁/资产处置",
    OTHER: "其他（非招标）",
}

# 默认只展示（= 真正的招标机会）
DEFAULT_INCLUDE_TYPES: frozenset[str] = frozenset({TENDER, PREQUAL})

# 同项目去重时类型优先级（越小越优先保留）
_TYPE_PRIORITY: dict[str, int] = {
    TENDER: 0, PREQUAL: 1, CHANGE: 2,
    PROCURE: 3, WIN: 4, LEASE: 5, OTHER: 6,
}

# ── 确定性规则词表 ─────────────────────────────────────
# ① 法规/规章/非标（《…暂行办法》 2026年第43号令 等）
_LEGAL_RE = re.compile(
    r"(暂行|办法|条例|实施办法|实施细则|征求意见|备案|令\s*\d*号|\d+\s*号令|"
    r"号令|\d+\s*号|规费结?算|招标人须知|资格标准)", re.I
)
# ② 中标类（F026/ADR-005：中标公示与成交公示高频形态补齐；采购结果/定标/评标结果一并排除）
_WIN_RE = re.compile(
    r"(中标结果|中标候选人|候选中标|候选人公示|中标公示|成交结果|成交候选人|成交公示|成交公告|"
    r"成交供应商|中标供应商|中标通知书|采购结果|采购成交|采购结果公示|中选结果|中选候选人|中选公告|"
    r"定标结果|评标结果|评标公示|招标结果|中标公告|结果公告|结果公示|中标侯选|资格后审结果)", re.I
)
# ③ 采购/磋商/谈判/比选/询价
_PROCURE_RE = re.compile(
    r"(采购公告|竞争性磋商|竞争性谈判|磋商|谈判|比选|竞价|单e渠道|单一来源|询价|"
    r"询比|邀请招标|框架协议)", re.I
)
# ④ 资格预审
_PREQUAL_RE = re.compile(r"(资格预审|资审|资格预审)", re.I)
# ⑤ 变更/更正/澄清/答疑/延期
_CHANGE_RE = re.compile(r"(变更公告|更正|澄清|答疑|更正通知|延期|补充公告|补遗)", re.I)
# ⑥ 招租/租赁/资产处置/产权交易（ADR-006：出租方找承租人的公告，不是投标机会）。
#    注意放在 PROCURE 之后判定：「设备租赁服务采购公告」先命中采购仍按采购处理；
#    词面避免裸「租赁」以免误伤「租赁服务公开招标」这类真招标。
_LEASE_RE = re.compile(
    r"(招租|竞租|出租|房屋租赁|场地租赁|店面租赁|车位租赁|商铺出租|摊位出租|门市出租|"
    r"资产处置|资产转让|资产出租|产权交易|产权转让|挂牌出让|国有产权)", re.I
)

# 待剥离的公告阶段/类型后缀（去重键内剥掉，避免"招标公告"(已丢) vs "中标…"被视为不同）
_STAGE_RE = re.compile(
    r"(中标结果公告|中标候选人公示|资格预审公告|资格预审|中标候选人|招标公告|采购公告|"
    r"中标公告|候选人公示|成交公告|结果公告|磋商公告|询价公告|资格预审资格预审)"
)
# 标段/代号归一：去掉「N标段 / 段」后缀，及任意字母+数字的代号/编号 token（YH-1 / 段G2002 等）。
# 只影响去重键，不改变标题展示字段；同项目异构编号 → 同一 key 而归并。
_LOT_RE = re.compile(
    r"[一二三四五六七八九十百\d]{1,5}[—\-]?[一二三四五六七八九十百\d]*标段|段号|标段|[A-Za-z]{1,10}[-—·]?\d{1,4}"
)


def classify_type(title: str | None) -> str:
    """确定性标题→公告类型。判定优先级：法规 > 中标 > 采购 > 资格预审 > 变更 > 招租。
    未命中任何排除词 → TENDER（不能确证非招标，保留）。"""
    t = _normalize_keyword(title or "")
    if not t:
        return OTHER
    if _LEGAL_RE.search(t):
        return OTHER
    if _WIN_RE.search(t):
        return WIN
    if _PROCURE_RE.search(t):
        return PROCURE
    if _PREQUAL_RE.search(t):
        return PREQUAL
    if _CHANGE_RE.search(t):
        return CHANGE
    if _LEASE_RE.search(t):
        return LEASE
    return TENDER


def is_default_include(t: str) -> bool:
    """是否默认展示（只真正的招标类型）。"""
    return t in DEFAULT_INCLUDE_TYPES


def classify(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """给每候选计算 announcement_type/type_label；返回 (招标保留, 排除清单)。

    items 中元素会被拷贝并补充 announcement_type / type_label 字段。
    """
    kept_excluded: tuple[list[dict], list[dict]] = [], []
    kept, excluded = [], []
    for it in items:
        t = classify_type(it.get("title"))
        it2 = dict(it)
        it2["announcement_type"] = t
        it2["type_label"] = TYPE_LABELS.get(t, t)
        (kept if is_default_include(t) else excluded).append(it2)
    return kept, excluded


# ── 项目键（去重用） ───────────────────────────────────
def project_key(title: str | None) -> str:
    """去重键：归一后去除【公告阶段词】和【标段/代号】。

    - 同项目「招标公告」与「中标候选人公示」→ 同 key（阶段归一）
    - 不同标段（YH-1 / YH-2）→ 同 key（标段不新建项目）
    """
    t = _normalize_keyword(title or "")
    t = _STAGE_RE.sub("", t)
    t = _LOT_RE.sub("", t)
    return _normalize_keyword(t)


def dedupe_by_project(items: list[dict]) -> list[dict]:
    """按 project_key 归并：同项目只留 1 条（类型优先级高的），
    补充 source_group / stage_labels / stage_count 供前端展示。"""
    groups: dict[str, list[dict]] = {}
    for it in items:
        key = project_key(it.get("title"))
        groups.setdefault(key, []).append(it)
    out: list[dict] = []
    for key, members in groups.items():
        members.sort(key=lambda m: _TYPE_PRIORITY.get(m.get("announcement_type"), 99))
        top = dict(members[0])
        top["dedup_key"] = key
        top["source_group"] = sorted({m.get("source_id") for m in members if m.get("source_id")})
        top["stage_count"] = len(members)
        top["stage_labels"] = sorted({
            m.get("type_label") or TYPE_LABELS.get(m.get("announcement_type"), "")
            for m in members if m.get("type_label")
        })
        out.append(top)
    out.sort(key=lambda m: _TYPE_PRIORITY.get(m.get("announcement_type"), 99))
    return out