# P4 规则预筛 + 云端大模型受约束兜底（docs/10 §5 P4）——链路不变量测试（注入假客户端，不触网）
#
# 覆盖：① 关闭/门禁不过 → 零副作用；② 逐字摘录 → 同一套锚点派生值 + 偏移 round-trip，
# confidence=low/review=llm_located；③ 伪造摘录拒收 + 带错误反馈重试一次 → 仍失败标 llm_failed；
# ④ 摘录在原文但锚点解析不出值 → 拒收；⑤ 审计关键字段值只能来自锚点 f(quote)；
# ⑥ 招标文件侧 missing 候选被 LLM 定位且锚点复核通过才替换，否则保持 missing；
# ⑦ build_detail_summary 端到端只补 missing 字段且再过 round-trip。
from __future__ import annotations

import json

import pytest

from runtime.core.model import ModelNotAllowedError, ModelResult
from runtime.parsing import llm_fallback as lf
from runtime.parsing.extractor import extract_rule_candidates
from runtime.parsing.provenance import build_detail_summary
from runtime.rag.chunker import ParsedPage

# 规则层故意抽不到工期/限价的公告版式（「施工期限」「拦标价」不在锚点词表）
TEXT = ("某市道路改造工程施工招标公告 建设地点：某市某区 施工期限为二百四十日历天。"
        "本项目拦标价 2,350.00 万元。质量标准：合格。投标文件递交的截止时间为2026年10月20日9时30分。"
        "评标办法：综合评估法。")
TITLE = "某市道路改造工程施工招标公告"


