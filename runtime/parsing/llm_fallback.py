# P4 — 规则预筛 + 云端大模型受约束兜底（docs/10 §5 P4，2026-09-14 启用）
#
# 方法论来源（docs/10 §4 调研）：
#   - Inupedia/tender-extract：「分层置信度正则 + 按需 LLM」——规则层先跑，只有规则未命中/低置信
#     的字段才交给模型，模型产物必须带原文片段回链；
#   - 567-labs/instructor：「pydantic 契约 + 校验失败带错误反馈重试」——模型输出先过 schema 校验，
#     再过业务校验器，不过则把错误原因回灌给模型重试一次；
#   - TDS《Validating the RAG Answer》/ MDPI 证据中心系统：quote 必须是原文精确子串
#     （归一只折叠空白），不过则按风险分级拒收转人工。
#
# 不变量（与 docs/10 §3 G1–G5 同构，全部机器可验证）：
#   1. 模型**只准输出 {field_key, quote}**，不准直接产出值；
#   2. quote 必须是固化原文逐字子串：normalize(quote) ⊂ normalize(原文) → relocate_quote 精确定位偏移；
#   3. 值 = f(quote)：把 quote 交回**同一套确定性锚点**（extract_prescreen / extractor ANCHORS）派生，
#      锚点在 quote 上都命不中 → 拒收（模型找到的不是该字段的条款）；
#   4. 任一步不过 → 该字段保持 missing；带错误反馈重试一次；仍不过 → review="llm_failed" 转人工；
#   5. 模型定位成功的字段一律 confidence=low、review="llm_located"、source="llm"，**必须人工确认**；
#      审计关键字段的值同样只能来自锚点 f(quote)，模型永不直接定值；
#   6. 开关关闭 / 非 public_read / 门禁不过 / 网络失败 → 零副作用（fail-closed），主链不受影响。
#
# 企业私有资料永不经过本模块（permission_scope 门禁在 model.deepseek_chat_json）。
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from runtime.core import config
from runtime.core.model import ModelNotAllowedError, ModelResult, check_deepseek_allowed, deepseek_chat_json

logger = logging.getLogger("runtime.parsing.llm_fallback")

REVIEW_LLM_LOCATED = "llm_located"   # 模型定位 + 规则复核通过，待人工确认
REVIEW_LLM_FAILED = "llm_failed"     # 模型重试后仍未通过校验，转人工
SOURCE_LLM = "llm"

_WS = re.compile(r"\s+")
_MAX_QUOTE_LEN = 400
_RETRIES = 1  # docs/10 P4-3：校验不过 → 重试一次 → 仍不过转人工


# ── pydantic 契约（instructor 风格：结构先过 schema，再过业务校验器）────────────


class QuoteLocation(BaseModel):
    """一个字段的原文摘录定位：模型只被允许填这两项。"""

    field_key: str = Field(min_length=1, max_length=64)
    quote: str = Field(min_length=1, max_length=_MAX_QUOTE_LEN)

    @field_validator("quote")
    @classmethod
    def _quote_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("quote 不能为空白")
        return v


class QuoteLocations(BaseModel):
    items: list[QuoteLocation] = Field(default_factory=list)


# ── 公告侧字段说明（给模型的字段语义，只描述"找什么"，不给任何取值规则）──────────

ANNOUNCEMENT_FIELD_HINTS: dict[str, str] = {
    "region": "建设地点 / 项目所在地",
    "budget_amount": "预算金额 / 总投资 / 采购预算",
    "ceiling_price": "最高投标限价 / 招标控制价",
    "quality": "质量标准 / 质量要求",
    "qualification": "投标人资质要求（资质等级、安全生产许可证等）",
    "duration": "计划工期 / 工期",
    "deadline_bid": "投标（文件递交）截止时间",
    "deadline_signup": "报名 / 招标文件获取起止时间",
    "open_date": "开标时间",
    "performance": "业绩要求",
    "scale": "建设规模 / 项目规模",
    "purchaser": "采购人 / 招标人",
    "bid_bond": "投标保证金金额",
    "bid_validity": "投标有效期",
    "evaluation_method": "评标办法",
    "payment_terms": "付款方式",
    "performance_bond": "履约保证金 / 履约担保",
    "contract_type": "合同类型 / 计价方式",
    "prequalification": "资格审查方式",
}


