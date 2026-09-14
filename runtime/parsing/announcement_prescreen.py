# R004/F004 扩展：公告正文初筛结构化抽取（纯确定性锚点，不调 LLM、不推断）
#
# 目的：候选卡 / 项目初筛视图做展示时，从**公告正文**（import_candidate_detail 已抓取
# 并经 html_to_text 固化净化文本）确定性提取投标专员第一眼要看的关键字段 ——
# 资质要求、金额、人员要求、地区、项目类型、工期、标段、联合体、质量、报名/投标
# 截止 —— 而非把正文里已写明的内容一律标"待详情确认"。字段全部带 assertion(条款级
# 原文摘录) + confidence，可回链原文，纯事实红线不变（AGENTS.md 规则 1/2）。
#
# 2026-09-10 输出质量重构（用户验收：断句截半句、字段值不完整）：
#   - assertion 由「正则命中点前后硬切 16 字」改为**条款级摘录**：起点回扩到句读后
#     （句读后紧跟的条款编号如 2.6/(1)/3.1.1 属本条款），终点扩到句读（。；）或
#     下一个条款编号，超长截断加省略号——不再出现「程地点：…质量要求及采」式断句；
#   - 质量要求/资质要求等长字段值取**完整谓语到句界**（如「合格，且需符合国家及
#     行业质量要求及采购人指定标准」），不再是首个命中词；
#   - 新增锚点：采购人/招标人（purchaser）、业绩要求（performance）、建设规模
#     （scale，回填候选卡「规模」）。
#
# 2026-09-11 P0（河北工大（二次）案例，docs/10 §1-§2）：
#   - 资质/业绩取值与摘录按**条款组**收尾（_group_end）：条款内"；"是并列子条件分隔符
#     （施工资质A或B；安全生产许可证），不再当句界切断；"；"后仅紧跟下一条款编号或
#     下一个 label 才收组；
#   - 句界/摘录终点增加「下一个 label 起点」（_RE_LABEL）：表单式公告字段间无句读，
#     值与摘录不再横跨相邻字段（采购人：X 建设地点：Y → 采购人=X）；
#   - 联合体极性改由 runtime/parsing/polarity 从命中原文派生（表单>勾选>叙述），
#     「是否接受联合体投标：否」不再被标签片段"接受"反转。
#
# 产出： {field_key: PrescreenField}。region/project_type/budget_amount/ceiling_price/
#   deadline_* 对齐 F005 §4.1 主卡 key；资质/工期/质量/标段/联合体/项目经理/安全员等
#   为初筛补充字段（不进入主卡契约，只做初筛视图）。
#
# 2026-09-14 锚点扩充批次（用户决策：按招标行业经验覆盖全部可用锚点）：
#   - 商务标/保证金：投标保证金、履约保证金、预付款、质保金、投标有效期、保修期、暂列金额、
#     下浮率、文件售价、安全文明施工费；
#   - 评标：评标办法、价格/技术/商务分值；合同：合同类型/计价方式、付款方式；
#   - 硬性资格：资格审查方式、安全生产许可证、类似业绩、无重大违法记录、信用黑名单、
#     技术负责人、项目经理业绩、社保；极性：是否允许分包、是否接受进口产品（polarity 注册表）；
#   - 标签式：财务要求、技术标准、招标范围、开标/递交地点、代理机构、资金来源、批准文号；
#   - 时间：文件获取截止、质疑/答疑截止、踏勘时间。
#   新字段全部**不进必查集**（不增待补率），未命中即不产出；命中必带 quote/偏移。
from __future__ import annotations

import re

from runtime.parsing.polarity import POLARITY_FIELDS, classify_project_type

CONF_CONFIRMED = "confirmed"
CONF_HIGH = "high"
CONF_LOW = "low"


class PrescreenField:
    """初筛字段（带溯源 assertion + confidence）。

    P1（2026-09-11，docs/10 §5 P1）溯源升级：quote/start/end 为**固化文本**上的逐字摘录
    与字符偏移（round-trip 保证 text[start:end] == quote，由构造成立、入库前复核）；
    enum 为极性字段的机器可校验枚举（True/False，非极性字段 None）；
    truncated=True 表示摘录超长被按 max_len 截断（G3：截断必须显式标记）。
    """

    __slots__ = ("field_key", "value", "assertion", "clause", "confidence", "missing",
                 "quote", "start", "end", "enum", "truncated", "review")

    def __init__(self, field_key, value, assertion="", clause="公告原文",
                 confidence=CONF_HIGH, missing=False, *, quote=None, start=None,
                 end=None, enum=None, truncated=False, review=None):
        self.field_key = field_key
        self.value = value
        self.assertion = assertion or ""
        self.clause = clause or "公告原文"
        self.confidence = confidence
        self.missing = missing
        self.quote = quote
        self.start = start
        self.end = end
        self.enum = enum
        self.truncated = truncated
        self.review = review

    def to_dict(self):
        return {
            "field_key": self.field_key,
            "value": self.value,
            "assertion": self.assertion,
            "clause": self.clause,
            "confidence": self.confidence,
            "missing": self.missing,
            "quote": self.quote,
            "start": self.start,
            "end": self.end,
            "enum": self.enum,
            "truncated": self.truncated,
            "review": self.review,
        }


