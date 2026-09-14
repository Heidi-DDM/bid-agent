# -*- coding: utf-8 -*-
# P1：溯源门禁与重定位单测（docs/10 §5 P1-4/P1-5）
#   - round-trip 门禁：审计关键字段 quote 不逐字 → 值清空转人工（零容忍）；
#     非关键字段 → 保留值、剥离偏移留 review 标记；
#   - relocate_quote：原文精确 → 折叠重定位 → 找不到返回 None（宁可不定位，不许错位）。
from __future__ import annotations

from runtime.parsing.provenance import (
    build_detail_summary,
    relocate_quote,
    text_sha256,
    verify_field_roundtrip,
)

BODY = ("采购人：河北工业大学 预算金额：97.000000万元 是否接受联合体投标： 0 "
        "3.1 投标人应具备建筑工程施工总承包三级及以上资质；具有有效期内的安全生产许可证。")


def test_build_detail_summary_attaches_offsets_and_hash():
    detail = build_detail_summary("x服务采购公告", BODY, content_hash=text_sha256(BODY))
    jv = detail["joint_venture"]
    assert jv["value"] == "不接受联合体投标" and jv["enum"] is False
    assert BODY[jv["start"]:jv["end"]] == jv["quote"] == "是否接受联合体投标： 0"
    assert detail["qualification"]["quote"].endswith("安全生产许可证。")
    for d in detail.values():
        assert d["content_hash"] == text_sha256(BODY)
        if d.get("start") is not None:
            assert BODY[d["start"]:d["end"]] == d["quote"]


def test_roundtrip_gate_levels_via_build(monkeypatch):
    """真实门禁：抽取器给出错偏移时，审计关键字段拒收转人工、非关键字段保值剥偏移、正确字段不动。"""
    from runtime.parsing import provenance
    from runtime.parsing.announcement_prescreen import PrescreenField

    def fake_extract(title, text, clause="公告原文"):
        return {
            # 审计关键（联合体）：偏移错 → 零容忍
            "joint_venture": PrescreenField("joint_venture", "不接受联合体投标", assertion="x",
                                            quote="是否接受联合体投标： 0", start=0, end=5, enum=False),
            # 非关键（采购人）：偏移错 → 保值、剥偏移、留标记
            "purchaser": PrescreenField("purchaser", "河北工业大学", assertion="采购人：河北工业大学",
                                        quote="采购人：河北工业大学", start=3, end=13),
            # 正确偏移 → 原样通过
            "scale": PrescreenField("scale", "河北工业大学", assertion="采购人：河北工业大学",
                                    quote="采购人：河北工业大学", start=0, end=10),
        }

    monkeypatch.setattr(provenance, "extract_prescreen", fake_extract)
    detail = provenance.build_detail_summary("t", BODY, content_hash="h")
    jv = detail["joint_venture"]
    assert jv["value"] is None and jv["missing"] is True and jv["enum"] is None
    assert jv["review"] == "quote_not_verbatim" and jv["assertion"] == "x"  # 摘录留给人工核对
    pu = detail["purchaser"]
    assert pu["value"] == "河北工业大学" and pu["review"] == "quote_not_verbatim"
    assert pu["quote"] is None and pu["start"] is None and pu["end"] is None
    ok = detail["scale"]
    assert ok["review"] is None and BODY[ok["start"]:ok["end"]] == ok["quote"]
    assert all(d["content_hash"] == "h" for d in detail.values())


def test_roundtrip_gate_non_critical_field_keeps_value_strips_offsets():
    from runtime.parsing.provenance import verify_field_roundtrip
    d = {"quote": "采购人", "start": 0, "end": 3}            # 偏移正确 → 通过
    assert verify_field_roundtrip("采购人：河北工业大学", d) is True
    bad = {"quote": "XYZ", "start": 0, "end": 2}             # 偏移错 → 拦下
    assert verify_field_roundtrip("采购人：河北工业大学", bad) is False
    assert verify_field_roundtrip("正文", {"quote": None, "start": None, "end": None}) is True


def test_relocate_quote_exact_then_folded_then_none():
    text = "服务期限： 2026年10月12日\t至2027年10月11日  本项目（是/否）接受联合体投标： 0"
    q = "本项目（是/否）接受联合体投标： 0"
    # ① 原文精确
    assert relocate_quote(text, q) == (text.index(q), text.index(q) + len(q))
    # ② 折叠重定位：quote 的空白与原文不一致（多空格/制表符折叠后一致）
    span = relocate_quote(text, "2026年10月12日 至2027年10月11日")
    assert span is not None and text[span[0]:span[1]].startswith("2026年10月12日")
    assert text[span[0]:span[1]].endswith("2027年10月11日")
    # ③ 找不到 → None（不猜测定位）
    assert relocate_quote(text, "根本不存在的一句话") is None
    assert relocate_quote(text, "") is None
