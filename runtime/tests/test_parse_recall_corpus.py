# P3 golden 召回语料（招标文件侧）：runtime/tests/golden/parse_recall_cases.json 全量加载。
# 每条用例 = 用户实测发现「原文明确写了、系统没抽到/抽错」的原文片段 + 期望结果。
# 新增用例只需往 JSON 里加一条（pages 用原文逐字摘录），本文件不用改。
from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.parsing.extractor import extract_main_card, extract_rule_candidates, extract_term_candidates
from runtime.rag.chunker import ParsedPage

_CORPUS = Path(__file__).parent / "golden" / "parse_recall_cases.json"


def _load_cases() -> list[dict]:
    with _CORPUS.open(encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("cases") or []
    assert cases, "召回语料为空：golden/parse_recall_cases.json 至少应保留基线用例"
    return cases


def _pages(case: dict) -> list[ParsedPage]:
    return [ParsedPage(page_no=p["page_no"], paragraphs=list(p.get("paragraphs") or []))
            for p in case.get("pages") or []]


CASES = _load_cases()
IDS = [c["case_id"] for c in CASES]


def test_corpus_every_case_has_source_and_regression_note():
    # 语料登记门槛：没有出处和回归说明的用例不许进（可回溯性是语料本身的价值）
    for c in CASES:
        assert c.get("source"), f"{c['case_id']} 缺 source（材料编号+页码）"
        assert c.get("regression"), f"{c['case_id']} 缺 regression（说明修复背景）"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_recall_case(case: dict):
    kind = case["kind"]
    expect = case["expect"]

    if kind == "main_card":
        fields = {f.field_key: f for f in extract_main_card(_pages(case))}
        got = fields[expect["field"]]
        if "value" in expect:
            assert got.missing_marker is False, f"{case['case_id']}: {expect['field']} 仍缺失"
            assert got.value == expect["value"], f"{case['case_id']}: {got.value!r}"
        else:
            assert got.missing_marker is True

    elif kind == "rule_candidate":
        cands = extract_rule_candidates(_pages(case), project_id="P", material_id="M", content_hash="h")
        by_anchor = {c.rule.get("anchor_key"): c for c in cands}
        if "anchor" in expect:
            c = by_anchor[expect["anchor"]]
            assert c.missing_marker is False, f"{case['case_id']}: {expect['anchor']} 仍缺失"
            if "assertion_equals" in expect:
                assert c.assertion == expect["assertion_equals"], f"{case['case_id']}: {c.assertion!r}"
            if "assertion_contains" in expect:
                assert expect["assertion_contains"] in c.assertion, f"{case['case_id']}: {c.assertion!r}"
            if "assertion_not_contains" in expect:
                assert expect["assertion_not_contains"] not in c.assertion, f"{case['case_id']}"
        else:
            assert expect["anchor_absent"] not in by_anchor or \
                by_anchor[expect["anchor_absent"]].missing_marker is True, \
                f"{case['case_id']}: {expect['anchor_absent']} 不应产出已定位候选"

    elif kind == "term":
        terms = {t.field_key: t for t in extract_term_candidates(_pages(case))}
        t = terms[expect["field"]]
        assert t.value and expect["value_contains"] in t.value, f"{case['case_id']}: {t.value!r}"

    elif kind == "discovery_classify":
        from runtime.parsing.llm_fallback import _classify_discovery
        norm = (case["quote"] or "").replace(" ", "")
        if "category_not" in expect:
            assert _classify_discovery(norm) != expect["category_not"], \
                f"{case['case_id']}: 仍被误判为 {expect['category_not']}"
        if "category" in expect:
            assert _classify_discovery(norm) == expect["category"], f"{case['case_id']}"
        else:
            assert _classify_discovery(norm) is None

    else:
        pytest.fail(f"{case['case_id']}: 未知用例类型 {kind}")