_WS = re.compile(r"\s+")


def _clean_text(text):
    return _WS.sub(" ", (text or "")).strip()


def _collapse_with_map(text):
    """把固化原文折叠为抽取工作面（空白串→单空格、去首尾空白），同时给出位置映射。

    P1：offsets[i] = 折叠文本第 i 个字符在**固化原文**中的位置（空白串映射到串首字符）。
    抽取仍以折叠文本为工作面（避免全锚点语义回归），quote/start/end 经映射换算回固化
    原文偏移——「值与偏移同源」，round-trip（原文[start:end]==quote）由构造成立。
    """
    out: list[str] = []
    offsets: list[int] = []
    i, n = 0, len(text)
    while i < n and text[i].isspace():
        i += 1
    end = n
    while end > i and text[end - 1].isspace():
        end -= 1
    while i < end:
        ch = text[i]
        if ch.isspace():
            j = i
            while j < end and text[j].isspace():
                j += 1
            out.append(" ")
            offsets.append(i)
            i = j
        else:
            out.append(ch)
            offsets.append(i)
            i += 1
    return "".join(out), offsets


def _trim_span(body, s, e):
    """把 [s,e) 两端收缩到非空白字符上——摘录 = body[s:e] 原样，无需二次清洗。"""
    while s < e and body[s] in _BLANKS:
        s += 1
    while e > s and body[e - 1] in _BLANKS:
        e -= 1
    return s, e


# ── 条款边界（断句按阅读习惯，2026-09-10） ─────────────────────────────
# 条款编号形态：2.6 / 3.1.1 / (1) / （二） / 一、 / 「3. 投标人资格要求」式单级编号（点后紧跟汉字，
# 排除「3.5万元」「2026.10.10日」等数字续接）
_RE_CLAUSE_NUM = re.compile(
    r"[0-9]{1,3}(?:\.[0-9]{1,3})+|[（(][一二三四五六七八九十0-9]{1,3}[)）]|[一二三四五六七八九十]+、"
    r"|(?<![0-9.])[0-9]{1,2}\.(?=\s*[\u4e00-\u9fff])")
_SENT_END_CHARS = "。；;"
# label 形态：2-10 个汉字 + 冒号（采购人：/ 建设地点：/ 是否接受联合体投标：）。
# 表单式公告字段间没有句读，值与摘录的终点靠下一个 label 起点定界。
_RE_LABEL = re.compile(r"[\u4e00-\u9fff]{2,10}\s*[：:]")
# 已知 label 词表（无冒号版式：表格单元格净化后「label 值」仅以空格分隔）。长词在前。
# 后随边界必须是冒号/空白/结尾——「行业质量要求及采购人的要求」的「质量要求」后是
# 「及」，属叙述词内命中，不算字段头（2026-09-11 河北工大表格版式）。
_KNOWN_LABEL_WORDS = (
    r"[(（]是[/／]?否[)）]\s*接受联合体投标|是否接受联合体投标|是否允许联合体投标|是否专门面向中小企业|"
    r"是否允许分包|是否接受进口产品|"
    r"合同履行期限|服务期限|"
    r"项目名称|项目编号|采购需求|采购预算金额|采购预算|采购人|招标人|代理机构|建设地点|建设规模|项目规模|"
    r"计划工期|资质要求|业绩要求|项目经理资格要求|项目经理|质量标准|质量要求|投标截止时间|"
    r"开标时间|投标保证金|最高投标限价|最高限价|预算金额|联系人|联系方式|地址|电话|"
    # 2026-09-14 锚点扩充：合同/商务/评标/资格类表单 label（复合词，避免叙述内误触发）
    r"投标有效期|履约保证金|履约担保|付款方式|支付方式|开标地点|递交地点|评标办法|评标方法|"
    r"资格审查方式|招标范围|采购内容|建设内容|招标内容|资金来源|合同类型|计价方式|暂列金额|"
    r"招标文件售价|财务要求|批准文号|项目概况|技术标准|技术要求|保修期|质保期|缺陷责任期|"
    r"预付款|工程预付款|质量保证金|质保金|"
    r"招标文件获取|文件获取时间|踏勘时间|答疑时间")