def is_enabled() -> bool:
    """兜底是否可用：总开关 + DeepSeek 出域门禁（任一不满足即 fail-closed）。"""
    return config.llm_fallback_enabled() and check_deepseek_allowed()


def _normalize(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


# ── 值 = f(quote)：确定性类型解析器（第二级；第一级是同一套抽取锚点）─────────────
# 规则层漏采的版式（「施工期限为二百四十日历天」「拦标价 2,350.00 万元」）由模型定位摘录后，
# 值仍只能由这些**确定性**解析器从摘录中切出：金额/百分比/工期/日期认数字（阿拉伯或中文大小写）
# + 单位；极性走 POLARITY_FIELDS；枚举只认词表；条款文本 = 摘录去掉前导 label。解析不出 → 拒收。
_CN_NUM = r"[零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟两]+"
_NUM = r"(?:[0-9][0-9，,.\s]{0,12}[0-9]?|" + _CN_NUM + r")"
_AMOUNT_RE = re.compile(_NUM + r"\s*(?:万元|亿元|万|元)(?:整)?")
_PERCENT_RE = re.compile(r"(?:[0-9]{1,3}(?:\.[0-9]+)?|" + _CN_NUM + r")\s*[%％]|百分之" + _CN_NUM)
_DURATION_RE = re.compile(_NUM + r"\s*(?:日历天|个月|个工作日|工作日|天|日|年|周)")
_DATE_RE = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日(?:\s*\d{1,2}\s*[:：时]\s*\d{2}\s*分?)?|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
_LABEL_PREFIX = re.compile(r"^[0-9.（）()一二三四五六七八九十、\s]{0,8}[\u4e00-\u9fff（）()/／、\s]{1,20}[:：]\s*")
_NEG_LABEL_BLOCK = re.compile(r"不(?:得)?(?:参与|作为|列入)?竞争(?:性)?(?:费用|报价)?")

EVALUATION_METHODS = ("经评审的最低投标价法", "最低投标价法", "最低价法", "最低评标价法", "综合评估法",
                      "综合评分法", "综合评审法", "综合评价法", "合理低价法", "性价比法", "定性评审法", "随机抽取法")
PREQUAL_METHODS = ("资格后审", "资格预审", "后审", "预审")
CONTRACT_TYPES = ("固定总价合同", "固定单价合同", "可调总价合同", "可调单价合同", "成本加酬金合同",
                  "固定总价包干", "总价包干", "固定总价", "固定单价", "总价合同", "单价合同", "成本加酬金")

FIELD_TYPES: dict[str, str] = {}
for _k in ("budget_amount", "ceiling_price", "equip_amount", "work_amount", "bid_bond", "provisional_sum", "file_price"):
    FIELD_TYPES[_k] = "amount"
for _k in ("performance_bond", "advance_payment", "retention_money", "safety_fee"):
    FIELD_TYPES[_k] = "amount_or_percent"
FIELD_TYPES["downward_rate"] = "percent"
for _k in ("duration", "bid_validity", "warranty"):
    FIELD_TYPES[_k] = "duration"
for _k in ("deadline_bid", "deadline_signup", "open_date", "doc_deadline", "q_deadline", "site_visit"):
    FIELD_TYPES[_k] = "date"
for _k in ("joint_venture", "sme_dedicated", "subcontract_allowed", "import_allowed"):
    FIELD_TYPES[_k] = "polarity"
FIELD_TYPES["evaluation_method"] = "enum:evaluation"
FIELD_TYPES["prequalification"] = "enum:prequal"
FIELD_TYPES["contract_type"] = "enum:contract"
# 其余（quality/qualification/performance/scale/purchaser/region/financial/payment_terms/...）→ clause


def _enum_pick(verbatim: str, options: tuple[str, ...]):
    best = None
    for opt in options:  # 词表按长词在前排列，首个命中即最长
        i = verbatim.find(opt)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, opt)
    return best[1] if best else None


