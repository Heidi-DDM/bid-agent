# 极性/枚举字段注册表（2026-09-11 P0 起步，P2 扩展为注册表；docs/10 §5 P2）
#
# 原则：**值 = f(原文摘录)**——抽取器（正则/模型）只负责命中原文片段，极性由本模块
# 的确定性规则从片段派生，禁止把"极性词出现"当作"值"。每个极性字段登记：
#   field_key / 展示标签 / 判定函数 matcher(text) -> (accepts, match) / 两极展示短语。
# matcher 判不出 → (None, None)，字段不产出或标 undetermined——不推断。
#
# 河北工大（二次）案例：「本项目（是/否）接受联合体投标： 0」里的"接受"是标签片段、
# "0"才是答案（政采表单 0=否 1=是）；农大案例：「（□接受/☑不接受）联合体投标」里的
# "□接受"是未勾选项。极性词只在三种位置算答案：标签冒号之后、勾选框之后、叙述句谓语
# （前面不是 是否/□/○/斜杠/右括号 等"选项或问句"语境）。
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

# ── 联合体 ──────────────────────────────────────────────────────────────
# 表单式：是否接受联合体投标：否 / 本项目（是/否）接受联合体投标：0（政采网表单 0=否 1=是）/
# 表格单元格换行后无冒号。答案「是」后不得紧跟「否」——否则它是下一个问句「是否专门面向
# 中小企业」的开头而非答案（extractor 去空白后「…投标是否专门…」与两列表头分离版式均落此例）；
# 数字答案后不得再跟数字（排除编号/金额）。
CONSORTIUM_FORM = re.compile(
    r"(?:是否|[(（]是[/／]?否[)）])\s*接受联合体投标\s*[：:]?\s*(是(?!否)|否|[01](?![0-9]))")
# 表单式（值为极性词）：联合体投标：不接受
CONSORTIUM_FORM_VALUE = re.compile(r"联合体投标\s*[：:]\s*(不接受|接受)")
# 勾选式：☑不接受）联合体投标 / ☑接受联合体投标（只认已勾选项）
CONSORTIUM_CHECK = re.compile(r"[☑√■●]\s*(不接受|接受)\s*[)）]?\s*联合体(?:投标)?")
# 叙述式：本次招标不接受联合体投标 / 本项目接受（允许）联合体投标
# 前一字符不得为 是否问句尾、未勾选框、斜杠、右括号（选项/问句语境，非答案）。
CONSORTIUM_NARR = re.compile(r"(?<![)）□○/／否])(不接受|不允许|接受|允许)\s*[)）]?\s*联合体(?:投标)?")
# 四种版式的并集（供 extractor 在去空白页文本上定位命中片段；极性仍由 consortium_match 派生）
CONSORTIUM_ANY = re.compile("|".join(
    p.pattern for p in (CONSORTIUM_FORM, CONSORTIUM_FORM_VALUE, CONSORTIUM_CHECK, CONSORTIUM_NARR)))

ACCEPT_LABEL = "接受联合体投标"
REJECT_LABEL = "不接受联合体投标"


def consortium_match(text: str) -> Tuple[Optional[bool], Optional[re.Match]]:
    """在 text 中判定是否接受联合体投标。

    返回 (accepts, match)：accepts=True/False；无法判定返回 (None, None)——不推断。
    优先级：表单式 > 表单值式 > 勾选式 > 叙述式（表单/勾选是显式答案，叙述式易受
    问句/选项片段干扰，故最后）。
    """
    m = CONSORTIUM_FORM.search(text)
    if m:
        return m.group(1) in ("是", "1"), m
    m = CONSORTIUM_FORM_VALUE.search(text)
    if m:
        return m.group(1) == "接受", m
    m = CONSORTIUM_CHECK.search(text)
    if m:
        return m.group(1) == "接受", m
    m = CONSORTIUM_NARR.search(text)
    if m:
        return not m.group(1).startswith("不"), m
    return None, None


# ── 是否专门面向中小企业（政采公告表单字段） ─────────────────────────────
SME_FORM = re.compile(r"是否专门面向中小企业\s*[：:]?\s*(是(?!否)|否|[01](?![0-9]))")
SME_NARR = re.compile(r"(?<![)）□○/／否])(不专门面向中小企业|专门面向中小企业|面向中小企业采购)")

SME_TRUE_LABEL = "专门面向中小企业"
SME_FALSE_LABEL = "非专门面向中小企业"


def sme_match(text: str) -> Tuple[Optional[bool], Optional[re.Match]]:
    """是否专门面向中小企业：表单式（是/否/0/1）优先，叙述式（不?专门面向中小企业）次之。"""
    m = SME_FORM.search(text)
    if m:
        return m.group(1) in ("是", "1"), m
    m = SME_NARR.search(text)
    if m:
        return not m.group(1).startswith("不"), m
    return None, None


# ── 是否允许分包（房建/市政招标公告与投标人须知前附表常见）────────────────
# 表单式：是否允许分包：否 / 分包：不允许；叙述式：本工程不允许分包 / 允许将非主体
# 非关键性工作分包。「不得」「禁止」归不允许；答案词前不得是问句尾/未勾选框。
SUBCONTRACT_FORM = re.compile(
    r"(?:是否|[(（]是[/／]?否[)）])\s*(?:允许|接受)\s*分包\s*[：:]?\s*(是(?!否)|否|[01](?![0-9]))"
    r"|分包\s*[：:]\s*(不允许|不接受|允许|接受)")