_RE_KNOWN_LABEL = re.compile(r"(?:%s)(?=[：:]|\s|$)" % _KNOWN_LABEL_WORDS)
# 「label+冒号?+值+空格」整体（仅空格分隔的表单版式）：其终点作为条款起点候选。
# 值上限 40 字（容得下项目名等长值）；因要求后随空白，纯叙述中文（无空格）不会误触发。
_RE_LABEL_UNIT = re.compile(
    r"(?:%s)[：:]?[ \u3000]?[^\s：:，。；]{1,40}(?=[ \u3000]|$)" % _KNOWN_LABEL_WORDS)
_BLANKS = " \u3000\n\r\t"


def _next_field_start(text, pos, upto):
    """[pos, upto) 内下一个字段（label）起点：泛型「汉字串+冒号」或已知 label 词。无则 None。"""
    best = None
    for pat in (_RE_LABEL, _RE_KNOWN_LABEL):
        m = pat.search(text, pos, upto)
        if m is not None and m.start() > pos and (best is None or m.start() < best):
            best = m.start()
    return best


def _is_time_colon(text, i):
    """「9:00」「17：30」里的冒号是时刻分隔符，不是 label 冒号/句读。"""
    return 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()


def _sent_start(text, pos, back=100):
    """match 起点向前扩到本条款开头：最近句读/换行/标签冒号之后（其后紧跟的编号属本条款），
    或最近「已知label+短值」单元之后（空格分隔的表单版式）。"""
    lo = max(0, pos - back)
    stops = [i for i in (text.rfind("。", lo, pos), text.rfind("；", lo, pos),
                         text.rfind(";", lo, pos), text.rfind("\n", lo, pos)) if i >= 0]
    for ch in ("：", ":"):
        i = text.rfind(ch, lo, pos)
        while i >= 0 and _is_time_colon(text, i):
            i = text.rfind(ch, lo, i)
        if i >= 0:
            stops.append(i)
    for m in _RE_LABEL_UNIT.finditer(text, lo, pos):
        # 单元必须由真实空白收尾（m.end() < pos）；finditer 的 endpos 会让 `$` 在 pos 处
        # 人工成立，把「项目经理连续缴纳」这类叙述误判为 label 单元并吞掉摘录起点
        if m.end() < pos:
            stops.append(m.end())
    return (max(stops) + 1) if stops else lo


def _sent_end(text, pos, fwd=240):
    """从 pos 向后到句读（含）、下一个条款编号起点或下一个字段（label）起点；
    均不在窗口内则按窗口截断。"""
    best = pos + fwd
    for ch in _SENT_END_CHARS:
        i = text.find(ch, pos, pos + fwd)
        if i >= 0:
            best = min(best, i + 1)
    m = _RE_CLAUSE_NUM.search(text, pos, pos + fwd)
    if m is not None and m.start() > pos:
        best = min(best, m.start())
    fs = _next_field_start(text, pos, pos + fwd)
    if fs is not None:
        best = min(best, fs)
    return best


def _clause_snip(text, m, max_len=220, end=None):
    """条款级原文摘录：[本条款开头, 句读]。返回 (摘录, s, e, truncated)，摘录 == text[s:e] 原样。

    end 由调用方给定时（资质/业绩条款组）摘录终点跟随取值终点，保证摘录覆盖值的全部子项。
    超长按 max_len 截断并显式标记 truncated（不再追加省略号——摘录必须是原文逐字子串）。"""
    s, e = m.start(), m.end()
    start = _sent_start(text, s)
    end = _sent_end(text, e) if end is None else end
    start, end = _trim_span(text, max(0, min(start, len(text))), max(0, min(end, len(text))))
    truncated = False
    if end - start > max_len:
        end = start + max_len
        start, end = _trim_span(text, start, end)
        truncated = True
    return text[start:end], start, end, truncated


def _clause_tail(text, m, limit=300):
    """标签式字段的值：从 match 结束取到句读/换行（不含句读），去首尾空白与标点。"""
    pos = m.end()
    end = _sent_end(text, pos, fwd=limit)
    tail = text[pos:end].rstrip("。；;，, ")
    return _clean_text(tail)[:limit]


def _clause_num_shape(s):
    """编号形态归类：paren=（1）/(1)、dotted=3.1.1 / 3.、cn=一、——列表续接仅认同形态。"""
    if "（" in s or "(" in s:
        return "paren"
    if re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3})+|[0-9]{1,2}\.", (s or "").strip()):
        return "dotted"
    return "cn"