def derive_value(field_key: str, verbatim: str, title: str = ""):
    """从逐字摘录派生值：(value, enum, derived_by) 或 None。

    第一级：同一套公告锚点在摘录上直接命中（语义与规则层完全一致）；
    第二级：按字段类型的确定性解析器（金额/百分比/工期/日期/极性/枚举/条款文本）。
    两级都解析不出 → None（摘录不是该字段的条款，拒收）。
    """
    from runtime.parsing.announcement_prescreen import extract_prescreen
    from runtime.parsing.polarity import POLARITY_FIELDS

    strict = extract_prescreen(title, verbatim).get(field_key)
    if strict is not None and not strict.missing and strict.value not in (None, ""):
        return strict.value, strict.enum, "anchor"
    ftype = FIELD_TYPES.get(field_key, "clause")
    if ftype == "polarity":
        pf = POLARITY_FIELDS[field_key]
        accepts, m = pf.match(verbatim)
        return (pf.value_for(accepts), accepts, "typed:polarity") if m is not None else None
    if ftype == "amount":
        m = _AMOUNT_RE.search(verbatim)
        return (_normalize(m.group(0)), None, "typed:amount") if m else None
    if ftype == "amount_or_percent":
        if field_key == "safety_fee":
            m = _NEG_LABEL_BLOCK.search(verbatim)
            if m:
                return m.group(0), None, "typed:enum"
        m = _PERCENT_RE.search(verbatim) or _AMOUNT_RE.search(verbatim)
        return (_normalize(m.group(0)), None, "typed:amount_or_percent") if m else None
    if ftype == "percent":
        m = _PERCENT_RE.search(verbatim)
        return (_normalize(m.group(0)), None, "typed:percent") if m else None
    if ftype == "duration":
        m = _DURATION_RE.search(verbatim)
        return (_normalize(m.group(0)), None, "typed:duration") if m else None
    if ftype == "date":
        m = _DATE_RE.search(verbatim)
        return (re.sub(r"\s+", "", m.group(0)), None, "typed:date") if m else None
    if ftype.startswith("enum:"):
        table = {"enum:evaluation": EVALUATION_METHODS, "enum:prequal": PREQUAL_METHODS,
                 "enum:contract": CONTRACT_TYPES}[ftype]
        v = _enum_pick(verbatim, table)
        return (v, None, "typed:enum") if v else None
    # clause：摘录去掉前导编号/label 与尾部标点即为值（长字段的规则层口径亦为"条款文本"）
    v = _LABEL_PREFIX.sub("", _normalize(verbatim), count=1).rstrip("。；;，, ")
    return (v, None, "typed:clause") if len(v) >= 2 else None


def _default_client(messages: list[dict]) -> ModelResult:
    return deepseek_chat_json(
        messages, permission_scope="public_read",
        timeout=config.llm_fallback_timeout_seconds(),
    )


def _build_messages(text: str, fields: dict[str, str], feedback: Optional[str]) -> list[dict]:
    """系统提示写死不变量；用户消息给字段清单 + 原文；重试时附上一轮的错误反馈。"""
    field_lines = "\n".join(f"- {k}: {label}" for k, label in fields.items())
    system = (
        "你是招标公告原文摘录定位器。任务：对给定字段，在原文中找到**逐字对应的原文片段**并原样复制。\n"
        "硬性规则：\n"
        "1. 只能从原文逐字复制（含标点），不得改写、缩写、翻译、补全、拼接不相邻的片段；\n"
        "2. 每个字段给出一个片段，尽量是完整条款（含字段名/标签和值），长度不超过 300 字；\n"
        "3. 原文中找不到的字段**不要输出**，不得猜测或编造；\n"
        "4. 不要输出字段的值、解释或任何多余内容；\n"
        "5. 只输出 JSON 对象：{\"items\": [{\"field_key\": \"...\", \"quote\": \"...\"}]}。"
    )
    user = f"需要定位的字段：\n{field_lines}\n\n原文：\n{text}"
    if feedback:
        user = f"上一轮结果未通过校验：\n{feedback}\n请严格逐字复制原文后重试。\n\n" + user
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_response(result: ModelResult) -> tuple[list[QuoteLocation], Optional[str]]:
    """模型响应 → 契约对象列表；schema 不过返回 (已通过的项, 错误反馈)。"""
    if not result.ok or not isinstance(result.data, dict):
        return [], f"模型调用失败：{result.error or '无响应'}"
    raw_items = result.data.get("items")
    if raw_items is None and isinstance(result.data, dict):
        # 容错：模型可能直接返回 {field_key: quote}
        raw_items = [{"field_key": k, "quote": v} for k, v in result.data.items()
                     if isinstance(v, str)]
    try:
        parsed = QuoteLocations(items=raw_items or [])
    except ValidationError as exc:
        # 逐项抢救：跳过不合规项，其余保留
        ok: list[QuoteLocation] = []
        for it in raw_items or []:
            try:
                ok.append(QuoteLocation(**it))
            except (ValidationError, TypeError):
                continue
        return ok, f"schema 校验失败：{exc.error_count()} 项不合规"
    return parsed.items, None


