# 溯源门禁与重定位（2026-09-11 P1，docs/10 §5 P1-4/P1-5）
#
# 入库前硬校验（round-trip）：detail_summary 每个带偏移的字段断言
#   固化文本[start:end] == quote
# 失败按风险分级：审计关键字段（资质/联合体/金额/截止时间）**零容忍**——值清空、
# missing=True、review=quote_not_verbatim 转人工复核；其余字段保留值、仅剥离偏移并留
# review 标记。正常情况下 quote 由构造切片而来、门禁必过——它防的是实现回归（bug），
# 不是概率过滤（对应 TDS《Validating the RAG Answer》与 MDPI 证据中心系统的入库硬约束）。
from __future__ import annotations

import hashlib
import re
from typing import Optional, Tuple

from runtime.parsing.announcement_prescreen import extract_prescreen

# 审计关键字段：报价与资格判断的输入，quote 不逐字即拒收转人工
# 2026-09-14 锚点扩充：保证金/担保/预付款/下浮率（报价成本）、投标有效期、分包与进口产品极性、
# 文件获取截止一并纳入零容忍集
AUDIT_CRITICAL_FIELDS = frozenset({
    "qualification", "joint_venture", "sme_dedicated", "subcontract_allowed", "import_allowed",
    "budget_amount", "ceiling_price", "equip_amount", "work_amount",
    "bid_bond", "performance_bond", "advance_payment", "retention_money", "downward_rate",
    "provisional_sum", "bid_validity",
    "deadline_bid", "deadline_signup", "open_date", "doc_deadline",
})

_WS = re.compile(r"\s+")


def text_sha256(text: str) -> str:
    """固化文本指纹（与服务端入库 sha256 同口径：utf-8 字节）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def verify_field_roundtrip(text: str, field: dict) -> bool:
    """quote/start/end 任一为空视为无偏移（不适用）；三者齐备才校验。"""
    start, end, quote = field.get("start"), field.get("end"), field.get("quote")
    if start is None or end is None or quote is None:
        return True
    return text[start:end] == quote


def build_detail_summary(title: str, text: str, *, content_hash: Optional[str] = None,
                         clause: str = "公告原文", llm_fallback: Optional[bool] = None,
                         llm_client=None) -> dict:
    """抽取 + 逐字段 round-trip 门禁 → detail_summary dict（service 与存量回填共用口径）。

    content_hash 绑定固化版本：文本重抓/多版本后旧偏移按 hash 判定失效（前端定位兜底）。
    P4（2026-09-14）：门禁之后对仍 missing 的字段跑「云端大模型定位摘录 → 同一套锚点复核」兜底
    （runtime/parsing/llm_fallback）；llm_fallback=None 按配置开关，False 强制关闭（离线回填/
    测试），兜底产物 confidence=low + review=llm_located，必须人工确认；兜底任何异常不阻断主链。
    """
    summary = extract_prescreen(title, text, clause=clause)
    out: dict = {}
    for key, f in summary.items():
        d = f.to_dict()
        d["content_hash"] = content_hash
        if not verify_field_roundtrip(text, d):
            d["review"] = "quote_not_verbatim"
            if key in AUDIT_CRITICAL_FIELDS:
                d["value"], d["missing"], d["enum"] = None, True, None
            else:
                d["quote"] = d["start"] = d["end"] = None
        out[key] = d
    if llm_fallback is False:
        return out
    try:
        from runtime.parsing.llm_fallback import apply_announcement_fallback
        out = apply_announcement_fallback(out, title, text, content_hash=content_hash,
                                          client=llm_client, enabled=llm_fallback)
        # 兜底产物再过一次 round-trip（构造性必过；防兜底实现回归）
        for key, d in out.items():
            if d.get("source") == "llm" and not verify_field_roundtrip(text, d):
                d["value"], d["missing"], d["enum"] = None, True, None
                d["review"] = "quote_not_verbatim"
    except Exception as exc:  # pragma: no cover - 兜底层异常不得影响确定性主链
        import logging
        logging.getLogger("runtime.parsing.provenance").warning("LLM 兜底跳过：%s", exc)
    return out


def relocate_quote(text: str, quote: str) -> Optional[Tuple[int, int]]:
    """偏移失效时找回 quote 在固化文本中的位置（返回 [start,end)）。

    两级：① 原文精确 indexOf（零漂移）；② 折叠重定位——把两侧空白串折叠成单空格后
    精确匹配（与抽取工作面同口径，构造性精确，不引入模糊阈值）；仍找不到返回 None
    （调用方必须放弃定位而不是错位——宁可不定位，不许错位）。
    """
    if not quote:
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)
    # 折叠重定位：构建 text 的 (folded, offsets)，在 folded 中找折叠后的 quote
    folded, offsets = _fold_with_offsets(text)
    fq = _WS.sub(" ", quote).strip()
    j = folded.find(fq)
    if j < 0:
        return None
    e = j + len(fq)
    start = offsets[j]
    end = offsets[min(e, len(offsets)) - 1] + 1
    return start, end


def _fold_with_offsets(text: str):
    folded: list[str] = []
    offsets: list[int] = []
    i, n = 0, len(text)
    while i < n and text[i].isspace():
        i += 1
    tail = n
    while tail > i and text[tail - 1].isspace():
        tail -= 1
    while i < tail:
        if text[i].isspace():
            j = i
            while j < tail and text[j].isspace():
                j += 1
            folded.append(" ")
            offsets.append(i)
            i = j
        else:
            folded.append(text[i])
            offsets.append(i)
            i += 1
    return "".join(folded), offsets