def _skip_blanks(text, i, upto):
    while i < upto and text[i] in _BLANKS:
        i += 1
    return i


def _group_end(text, pos, fwd=400, shape=None):
    """条款组终点（资质/业绩等多子项条款取值与摘录共用）。

    组内"；"是并列子条件分隔符，不在此切断（河北工大：施工资质A或B；安全生产许可证）。
    收组条件：
      - "。"/"；"之后紧跟 label（可隔一个编号）→ 下一条款，收组；
      - 之后紧跟条款编号：同形态 → 列表续接；paren 形态跟在无编号组后 → 子项续接
        （…；（2）具有…）；其他形态（如 3.1.2）→ 新条款，收组；
      - 之后是普通文字："。"→ 收组，"；"→ 续接；
      - 段内（非续接位）出现 label 或条款编号 → 在其起点收组（表单式公告无句读）；
      - 均无 → 窗口截断。
    """
    upto = min(len(text), pos + fwd)
    scan = pos
    while scan < upto:
        cuts = [i for i in (text.find("。", scan, upto), text.find("；", scan, upto),
                            text.find(";", scan, upto)) if i >= 0]
        cut = min(cuts) if cuts else -1
        seg_stop = cut if cut >= 0 else upto
        fs = _next_field_start(text, scan, seg_stop)
        if fs is not None:
            return fs
        nm = _RE_CLAUSE_NUM.search(text, scan, seg_stop)
        if nm is not None and nm.start() > scan:
            return nm.start()
        if cut < 0:
            return upto
        j = _skip_blanks(text, cut + 1, upto)
        if j >= upto:
            return cut + 1
        nxt = _RE_CLAUSE_NUM.match(text, j)
        k = _skip_blanks(text, nxt.end() if nxt else j, upto)
        if _RE_LABEL.match(text, k) or _RE_KNOWN_LABEL.match(text, k):
            return cut + 1
        if nxt is not None:
            ns = _clause_num_shape(nxt.group(0))
            if ns == shape or (shape is None and ns == "paren" and text[cut] != "。"):
                scan = nxt.end()
                continue
            return cut + 1
        if text[cut] == "。":
            return cut + 1
        scan = j
    return upto


def _list_aware_tail(text, m, limit=380):
    """列表式条款取值（资质/业绩要求常见）：标签后紧跟 (1)/一、 编号列表时整组取值——
    不截断在首项、不截断在"；"并列子项，也不吞并下一条款（如 3.1.1 资质列表后的
    3.1.2 项目经理要求）。返回 (值, 组终点)，组终点供摘录对齐。"""
    pos = _skip_blanks(text, m.end(), len(text))
    first = _RE_CLAUSE_NUM.match(text, pos)
    shape = _clause_num_shape(first.group(0)) if first else None
    end = _group_end(text, first.end() if first else pos, fwd=limit, shape=shape)
    tail = text[pos:end].rstrip("。；;，, ")
    return _clean_text(tail)[:limit], end


# ── 初筛锚点（确定性规则，非 LLM） ────────────────────────────────
def _re(p):
    return re.compile(p)


# 精确取值锚点（regex 分组即完整值，不追加条款尾）
# 2026-09-11 P3 快赢批次（docs/10 附录 A prescreen #6/#7/#9/#11/#12）：金额/工期/地区/
# 标段数/建造师等级按政采与房建两类实测措辞放宽——冒号可选、「为/约」插语、亿元、
# 「市」结尾（后不跟区县时）、「特级」、无「及以上」、划分多一个「为」字。
_ANCHORS = {
    "region":    _re(r"建设地点[:：]\s*([\u4e00-\u9fff0-9]{2,40}?(?:[区县镇]|市(?=[^\u4e00-\u9fff0-9]|$)))"),
    "budget_amount":  _re(r"(?:总投资|预算金额|采购预算|项目预算)(?:（元）|\(元\))?\s*[:：为]?\s*(?:约)?\s*([0-9][0-9，,.\s]{0,10}[0-9]?\s*(?:万元|亿元|万|元)?)"),
    "ceiling_price":  _re(r"(?:最高投标限价|最高限价|招标控制价)\s*[:：]?\s*(?:为)?\s*([0-9][0-9，,.\s]{0,10}[0-9]?\s*(?:万元|亿元|万|元)?)"),
    "equip_amount":   _re(r"设备购置费最高限价(?:为)?\s*[:：]?\s*([0-9][0-9\s，,.]{0,10}[0-9]?\s*万元)"),
    "work_amount":    _re(r"工程费最高限价(?:为)?\s*[:：]?\s*([0-9][0-9\s，,.]{0,10}[0-9]?\s*万元)"),
    "duration":   _re(r"(?:计划工期|工期)\s*[:：]?\s*(?:为|约)?\s*([0-9]+\s*(?:日历天|天|个月))"),
    "section_count":  _re(r"共?计划?划分(?:为)?\s*([一二三四五六七八九十0-9]+)\s*个标段"),
    # 项目经理：等级兼容大写数字（贰级）与「特级」，允许「项目经理资格要求：须具备…」的间隔，
    # 「及以上」可选
    "pm_registered": _re(r"(?:拟派)?项目经理[^；。\n]{0,60}?[一二三壹贰叁特]级(?:及以上)?注册建造师[^；。，\n]{0,16}"),
    "pm_b_cert": _re(r"[＋\+☑□＝]?同时(?:具有|取得)[^。；]{0,10}安全生产考核合格证书(?:[（(][A-Z]类?[)）])?"),
    "safety_officer": _re(r"安全生产管理人员[^。]{0,70}(?:配备人数|配备)[\s:：]*([0-9一二三四五六七八九十])\s*个(?!人)"),
    # 「未列入…」与「未被(法院/平台…)列入失信被执行人名单」两种写法
    "credit_ok": _re(r"未(?:被[^。；]{0,60}?)?列入失信被执行人名单"),
}