class FakeClient:
    """按调用轮次返回预设响应；记录每轮消息（断言重试反馈）。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        r = self.responses.pop(0) if self.responses else {"items": []}
        if isinstance(r, Exception):
            raise r
        return ModelResult(ok=True, data=r)


def _summary():
    return build_detail_summary(TITLE, TEXT, content_hash="h", llm_fallback=False)


def test_rule_layer_leaves_duration_and_ceiling_missing():
    s = _summary()
    assert s["duration"]["missing"] is True
    assert s["ceiling_price"]["missing"] is True


def test_disabled_is_noop_and_never_calls_model():
    client = FakeClient({"items": [{"field_key": "duration", "quote": "施工期限为二百四十日历天。"}]})
    s = _summary()
    out = lf.apply_announcement_fallback(s, TITLE, TEXT, content_hash="h", client=client, enabled=False)
    assert out is s and client.calls == []


def test_gate_refusal_is_fail_closed(monkeypatch):
    def refuse(_messages):
        raise ModelNotAllowedError("public-only 关闭")
    s = _summary()
    out = lf.apply_announcement_fallback(s, TITLE, TEXT, content_hash="h", client=refuse, enabled=True)
    assert out["duration"]["missing"] is True
    assert out["duration"]["review"] == lf.REVIEW_LLM_FAILED


def test_verbatim_quote_value_derived_by_typed_parser_with_roundtrip():
    """规则层漏采的版式：模型定位逐字条款 → 值由确定性类型解析器从摘录切出（中文大写工期 / 带千分位金额）。
    值不由模型给；confidence=low + review=llm_located 必须人工确认；quote 偏移 round-trip 成立。"""
    client = FakeClient({"items": [
        {"field_key": "duration", "quote": "施工期限为二百四十日历天。"},
        {"field_key": "ceiling_price", "quote": "本项目拦标价 2,350.00 万元。"},
    ]}, {"items": []})
    s = _summary()
    out = lf.apply_announcement_fallback(s, TITLE, TEXT, content_hash="h", client=client, enabled=True)
    d = out["duration"]
    assert d["missing"] is False and d["value"] == "二百四十日历天" and d["derived_by"] == "typed:duration"
    assert d["confidence"] == "low" and d["review"] == lf.REVIEW_LLM_LOCATED and d["source"] == "llm"
    assert TEXT[d["start"]:d["end"]] == d["quote"] == "施工期限为二百四十日历天。"
    c = out["ceiling_price"]
    assert c["value"] == "2,350.00 万元" and TEXT[c["start"]:c["end"]] == c["quote"] and c["content_hash"] == "h"
    # 其余仍缺字段（budget/qualification…）模型未返回 → 重试一轮后标 llm_failed
    assert out["budget_amount"]["missing"] is True and out["budget_amount"]["review"] == lf.REVIEW_LLM_FAILED
    assert len(client.calls) == 2


def test_quote_in_text_but_wrong_field_is_rejected():
    """摘录逐字在原文，但按字段类型解析不出值（把质量标准条款冒充工期）→ 拒收，带原因重试。"""
    client = FakeClient({"items": [{"field_key": "duration", "quote": "质量标准：合格"}]}, {"items": []})
    s = _summary()
    out = lf.apply_announcement_fallback(s, TITLE, TEXT, content_hash="h", client=client, enabled=True)
    assert out["duration"]["missing"] is True and out["duration"]["review"] == lf.REVIEW_LLM_FAILED
    assert "未能解析出该字段的值" in client.calls[1][1]["content"]


def test_strict_anchor_preferred_over_typed_parser():
    """摘录里锚点能直接命中时走第一级（与规则层同口径）。"""
    text = TEXT + " 最高投标限价：2350万元"
    s = _summary()
    client = FakeClient({"items": [{"field_key": "ceiling_price", "quote": "最高投标限价：2350万元"}]}, {"items": []})
    out = lf.apply_announcement_fallback(s, TITLE, text, content_hash="h", client=client, enabled=True)
    assert out["ceiling_price"]["value"] == "2350万元" and out["ceiling_price"]["derived_by"] == "anchor"


def test_polarity_field_value_must_be_derivable_from_quote():
    text = "某采购公告 是否接受联合体投标：否 其他内容。"
    s = build_detail_summary("t", text, content_hash="h", llm_fallback=False)
    assert "joint_venture" in s and s["joint_venture"]["enum"] is False  # 规则层本就命中
    # 强制构造 missing 场景验证解析器：摘录含极性 → 派生 enum；不含 → 拒收
    assert lf.derive_value("joint_venture", "本次招标接受联合体投标") == ("接受联合体投标", True, "anchor")
    assert lf.derive_value("subcontract_allowed", "不允许分包") == ("不允许分包", False, "anchor")
    assert lf.derive_value("joint_venture", "联合体成员均应提供") is None


def test_typed_parsers_cover_industry_value_shapes():
    assert lf.derive_value("bid_bond", "投标担保金额人民币贰拾万元整")[0] == "贰拾万元整"
    assert lf.derive_value("performance_bond", "履约担保为中标价的百分之十") [0] == "百分之十"
    assert lf.derive_value("deadline_bid", "递交截止：2026-10-20 09:30")[0] == "2026-10-20"
    assert lf.derive_value("evaluation_method", "本次评标采用经评审的最低投标价法")[0] == "经评审的最低投标价法"
    assert lf.derive_value("prequalification", "采用资格后审方式")[0] == "资格后审"
    assert lf.derive_value("payment_terms", "3.2 工程款支付：按月支付80%")[0] == "按月支付80%"
    assert lf.derive_value("duration", "交货期：签订合同后叁拾天内")[0] == "叁拾天"
    assert lf.derive_value("budget_amount", "无任何数字") is None


def test_fabricated_quote_rejected_then_retry_with_feedback():
    s = _summary()
    client = FakeClient(
        {"items": [{"field_key": "duration", "quote": "施工期限为240日历天。"}]},      # 改写→非逐字
        {"items": [{"field_key": "duration", "quote": "施工期限为二百四十日历天。"}]},  # 重试逐字
    )
    out = lf.apply_announcement_fallback(s, TITLE, TEXT, content_hash="h", client=client, enabled=True)
    assert out["duration"]["value"] == "二百四十日历天"
    assert len(client.calls) == 2
    assert "不是原文逐字子串" in client.calls[1][1]["content"]


def test_model_never_sets_value_directly_even_if_it_tries():
    """模型多给的 value 字段被契约丢弃（值只能来自解析器）；未返回的字段标 llm_failed。"""
    s = _summary()
    client = FakeClient({"items": [{"field_key": "duration", "quote": "施工期限为二百四十日历天。", "value": "999天"}]},
                        {"items": []})
    out = lf.apply_announcement_fallback(s, TITLE, TEXT, content_hash="h", client=client, enabled=True)
    assert out["duration"]["value"] == "二百四十日历天"
    assert out["ceiling_price"]["review"] == lf.REVIEW_LLM_FAILED


def test_bad_json_or_schema_is_tolerated():
    s = _summary()
    client = FakeClient({"items": [{"field_key": "duration"}]}, {"garbage": 1})
    out = lf.apply_announcement_fallback(s, TITLE, TEXT, content_hash="h", client=client, enabled=True)
    assert out["duration"]["missing"] is True


def test_build_detail_summary_end_to_end_with_client():
    client = FakeClient({"items": [{"field_key": "duration", "quote": "施工期限为二百四十日历天。"}]}, {"items": []})
    out = build_detail_summary(TITLE, TEXT, content_hash="h", llm_fallback=True, llm_client=client)
    assert out["duration"]["value"] == "二百四十日历天" and out["duration"]["source"] == "llm"
    assert TEXT[out["duration"]["start"]:out["duration"]["end"]] == out["duration"]["quote"]
    # 规则层已命中的字段不被触碰
    assert out["quality"]["value"] == "合格" and out["quality"].get("source") is None
    assert out["evaluation_method"]["value"] == "综合评估法"


# ── 招标文件侧 ─────────────────────────────────────────────────────────

def _page(n, *paras):
    return ParsedPage(page_no=n, paragraphs=list(paras))


PAGES = [
    _page(1, "招标公告"),
    # 「投标人须为…资质企业」不含基线锚点词面「具备…施工总承包…级」/「资质要求：」→ 规则层 missing
    _page(2, "3.2 投标人须为市政公用工程施工总承包 三级及以上资质企业，", "并持有有效的安全生产许可证；"),
    _page(3, "3.3 本次招标接受联合体投标。", "3.5 投标担保：人民币 贰拾万元整。"),
]


def test_rule_candidates_fallback_replaces_missing_only_when_guard_passes():
    cands = extract_rule_candidates(PAGES, project_id="P", material_id="MAT-L", content_hash="h")
    by = {(c.rule or {}).get("anchor_key"): c for c in cands}
    assert by["qualification_grade"].missing_marker is True
    assert by["bid_bond"].missing_marker is True
    client = FakeClient({"items": [
        {"field_key": "qualification_grade", "quote": "投标人须为市政公用工程施工总承包 三级及以上资质企业，并持有有效的安全生产许可证；"},
        {"field_key": "bid_bond", "quote": "投标担保：人民币 贰拾万元整。"},          # 关键词「保证金」缺 → 拒收
        {"field_key": "pm_registered_builder", "quote": "并持有有效的安全生产许可证；"},  # 在原文但无主题词 → 拒收
        {"field_key": "bid_validity", "quote": "投标有效期 90 日历天"},                 # 不在原文（伪造）→ 拒收
    ]}, {"items": []})
    out = lf.fallback_rule_candidates(PAGES, cands, client=client, enabled=True)
    by2 = {(c.rule or {}).get("anchor_key"): c for c in out}
    q = by2["qualification_grade"]
    assert q.missing_marker is False and q.confidence == "low" and q.page_no == 2
    assert "安全生产许可证" in q.assertion and q.rule["located_by"] == "llm" and q.rule["type"] == "qualification"
    assert "待人工确认" in (q.note or "")
    for k in ("bid_bond", "pm_registered_builder", "bid_validity"):
        assert by2[k].missing_marker is True, k
        assert "转人工补录" in (by2[k].note or "")
    assert len(client.calls) == 2
    fb = client.calls[1][1]["content"]
    assert "主题关键词" in fb and "不是原文逐字子串" in fb


def test_rule_candidates_fallback_disabled_or_enterprise_scope_noop():
    cands = extract_rule_candidates(PAGES, project_id="P", material_id="MAT-L", content_hash="h")
    before = [c.to_dict() for c in cands]
    client = FakeClient({"items": [{"field_key": "qualification_grade", "quote": "x"}]})
    assert [c.to_dict() for c in lf.fallback_rule_candidates(PAGES, cands, client=client, enabled=False)] == before
    assert [c.to_dict() for c in lf.fallback_rule_candidates(
        PAGES, cands, permission_scope="enterprise_read", client=client, enabled=True)] == before
    assert client.calls == []


def test_is_enabled_requires_both_switch_and_gate(monkeypatch):
    from runtime.core import config
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_ENABLED", "false")
    assert lf.is_enabled() is False
    monkeypatch.setenv("DEEPSEEK_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_PUBLIC_ONLY", "true")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.invalid")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    assert lf.is_enabled() is False  # 无凭据 fail-closed，不发起必然 401 的请求
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert lf.is_enabled() is True
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "false")
    assert lf.is_enabled() is False


# ── P4-2 条款发现（2026-09-15）───────────────────────────────────────

def _pages_with_clauses():
    from runtime.rag.chunker import ParsedPage
    return [
        ParsedPage(page_no=1, paragraphs=[
            "3.1 具备有效的营业执照。",
            "3.5 自 2020 年 9 月 1 日以来完成过一项 3000 万元及以上市政工程的施工业绩；",
        ]),
        ParsedPage(page_no=2, paragraphs=[
            "（7）投标人通过 ISO9001 质量管理体系认证的得 3 分，本项满分 3 分。",
        ]),
    ]


def _fake_client(quotes):
    from runtime.core.model import ModelResult
    def call(messages):
        return ModelResult(ok=True, data={"items": [{"quote": q} for q in quotes]})
    return call


def test_discovery_offline_happy_and_filters():
    from runtime.parsing.llm_fallback import discover_rule_candidates
    from runtime.parsing.extractor import extract_rule_candidates

    pages = _pages_with_clauses()
    existing = extract_rule_candidates(pages, project_id="P", material_id="M", content_hash="h")
    # 既有：3.5 业绩（锚点已覆盖）
    quotes = [
        "投标人通过 ISO9001 质量管理体系认证的得 3 分，本项满分 3 分",   # 发现：评分
        "自 2020 年 9 月 1 日以来完成过一项 3000 万元及以上市政工程的施工业绩",  # 与既有锚点候选重叠 → 丢弃
        "这句话不在原文里",                                              # 伪造 → 丢弃
        "好的",                                                          # 太短 → 丢弃
    ]
    out = discover_rule_candidates(pages, existing, project_id="P", material_id="M",
                                   content_hash="h", client=_fake_client(quotes), enabled=True)
    assert len(out) == 1
    c = out[0]
    assert c.req_type == "scored_requirement" and c.category == "评分"
    assert c.page_no == 2 and c.confidence == "low" and not c.missing_marker
    assert c.rule == {"type": "generic", "located_by": "llm", "discovered": True}
    assert c.requirement_id.startswith("M-LLM-") and c.requirement_id.endswith("-draft")
    assert "大模型发现" in (c.note or "")
    # 幂等：同摘录重跑产出同 ID
    out2 = discover_rule_candidates(pages, existing, project_id="P", material_id="M",
                                    content_hash="h", client=_fake_client(quotes[:1]), enabled=True)
    assert [x.requirement_id for x in out2] == [c.requirement_id]


def test_discovery_disabled_and_non_public_zero_effect():
    from runtime.parsing.llm_fallback import discover_rule_candidates
    pages = _pages_with_clauses()
    assert discover_rule_candidates(pages, [], project_id="P", material_id="M",
                                    content_hash="h", enabled=False) == []
    assert discover_rule_candidates(pages, [], project_id="P", material_id="M",
                                    content_hash="h", permission_scope="enterprise_read",
                                    client=_fake_client(["x" * 30]), enabled=True) == []


def test_discovery_category_closed_set():
    from runtime.parsing.llm_fallback import discover_rule_candidates
    from runtime.rag.chunker import ParsedPage
    pages = [ParsedPage(page_no=1, paragraphs=["本项目所在区域气候宜人风景秀丽适合施工。"])]
    out = discover_rule_candidates(pages, [], project_id="P", material_id="M",
                                   content_hash="h",
                                   client=_fake_client(["本项目所在区域气候宜人风景秀丽适合施工。"]),
                                   enabled=True)
    assert out == []  # 词表类别判不出 → 丢弃（不猜）


# ── 2026-09-24 邢台实测回归：发现层分类与去重 ──────────────────────────────
# 用户实测两起分类事故（MAT-PJ1d7fadae0b-TENDER）：
#   ① 「…招标人取消其中标资格…」（合同签订条款）因含弱词「资格」被判**资质**类 →
#      复核页出现"资质要求（大模型发现）"挂着无关摘录；
#   ② 「单位负责人为同一人…不得参加投标」（信用条款）因含弱词「负责人」被判**人员**类 →
#      人员组混入无关行（用户感知"人员要求重复"）。
def test_discovery_classification_requires_topic_strong_words():
    from runtime.parsing.llm_fallback import _classify_discovery

    quote_contract = ("7.5.1招标人和中标人应当自中标通知书发出之日起30日内，根据招标文件和中标人的投标文件"
                      "订立合同。中标人无正当理由拒签合同的，招标人取消其中标资格，其投标保证金不予退还。")
    assert _classify_discovery(quote_contract) != "资质"   # 「中标资格」≠资质要求
    assert _classify_discovery(quote_contract) == "动作"   # 中标通知书/保证金 → 投标动作
    quote_conflict = ("3.13与招标人存在利害关系可能影响招标公正性的法人、其他组织或者个人，不得参加投标；"
                      "单位负责人为同一人或者存在控股、管理关系的不同单位，不得参加同一项目投标；"
                      "投标单位未处于投标禁止期。")
    assert _classify_discovery(quote_conflict) != "人员"   # 「单位负责人」≠人员要求
    assert _classify_discovery(quote_conflict) == "信用"
    quote_cert_a = ("☑3.6企业主要负责人（法定代表人、企业经理、企业分管安全生产的副经理）"
                    "具有对应有效的安全生产考核合格证书。")
    assert _classify_discovery(quote_cert_a) == "人员"
    # 「业绩…得3分」是评分条款（角色类优先于主体类）
    assert _classify_discovery("完成过一项类似市政工程的得3分。") == "评分"
    # 只有弱词/无主题词 → 丢弃
    assert _classify_discovery("本项目所在区域气候宜人风景秀丽适合施工。") is None


def test_discovery_drops_quote_already_covered_by_term_candidates():
    # 「质量保证金 3%」已由 TERM_ANCHORS.retention_money 抽到；发现层此前只对比规则候选
    # → 同一条款在"投标动作与风险条款"组出现两行（用户实测"重复"问题的一部分）
    from runtime.parsing.llm_fallback import discover_rule_candidates
    from runtime.rag.chunker import ParsedPage

    pages = [ParsedPage(page_no=1, paragraphs=[
        "☑从应付的工程款中预留质量保证金，比例为工程价款结算总额的3%；返还方式为：按照双方约定。",
    ])]
    term_like = [type("TermCand", (), {  # 形态对齐 MainCardCandidate（term 候选）
        "assertion": "从应付的工程款中预留质量保证金，比例为工程价款结算总额的3%",
        "missing_marker": False, "category": "", "field_key": "retention_money"})()]
    out_with = discover_rule_candidates(pages, list(term_like), project_id="P", material_id="M",
                                        content_hash="h", client=_fake_client([
        "☑从应付的工程款中预留质量保证金，比例为工程价款结算总额的3%；返还方式为：按照双方约定。"]),
        enabled=True)
    assert out_with == []


# ── 2026-09-24 解析优化四步之三：发现层 LangExtract 式分块多轮 ─────────────
# 单次大截断只送得进得分最高的前几章；分块后每块独立送模型，长文件后部（评标办法/
# 合同条款/投标文件格式）同样进入视野，逐字校验/哈希去重/重叠丢弃/强词分类口径不变。
def test_discovery_chunked_multi_round_cover_late_pages(monkeypatch):
    from runtime.core import config as cfg
    from runtime.parsing.llm_fallback import discover_rule_candidates
    from runtime.rag.chunker import ParsedPage

    filler = "这一页是无关的填充内容，用于把分块预算撑满。" * 30   # ~510 字/页
    # 两个相关页各带主题词且长度超过单块上限 → 必然分成两块（分块多轮才有意义）
    pages = [
        ParsedPage(page_no=1, paragraphs=[filler]),
        ParsedPage(page_no=2, paragraphs=[
            "（一）安全生产考核合格证书要求：企业主要负责人具有安全生产考核合格证书。",
            "关于本条款的说明段落。"] * 12),
        ParsedPage(page_no=3, paragraphs=[filler]),
        ParsedPage(page_no=4, paragraphs=[filler]),
        ParsedPage(page_no=9, paragraphs=[
            "（七）与招标人存在利害关系可能影响招标公正性的，不得参加投标。",
            "关于本条款的说明段落。"] * 12),
    ]
    calls = []

    def client(messages):
        calls.append(messages[1]["content"])
        # 每块都返回该块里的条款（模型只看得到当前块）
        quotes = []
        if "安全生产考核" in messages[1]["content"]:
            quotes.append("企业主要负责人具有安全生产考核合格证书。")
        if "利害关系" in messages[1]["content"]:
            quotes.append("与招标人存在利害关系可能影响招标公正性的，不得参加投标。")
        from runtime.core.model import ModelResult
        return ModelResult(ok=True, data={"items": [{"quote": q} for q in quotes]})

    monkeypatch.setattr(cfg, "llm_discovery_chunk_chars", lambda: 400)
    monkeypatch.setattr(cfg, "llm_discovery_max_chunks", lambda: 4)
    out = discover_rule_candidates(pages, [], project_id="P", material_id="M",
                                   content_hash="h", client=client, enabled=True)
    assert len(calls) >= 2, f"应分多块调用（实际 {len(calls)} 次）"
    cats = {c.category for c in out}
    assert cats == {"人员", "信用"}          # 后部页面（利害关系）也能被发现
    assert all(c.page_no in (2, 9) for c in out)


def test_discovery_chunk_budget_respected(monkeypatch):
    from runtime.core import config as cfg
    from runtime.parsing.llm_fallback import discover_rule_candidates
    from runtime.rag.chunker import ParsedPage

    pages = [ParsedPage(page_no=i, paragraphs=["评分：本项得3分。" + "填充" * 100])
             for i in range(1, 9)]
    calls = []

    def client(messages):
        calls.append(1)
        from runtime.core.model import ModelResult
        return ModelResult(ok=True, data={"items": []})

    monkeypatch.setattr(cfg, "llm_discovery_chunk_chars", lambda: 400)
    monkeypatch.setattr(cfg, "llm_discovery_max_chunks", lambda: 2)
    out = discover_rule_candidates(pages, [], project_id="P", material_id="M",
                                   content_hash="h", client=client, enabled=True)
    assert out == []
    assert len(calls) == 2                    # 预算硬上限：块数 ≤ max_chunks