# ── 公告侧：detail_summary 缺失字段兜底 ───────────────────────────────────


def _locate_and_verify(text: str, title: str, field_key: str, quote: str) -> tuple[Optional[dict], Optional[str]]:
    """G1/G2 校验链：quote 逐字定位（精确→折叠重定位）→ 值 = f(quote)（锚点 → 类型解析器）。
    返回 (字段 dict, None) 或 (None, 拒收原因)。quote 即证据（固化原文偏移），值只从 quote 内切出。"""
    from runtime.parsing.provenance import relocate_quote

    if _normalize(quote) not in _normalize(text):
        return None, "摘录不是原文逐字子串"
    span = relocate_quote(text, quote)
    if span is None:
        return None, "摘录无法在原文中精确定位"
    q_start, q_end = span
    verbatim = text[q_start:q_end]
    derived = derive_value(field_key, verbatim, title)
    if derived is None:
        return None, "确定性解析器在摘录内未能解析出该字段的值（摘录可能不是该字段的条款）"
    value, enum, derived_by = derived
    d = {
        "field_key": field_key, "value": value, "assertion": _normalize(verbatim),
        "clause": "公告原文", "confidence": "low", "missing": False,
        "quote": verbatim, "start": q_start, "end": q_end, "enum": enum,
        "truncated": False, "review": REVIEW_LLM_LOCATED, "source": SOURCE_LLM,
        "derived_by": derived_by,
    }
    if text[d["start"]:d["end"]] != d["quote"]:
        return None, "偏移换算后 round-trip 失败"
    return d, None


def locate_missing_fields(
    title: str, text: str, field_keys: list[str], *,
    client: Optional[Callable[[list[dict]], ModelResult]] = None,
) -> tuple[dict[str, dict], dict[str, str]]:
    """对规则层未命中的字段调用云端模型定位摘录并做确定性复核。

    返回 (accepted{field_key: 字段 dict}, rejected{field_key: 原因})。零字段/门禁不过/
    调用失败 → (空, 原因)；调用方按 fail-closed 保持 missing。
    """
    keys = [k for k in field_keys if k in ANNOUNCEMENT_FIELD_HINTS]
    if not keys:
        return {}, {}
    call = client or _default_client
    body = (text or "")[: config.llm_fallback_max_chars()]
    pending = {k: ANNOUNCEMENT_FIELD_HINTS[k] for k in keys}
    accepted: dict[str, dict] = {}
    rejected: dict[str, str] = {}
    feedback: Optional[str] = None
    for attempt in range(_RETRIES + 1):
        try:
            result = call(_build_messages(body, pending, feedback))
        except ModelNotAllowedError as exc:
            return {}, {k: f"门禁拒绝：{exc}" for k in pending}
        except Exception as exc:  # 网络/未知异常：不阻断主链
            return accepted, {**rejected, **{k: f"调用异常：{type(exc).__name__}" for k in pending}}
        items, schema_err = _parse_response(result)
        errors: list[str] = [schema_err] if schema_err else []
        seen: set[str] = set()
        for it in items:
            if it.field_key not in pending or it.field_key in seen:
                continue
            seen.add(it.field_key)
            field, why = _locate_and_verify(text, title, it.field_key, it.quote)
            if field is not None:
                accepted[it.field_key] = field
            else:
                errors.append(f"{it.field_key}: {why}")
                rejected[it.field_key] = why or "校验失败"
        for k in list(pending):
            if k in accepted:
                pending.pop(k)
            elif k not in seen:
                rejected.setdefault(k, "模型未返回该字段")
        if not pending or attempt == _RETRIES:
            break
        feedback = "\n".join(errors) or "部分字段未返回"
    for k in pending:
        rejected.setdefault(k, "重试后仍未通过校验")
    return accepted, rejected