# 金额片段（阿拉伯数字 + 单位 / 大写金额）；供保证金/担保/暂列金等复用
_AMOUNT = r"(?:[0-9][0-9，,.\s]{0,12}[0-9]?\s*(?:万元|亿元|万|元)(?:整)?|(?:人民币)?[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{2,14}元(?:整)?)"
_PERCENT = r"[0-9]{1,2}(?:\.[0-9]{1,2})?\s*[%％]"
_DATE = r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"

# ── 2026-09-14 锚点扩充批次（按招标行业经验覆盖：硬性资格 / 评标评分 / 技术 / 合同 / 商务标）
# 仍为确定性锚点、不推断；新增字段一律**不进 _HARD_REQUIRED**（docs/10 §7.5 护栏：待补率不得
# 高于改造前基线——新增只增覆盖，不增待补）。多备选分支各带一个捕获组，取值取首个非空组。
_ANCHORS.update({
    # ── 商务标 / 保证金 / 担保 ──────────────────────────────────────
    "bid_bond":         _re(r"投标保证金(?:金额|数额)?\s*[:：]?\s*(?:为|人民币|金额为|数额为)?\s*[:：]?\s*[¥￥]?\s*(" + _AMOUNT + ")"),
    "performance_bond": _re(r"履约(?:保证金|担保|保函)(?:金额|数额|比例)?[^。；]{0,24}?(?:为|按|不(?:得)?超过|不(?:得)?高于)?\s*(?:合同(?:总)?(?:价|金额|价款)的?)?\s*(" + _PERCENT + "|" + _AMOUNT + ")"),
    "advance_payment":  _re(r"(?:工程|合同)?预付款[^。；]{0,30}?(" + _PERCENT + "|" + _AMOUNT + ")"),
    "retention_money":  _re(r"(?:质量保证金|质保金|保修金|工程质量保修金)[^。；]{0,30}?(" + _PERCENT + "|" + _AMOUNT + ")"),
    "bid_validity":     _re(r"投标有效期\s*[:：]?\s*(?:为|自[^。；]{0,30}?起)?\s*([0-9]{2,3}\s*(?:日历天|天|日))"),
    "warranty":         _re(r"(?:工程)?(?:保修期|质保期|质量保修期|缺陷责任期|保修期限)\s*[:：]?\s*(?:为|自[^。；]{0,30}?起)?\s*([0-9]{1,3}\s*(?:个月|年|日历天|天))"),
    "provisional_sum":  _re(r"暂列金额\s*[:：]?\s*(?:为)?\s*(" + _AMOUNT + ")"),
    "downward_rate":    _re(r"下浮率\s*[:：]?\s*(?:为|不(?:得)?低于|不(?:得)?高于|不(?:得)?超过|按)?\s*(" + _PERCENT + ")"),
    "file_price":       _re(r"(?:招标|采购)?文件(?:每套)?(?:售价|工本费|费用)\s*[:：]?\s*(?:为|人民币|每套)?\s*[¥￥]?\s*([0-9][0-9，,.]{0,8}\s*元(?:/套)?)"),
    "safety_fee":       _re(r"安全(?:文明)?施工(?:措施)?费[^。；]{0,30}?(不(?:得)?(?:参与|作为|列入)?竞争(?:性)?(?:费用|报价)?|" + _AMOUNT + "|" + _PERCENT + ")"),
    # ── 评标评分项 ────────────────────────────────────────────────
    "evaluation_method": _re(
        r"(?:评标|评审|评定|评选|评分)(?:办法|方法|方式)\s*[:：]?\s*(?:本项目)?(?:采用|为|拟采用)?\s*"
        r"((?:经评审的)?(?:最低投标价法|最低价法|综合评估法|综合评分法|合理低价法|综合评审法|性价比法|最低评标价法|综合评价法|定性评审法?|随机抽取法))"
        r"|(?:采用|实行|使用)\s*((?:经评审的)?(?:最低投标价法|综合评估法|综合评分法|合理低价法|综合评审法|最低评标价法|综合评价法))"),
    "price_score":      _re(r"(?:价格|报价|投标报价)(?:分|部分|得分|评分|分值)\s*[:：（(]?\s*(?:满分|权重|占|为|共|计)?\s*[:：]?\s*(?:为)?\s*([0-9]{1,3}\s*(?:分|[%％]))"
                            r"|投标报价\s*[（(]\s*([0-9]{1,3}\s*分)\s*[)）]"),
    "tech_score":       _re(r"(?:技术|技术标|技术部分|施工组织设计)(?:分|得分|评分|分值)\s*[:：（(]?\s*(?:满分|权重|占|为|共|计)?\s*[:：]?\s*(?:为)?\s*([0-9]{1,3}\s*(?:分|[%％]))"
                            r"|技术(?:标|部分)?\s*[（(]\s*([0-9]{1,3}\s*分)\s*[)）]"),
    "business_score":   _re(r"(?:商务|商务标|商务部分|信用)(?:分|得分|评分|分值)\s*[:：（(]?\s*(?:满分|权重|占|为|共|计)?\s*[:：]?\s*(?:为)?\s*([0-9]{1,3}\s*(?:分|[%％]))"
                            r"|商务(?:标|部分)?\s*[（(]\s*([0-9]{1,3}\s*分)\s*[)）]"),
    # ── 合同条款 ──────────────────────────────────────────────────
    "contract_type":    _re(
        r"(?:合同(?:类型|形式|计价(?:方式|模式)?)|计价(?:方式|模式|形式)|承包方式)\s*[:：]?\s*(?:采用|为|本项目采用|实行)?\s*"
        r"((?:固定|可调)?(?:总价|单价|成本加酬金)(?:合同|包干|承包)?)"
        r"|(?:采用|实行)\s*((?:固定|可调)(?:总价|单价)(?:合同|包干))"),
    # ── 硬性资格要求 ──────────────────────────────────────────────
    "prequalification": _re(r"资格审查(?:方式|方法)?\s*[:：]?\s*(?:采用|为)?\s*(资格后审|资格预审|后审|预审)"
                            r"|(?:采用|实行)\s*(资格后审|资格预审)"),
    "safety_license":   _re(r"(?:有效的|有效期内的|合法有效的)?(?:企业)?安全生产许可证(?![书])"),
    "similar_performance": _re(r"(?:近|自)\s*[0-9一二三四五]\s*年(?:内|以来|至今)?[^。；]{0,60}?(?:完成|承建|承担|竣工)[^。；]{0,60}?类似[^。；，]{0,40}?(?:[\u4e00-\u9fff]{0,8}?业绩|工程|项目|经验)"),
    "no_violation":     _re(r"(?:近|前|参加[^。；]{0,12}?活动前)\s*[0-9一二三]\s*年(?:内)?[^。；]{0,30}?(?:无|没有)(?:重大)?(?:违法|违规|违约)(?:记录|行为)?"),
    "credit_blacklist": _re(r"重大税收违法(?:失信)?(?:案件)?(?:当事人|主体)?名单|政府采购严重违法失信行为(?:记录)?名单|(?:建筑市场监管公共服务平台|信用中国)[^。；]{0,24}?(?:黑名单|失信|不良)"),
    "tech_lead":        _re(r"技术负责人[^；。\n]{0,60}?(?:高级|中级|初级)?(?:工程师|职称|技术职务)[^；。，\n]{0,24}"),
    "pm_similar":       _re(r"项目经理[^；。\n]{0,40}?(?:担任|主持|负责|完成)[^；。\n]{0,40}?类似[^；。，\n]{0,30}?(?:[\u4e00-\u9fff]{0,8}?业绩|工程|项目)"),
    "social_security":  _re(r"(?:连续|近|最近)\s*[0-9一二三六十二]{1,2}\s*个月[^。；]{0,30}?(?:社保|社会保险|养老保险|社会保障)[^。；，]{0,24}"),
})

