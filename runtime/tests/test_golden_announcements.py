# -*- coding: utf-8 -*-
# P3：公告抽取 golden 回归（docs/10 §5 P3，2026-09-11）
#
# 语料：runtime/tests/golden/announcements/*.json（首期 6 案：河北工大（二次）用户报告案例、
# 惠招标太行冷链、唐山三友 EPC、询比采购、联合体极性矩阵 13 变体、附录 A 快赢批次 17 变体）。
# 每个 body 带 body_sha256 锁定指纹——语料被静默改动会立刻红（须显式重算指纹，保证可复现）。
#
# 断言三层（不变量式，非逐字对答案——将来优化取值边界只要不变量不破即不误报）：
#   ① 关键子项包含（资质必须含「安全生产许可证」等，0 静默截断红线）；
#   ② quote round-trip：固化文本[start:end] == quote 且 assertion == 折叠(quote)（逐字红线）；
#   ③ 极性字段 enum 正确（0 反转红线）。
# golden 为离线 CI 回归，不参与线上对新公告的抽取（新说法路径：未命中→待补→人工→回流成新题）。
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from runtime.parsing.provenance import build_detail_summary

GOLDEN_DIR = Path(__file__).parent / "golden" / "announcements"
_WS = re.compile(r"\s+")

_CASES = sorted(GOLDEN_DIR.glob("*.json"))


def _load_cases():
    """展平为 [(case_id, 变体序号, title, body, body_sha256, expect)]。"""
    out = []
    for p in _CASES:
        data = json.loads(p.read_text(encoding="utf-8"))
        variants = data.get("variants")
        if variants:
            for i, v in enumerate(variants):
                out.append(pytest.param(
                    data["case_id"], i, v.get("title") or data["title"], v["body"],
                    v["body_sha256"], v["expect"],
                    id=f"{data['case_id']}[{i}]"))
        else:
            out.append(pytest.param(
                data["case_id"], None, data["title"], data["body"],
                data["body_sha256"], data["expect"],
                id=data["case_id"]))
    return out


def _check_expected(case_label: str, key: str, spec: dict, d: dict | None, body: str):
    where = f"{case_label}.{key}"
    if spec.get("absent"):
        assert d is None, f"{where}: 应不产出（判不出极性/未载明），实际 {d and d['value']!r}"
        return
    assert d is not None, f"{where}: 字段未产出（漏采）"
    if spec.get("missing"):
        assert d.get("missing") is True, f"{where}: 应 missing（不推断），实际 value={d.get('value')!r}"
    else:
        assert not d.get("missing"), f"{where}: 不应 missing，实际 {d}"
    if "value" in spec:
        assert d.get("value") == spec["value"], f"{where}: value={d.get('value')!r} 期望 {spec['value']!r}"
    for sub in spec.get("contains", []):
        assert sub in (d.get("value") or ""), f"{where}: value 应含 {sub!r}，实际 {d.get('value')!r}"
    for sub in spec.get("not_contains", []):
        assert sub not in (d.get("value") or ""), f"{where}: value 不应含 {sub!r}（跨条款吞并）"
    for end in (spec.get("endswith"),):
        if end:
            assert (d.get("value") or "").endswith(end), f"{where}: value 应以 {end!r} 结尾"
    start = spec.get("startswith")
    if start:
        assert (d.get("value") or "").startswith(start), f"{where}: value 应以 {start!r} 开头"
    if "enum" in spec:
        assert d.get("enum") == spec["enum"], f"{where}: 极性反转！enum={d.get('enum')} 期望 {spec['enum']}"
    quote = d.get("quote")
    if spec.get("quote"):
        assert quote == spec["quote"], f"{where}: quote={quote!r} 期望 {spec['quote']!r}"
    for kw, attr in (("quote_startswith", "quote"), ("quote_endswith", "quote")):
        want = spec.get(kw)
        if want:
            assert (quote or "").startswith(want) if kw == "quote_startswith" else (quote or "").endswith(want), \
                f"{where}: quote={quote!r} 应{'以' if kw == 'quote_startswith' else '以…结尾'} {want!r}"


@pytest.mark.parametrize("case_id,variant,title,body,body_sha256,expect", _load_cases())
def test_golden_announcement(case_id, variant, title, body, body_sha256, expect):
    label = f"{case_id}" + (f"[{variant}]" if variant is not None else "")
    # 语料锁定：正文被静意改动（指纹不匹配）立刻失败，防止期望值与语料漂移
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == body_sha256, \
        f"{label}: body_sha256 不匹配——语料被改动，请显式重算并复核期望值"

    detail = build_detail_summary(title, body, content_hash=body_sha256)

    # 各字段期望（三层断言，见模块注释）
    for key, spec in expect.items():
        _check_expected(label, key, spec, detail.get(key), body)

    # 全字段不变量：quote 逐字 round-trip + assertion == 折叠(quote) + golden 案例零复核标记
    for key, d in detail.items():
        start, end, quote = d.get("start"), d.get("end"), d.get("quote")
        if start is None or end is None or quote is None:
            assert d.get("missing") or d.get("clause") == "标题" or not d.get("value"), \
                f"{label}.{key}: 有值但无偏移（应只有标题源与 missing 可无偏移）"
            continue
        assert body[start:end] == quote, f"{label}.{key}: round-trip 失败 text[{start}:{end}] != quote"
        assert _WS.sub(" ", quote).strip() == (d.get("assertion") or "").strip(), \
            f"{label}.{key}: assertion 与 quote 折叠后不一致"
        assert d.get("review") is None, f"{label}.{key}: 不应触发 quote_not_verbatim 复核标记"