SUBCONTRACT_CHECK = re.compile(r"[☑√■●]\s*(不允许|允许|不接受|接受)\s*[)）]?\s*分包")
SUBCONTRACT_NARR = re.compile(
    r"(?<![)）□○/／否])(不允许|不接受|不得|禁止|允许|接受|可以)\s*(?:将[^。；，]{0,30}?)?(?:进行)?分包")

SUBCONTRACT_TRUE_LABEL = "允许分包"
SUBCONTRACT_FALSE_LABEL = "不允许分包"


def subcontract_match(text: str) -> Tuple[Optional[bool], Optional[re.Match]]:
    """是否允许分包：表单式 > 勾选式 > 叙述式；判不出 (None, None)。"""
    m = SUBCONTRACT_FORM.search(text)
    if m:
        ans = m.group(1) or m.group(2) or ""
        if ans in ("是", "1", "允许", "接受"):
            return True, m
        return False, m
    m = SUBCONTRACT_CHECK.search(text)
    if m:
        return not m.group(1).startswith("不"), m
    m = SUBCONTRACT_NARR.search(text)
    if m:
        return m.group(1) in ("允许", "接受", "可以"), m
    return None, None


# ── 是否接受进口产品（政采货物公告表单字段）────────────────────────────────
IMPORT_FORM = re.compile(
    r"(?:是否|[(（]是[/／]?否[)）])\s*(?:接受|允许)\s*进口产品\s*[：:]?\s*(是(?!否)|否|[01](?![0-9]))")
IMPORT_NARR = re.compile(r"(?<![)）□○/／否])(不接受|不允许|接受|允许)\s*进口产品")

IMPORT_TRUE_LABEL = "接受进口产品"
IMPORT_FALSE_LABEL = "不接受进口产品"


def import_product_match(text: str) -> Tuple[Optional[bool], Optional[re.Match]]:
    """是否接受进口产品：表单式（是/否/0/1）优先，叙述式次之。"""
    m = IMPORT_FORM.search(text)
    if m:
        return m.group(1) in ("是", "1"), m
    m = IMPORT_NARR.search(text)
    if m:
        return not m.group(1).startswith("不"), m
    return None, None


@dataclass(frozen=True)
class PolarityField:
    """极性字段登记项：值 = f(命中原文)，两极展示短语固定（P0 兼容口径，枚举入 enum）。"""

    field_key: str
    label: str
    matcher: Callable[[str], Tuple[Optional[bool], Optional[re.Match]]]
    true_label: str
    false_label: str

    def match(self, text: str):
        return self.matcher(text)

    def value_for(self, accepts: bool) -> str:
        return self.true_label if accepts else self.false_label


# 注册表（P2：新增极性字段在此登记；2026-09-14 锚点扩充批次补 分包 / 进口产品）
POLARITY_FIELDS: dict[str, PolarityField] = {
    "joint_venture": PolarityField(
        "joint_venture", "联合体", consortium_match, ACCEPT_LABEL, REJECT_LABEL),
    "sme_dedicated": PolarityField(
        "sme_dedicated", "面向中小企业", sme_match, SME_TRUE_LABEL, SME_FALSE_LABEL),
    "subcontract_allowed": PolarityField(
        "subcontract_allowed", "分包", subcontract_match,
        SUBCONTRACT_TRUE_LABEL, SUBCONTRACT_FALSE_LABEL),
    "import_allowed": PolarityField(
        "import_allowed", "进口产品", import_product_match,
        IMPORT_TRUE_LABEL, IMPORT_FALSE_LABEL),
}

# ── project_type 优先级分类器（标题/封面判类，修复首命中乱序与货物/服务被丢弃） ──
# 优先级：总承包/EPC > 施工 > 监理 > 设计 > 勘察（工程族）；
# 无工程词时按 采购/询比/磋商 语境给 货物/服务——两者都无则不产出（不推断）。
_PT_PRIORITY = (
    (re.compile(r"(?:工程总承包|施工总承包|总承包|EPC)"), "工程", "总承包招标"),
    (re.compile(r"施工图设计"), "工程", "设计招标"),   # 「施工图设计」是设计类，不被「施工」抢占
    (re.compile(r"施工"), "工程", "施工招标"),
    (re.compile(r"监理"), "工程", "监理招标"),
    (re.compile(r"设计"), "工程", "设计招标"),
    (re.compile(r"勘察"), "工程", "勘察招标"),
)


def classify_project_type(text: str) -> Optional[str]:
    """从标题/封面类短文本判定项目类型（纯词面优先级，非语义推断）。

    返回如 工程（施工招标）/工程（总承包招标）/货物（采购）/服务（采购）；判不出 None。
    「设计施工总承包（EPC）招标」按优先级取总承包，不再被首命中「设计」抢占。
    """
    text = text or ""
    for pat, family, word in _PT_PRIORITY:
        if pat.search(text):
            return f"{family}（{word}）"
    if re.search(r"采购|询比|磋商|谈判", text):
        if "货物" in text or "设备" in text:
            return "货物（采购）"
        if "服务" in text:
            return "服务（采购）"
    return None