# 标签式锚点扩充（命中标签后取完整谓语到句界/下一 label；值为原文条款）
_LABEL_ANCHORS_EXT = {
    "financial":      _re(r"(?:财务(?:状况|要求|能力)|资金要求)\s*[:：]"),
    "payment_terms":  _re(r"(?:付款(?:方式|条件)|支付(?:方式|条件)|工程款支付(?:方式)?|资金支付(?:方式)?|进度款支付(?:方式)?)\s*[:：]"),
    "tech_standard":  _re(r"(?:技术(?:标准|要求|规范)(?:和要求|及要求)?|主要技术(?:参数|指标|要求)|执行标准)\s*[:：]"),
    "scope_content":  _re(r"(?:招标范围|采购内容|建设内容|工程内容|服务内容|采购范围|招标内容|项目概况(?:及招标范围)?)\s*[:：]"),
    "open_location":  _re(r"开标地点\s*[:：]"),
    "bid_location":   _re(r"(?:投标文件)?(?:递交|提交|送达)(?:地点|地址)\s*[:：]"),
    "agency":         _re(r"(?:招标)?代理机构\s*(?:名称)?\s*[:：]"),
    "funding_source": _re(r"(?:资金来源|资金性质|资金来源及落实情况)\s*[:：]"),
    "approval_doc":   _re(r"(?:批准文号|批复文号|审批文号|项目批准文号)\s*[:：]"),
}