def apply_announcement_fallback(
    summary: dict, title: str, text: str, *, content_hash: Optional[str] = None,
    client: Optional[Callable[[list[dict]], ModelResult]] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """在 build_detail_summary 的规则层 + round-trip 门禁之后运行：只补 missing 字段。

    返回新的 summary（原 dict 不改）。关闭/门禁不过 → 原样返回。被模型补上的字段带
    review=llm_located / confidence=low / source=llm；未补上的字段 review=llm_failed。
    """
    on = is_enabled() if enabled is None else enabled
    if not on:
        return summary
    missing_keys = [k for k, f in summary.items() if f.get("missing") and k in ANNOUNCEMENT_FIELD_HINTS]
    if not missing_keys:
        return summary
    accepted, rejected = locate_missing_fields(title, text, missing_keys, client=client)
    out = dict(summary)
    for k, field in accepted.items():
        field["content_hash"] = content_hash
        out[k] = field
    for k, why in rejected.items():
        if k in out and out[k].get("missing"):
            d = dict(out[k])
            d["review"] = REVIEW_LLM_FAILED
            d["review_note"] = why
            out[k] = d
    if accepted or rejected:
        logger.info("LLM 兜底（公告）accepted=%s rejected=%s", sorted(accepted), sorted(rejected))
    return out


# ── 招标文件侧：missing_marker 规则候选兜底 ────────────────────────────────

# 基线锚点（可产出 missing 的 16 项）→ 主题关键词组：摘录必须每组至少含一个词（确定性守门，
# 防模型拿无关条款冒充）；typed 为可选的类型守门（金额/工期必须能从摘录切出数字+单位）。
ANCHOR_TOPIC_GUARDS: dict[str, dict] = {
    "qualification_grade": {"groups": [["资质"]]},
    "safety_license": {"groups": [["安全生产许可证"]]},
    "pm_registered_builder": {"groups": [["项目经理", "项目负责人"], ["建造师"]]},
    "pm_b_cert": {"groups": [["安全生产考核合格证", "B类", "B证", "安全考核合格证"]]},
    "pm_no_active": {"groups": [["项目经理"], ["在施", "在建", "在岗", "其他项目"]]},
    "pm_social_security": {"groups": [["社保", "社会保险", "养老保险", "社会保障"]]},
    "safety_officer": {"groups": [["安全生产管理人员", "安全员", "安全管理人员"]]},
    "tech_team": {"groups": [["技术人员", "技术负责人", "专业人员", "技术团队"]]},
    "financial_audit": {"groups": [["审计报告", "财务报告", "财务审计", "财务状况", "审计"]]},
    "credit_no_loser": {"groups": [["失信"]]},
    "consortium": {"groups": [["联合体"]], "polarity": True},
    "bid_validity": {"groups": [["投标有效期"]], "typed": _DURATION_RE},
    "bid_bond": {"groups": [["保证金"]], "typed": _AMOUNT_RE},
    "ceiling_price": {"groups": [["限价", "控制价", "拦标价"]], "typed": _AMOUNT_RE},
    "scoring_tech": {"groups": [["技术标", "技术部分", "技术评审", "暗标"]]},
    "scoring_similar_performance": {"groups": [["业绩"]]},
}


def _guard_ok(anchor_key: str, norm_quote: str) -> Optional[str]:
    """None=通过；否则返回拒收原因。未登记守门的锚点一律拒收（不推断）。"""
    from runtime.parsing.polarity import consortium_match

    g = ANCHOR_TOPIC_GUARDS.get(anchor_key)
    if g is None:
        return "该锚点未登记兜底守门规则"
    for group in g["groups"]:
        if not any(w in norm_quote for w in group):
            return f"摘录缺少主题关键词 {group}"
    typed = g.get("typed")
    if typed is not None and typed.search(norm_quote) is None:
        return "摘录内无法切出数值+单位"
    if g.get("polarity") and consortium_match(norm_quote)[0] is None:
        return "摘录内极性无法判定"
    return None


def _norm_nospace(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _find_verbatim_in_pages(pages: list, quote: str) -> Optional[tuple[int, str, str]]:
    """去空白后在逐页文本里精确定位 quote → (page_no, 原文片段, 归一片段)；找不到 None。"""
    nq = _norm_nospace(quote)
    if not nq:
        return None
    for p in pages:
        joined = "\n".join(getattr(p, "paragraphs", []) or [])
        raw_chars = [ch for ch in joined if ch not in " \t\r\n\f"]
        norm_text = "".join(raw_chars)
        i = norm_text.find(nq)
        if i >= 0:
            return p.page_no, "".join(raw_chars[i:i + len(nq)]), nq
    return None


def fallback_rule_candidates(
    pages: list, candidates: list, *, permission_scope: str = "public_read",
    client: Optional[Callable[[list[dict]], ModelResult]] = None,
    enabled: Optional[bool] = None,
) -> list:
    """对 extract_rule_candidates 产出的 missing_marker 候选做受约束兜底。

    模型只定位摘录；摘录须在某页去空白文本中精确出现，且通过该锚点的确定性守门
    （主题关键词组 + 金额/工期类型守门 + 联合体极性可判），才用其替换 missing 候选
    （confidence=low、note 标明 LLM 定位待人工确认、rule.located_by=llm）。
    其余情况保持 missing（不推断）。permission_scope 非 public_read 直接返回原列表。
    """
    from runtime.parsing.extractor import ANCHORS, CONFIDENCE_LOW, _clause_from_line, _rule_type_for
    from runtime.parsing.polarity import consortium_match

    on = is_enabled() if enabled is None else enabled
    if not on or permission_scope != "public_read":
        return candidates
    missing = [c for c in candidates if getattr(c, "missing_marker", False)
               and (c.rule or {}).get("anchor_key") in ANCHORS]
    if not missing:
        return candidates
    body = "\n".join("\n".join(getattr(p, "paragraphs", []) or []) for p in pages)
    body = body[: config.llm_fallback_max_chars()]
    pending = {(c.rule or {})["anchor_key"]: f"{c.category}：{c.clause_ref}" for c in missing}
    by_key = {(c.rule or {})["anchor_key"]: c for c in missing}
    call = client or _default_client
    feedback: Optional[str] = None
    replaced = 0
    for attempt in range(_RETRIES + 1):
        try:
            result = call(_build_messages(body, pending, feedback))
        except ModelNotAllowedError:
            break
        except Exception as exc:
            logger.warning("LLM 兜底（规则候选）调用异常：%s", type(exc).__name__)
            break
        items, schema_err = _parse_response(result)
        errors: list[str] = [schema_err] if schema_err else []
        for it in items:
            if it.field_key not in pending:
                continue
            found = _find_verbatim_in_pages(pages, it.quote)
            if found is None:
                errors.append(f"{it.field_key}: 摘录不是原文逐字子串")
                continue
            page_no, snippet, norm_hit = found
            why = _guard_ok(it.field_key, norm_hit)
            if why is not None:
                errors.append(f"{it.field_key}: {why}")
                continue
            cand = by_key[it.field_key]
            cand.assertion = snippet[:300]
            cand.page_no = page_no
            cand.clause_ref = f"{_clause_from_line(snippet, ANCHORS[it.field_key]['clause_hint'])}（{ANCHORS[it.field_key]['clause_hint']}）"
            cand.confidence = CONFIDENCE_LOW
            cand.missing_marker = False
            cand.note = "LLM 定位原文摘录，确定性锚点复核通过——待人工确认（P4）"
            rule = dict(cand.rule or {})
            rule["type"] = _rule_type_for(it.field_key)
            rule["located_by"] = SOURCE_LLM
            if it.field_key == "consortium":
                rule["accepts_consortium"] = consortium_match(norm_hit)[0]
            cand.rule = rule
            pending.pop(it.field_key, None)
            replaced += 1
        if not pending or attempt == _RETRIES:
            break
        feedback = "\n".join(errors) or "部分字段未返回"
    for key in pending:
        cand = by_key[key]
        cand.note = (cand.note or "") + "；LLM 兜底未通过校验，转人工补录"
    if replaced:
        logger.info("LLM 兜底（规则候选）替换 missing=%s 剩余=%s", replaced, len(pending))
    return candidates