_TIME_PATTERNS_EXT = {
    "doc_deadline": _re(r"(?:招标|采购|资格预审)文件(?:的)?(?:获取|发售|领取|下载|出售|购买)(?:时间|期限|截止时间)?[^。；]{0,30}?(?:至|到|止于)\s*(" + _DATE + ")"),
    "q_deadline":   _re(r"(?:质疑|答疑|澄清|异议)(?:截止)?(?:时间|期限|日期)[^。；]{0,25}?(" + _DATE + ")"),
    "site_visit":   _re(r"(?:现场踏勘|踏勘现场|踏勘)(?:时间)?\s*[:：]?[^。；]{0,20}?(" + _DATE + ")"),
}

# 标签式锚点（命中标签后取完整谓语到句界，2026-09-10：值不再截断在首个命中词）
# 「设计质量标准」是 EPC 设计要求，不冒充工程质量 → 负向后行排除
_LABEL_ANCHORS = {
    "quality":      _re(r"(?<!设计)质量(?:标准|要求)\s*[:：]"),
    "qualification": _re(r"资质要求\s*[:：]|(?:具备|具有|须具有)[^。；\n]{0,80}(?:施工总承包|专业承包|专项设计|工程设计综合|不分等级)[^。；\n]{0,200}?资质"),
    "performance":  _re(r"业绩要求\s*[:：]"),
    "scale":        _re(r"(?:建设规模|项目规模)\s*[:：]"),
    "purchaser":    _re(r"(?:采购人|招标人)\s*(?:名称)?\s*[:：]"),
}

_TIME_PATTERNS = {
    # 允许日期后紧跟「09:00 / 09时00分」再接「至/于」（EPC 公告常见「请于…日09:00至…日17:00」）；
    # P3：数字与汉字间允许空白（pdftotext/HTML 净化常见「2026 年 10 月 10 日」）
    "deadline_signup": _re(r"请于\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)(?:\s*\d{1,2}[:：时]\d{2}分?)?\s*(?:至|于)\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"),
    # 允许「递交的截止时间（投标截止时间，下同）为2026年…」的插入语与「为」
    "deadline_bid": _re(r"投标(?:文件)?(?:递交)?(?:的)?截止时间[^。；]{0,30}?(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"),
    "open_date": _re(r"开标时间\s*[:：]?\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"),
}
_LABEL_ANCHORS.update(_LABEL_ANCHORS_EXT)
_TIME_PATTERNS.update(_TIME_PATTERNS_EXT)

# project_type 由 polarity.classify_project_type 按优先级判定（P2）：
# 总承包/EPC > 施工图设计 > 施工 > 监理 > 设计 > 勘察 > 货物/服务（采购语境）
# 2026-09-14：锚点扩充批次新增字段一律不进必查集（护栏：待补率不高于基线）
_HARD_REQUIRED = {"region", "budget_amount", "ceiling_price", "quality",
                  "qualification", "duration", "deadline_bid"}


def _first_group(m):
    """多备选分支各带一个捕获组时取首个非空组；无捕获组取整体命中（docs/10 附录 A
    「_ANCHORS 取组 结构性」：可选组未参与时不再静默落 group(0) 或空串）。"""
    if not m.lastindex:
        return m.group(0).strip()
    for g in m.groups():
        if g:
            return g.strip()
    return m.group(0).strip()


def extract_prescreen(title, text, *, clause="公告原文"):
    """对公告正文抽取初筛字段 → {field_key: PrescreenField}。

    确定性锚点，禁止推断；未命中的必查字段置 missing=True（进待补），不静默丢。
    assertion 为条款级原文摘录（句界对齐）；标签式字段值为完整谓语（到句读）。
    P1：text 视为**固化原文**，每个命中字段附 quote/start/end（固化原文偏移，逐字）；
    P2：极性字段（POLARITY_FIELDS）值由命中原文派生并附 enum；project_type 按优先级分类。
    """
    raw = text or ""
    body, offsets = _collapse_with_map(raw)
    if not body:
        return {}
    title = _clean_text(title or "")
    out = {}

    def _stored_span(s, e):
        """折叠文本 [s,e) → (quote, 固化原文 start, end)；quote == raw[start:end] 由构造成立。"""
        if s >= e or s >= len(offsets):
            return None, None, None
        st = offsets[s]
        en = offsets[min(e, len(offsets)) - 1] + 1
        return raw[st:en], st, en

    def _field(key, value, m, *, max_len=220, end=None, enum=None, missing=False):
        snip, s, e, truncated = _clause_snip(body, m, max_len=max_len, end=end)
        quote, st, en = _stored_span(s, e)
        return PrescreenField(key, value, assertion=snip, clause=clause, missing=missing,
                              quote=quote, start=st, end=en, enum=enum, truncated=truncated)

    def _put(key, value, m, **kw):
        out[key] = _field(key, value, m, **kw)

    for key, pat in _ANCHORS.items():
        m = pat.search(body)
        if not m:
            if key in _HARD_REQUIRED:
                out[key] = PrescreenField(key, None, assertion="", clause=clause, missing=True)
            continue
        val = _first_group(m)
        _put(key, val or None, m)

    for key, pat in _LABEL_ANCHORS.items():
        m = pat.search(body)
        if m is None:
            if key in _HARD_REQUIRED:
                out[key] = PrescreenField(key, None, assertion="", clause=clause, missing=True)
            continue
        group_end = None
        if key == "qualification" and "资质要求" not in m.group(0):
            # 短语式（具备/具有…资质）：从 match 起点取到条款组终点——"；"并列的
            # 安全生产许可证等子项一并保留，不切断在首个"；"（河北工大案例）
            group_end = _group_end(body, m.end(), fwd=400)
            val = _clean_text(body[m.start():group_end]).rstrip("。；;，, ")
        elif key in ("qualification", "performance"):
            # 标签式 + 常见编号列表（（1）设计资质…（2）施工资质…）：整组取值
            val, group_end = _list_aware_tail(body, m)
        else:
            val = _clause_tail(body, m)
        if not val and key in _HARD_REQUIRED:
            # 标签命中但条款为空（如列表排版异常）→ 按未载明占位，不静默丢
            out[key] = _field(key, None, m, missing=True)
            continue
        out[key] = _field(key, val or None, m, max_len=260, end=group_end)

    # 极性字段（P2 注册表）：值由极性判定从命中原文派生（表单式「（是/否）接受联合体投标：0」
    # → 不接受），enum 为机器可校验枚举；无法判定则不产出（非必查字段，不推断）
    for key, pf in POLARITY_FIELDS.items():
        accepts, pm = pf.match(body)
        if pm is not None:
            _put(key, pf.value_for(accepts), pm, enum=accepts)

    for key, pat in _TIME_PATTERNS.items():
        m = pat.search(body)
        if m:
            # 报名窗口展示完整起止区间；单捕 start 日放在「报名截止」标签下有误导。
            # 值内去空白（「2026 年 10 月 10 日」→「2026年10月10日」），摘录仍为原文逐字
            value = _WS.sub("", m.group(1))
            if key == "deadline_signup" and m.lastindex and m.lastindex >= 2:
                value = f"{value}至{_WS.sub('', m.group(2))}"
            _put(key, value, m)
    # 必查时间字段（投标截止）未命中也置 missing 占位，不静默丢（_HARD_REQUIRED 契约）
    for key in sorted(_HARD_REQUIRED & _TIME_PATTERNS.keys()):
        if key not in out:
            out[key] = PrescreenField(key, None, assertion="", clause=clause, missing=True)
    pt = classify_project_type(title)
    if pt:
        # 来源为标题（非正文偏移）：quote=标题原文，start/end 留空，前端不提供正文定位
        out["project_type"] = PrescreenField("project_type", pt, assertion=title,
                                             clause="标题", quote=title)
    return out
