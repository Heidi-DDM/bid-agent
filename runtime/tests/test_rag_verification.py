# F025 §6 / docs/07 方案 §3.5：结构化核验纯逻辑测试（verification.py）
# 覆盖：金额/日期/等级/有效期/哈希核验的 passed/failed/blocked 三分；
# 无候选/无法解析 → blocked（F008 缺失阻断，不得推断满足）。
from __future__ import annotations

from decimal import Decimal

import pytest

from runtime.rag.verification import (
    verify_amount_ge,
    verify_candidates,
    verify_date_window,
    verify_hash,
    verify_level_ge,
    verify_validity,
)

pytestmark = pytest.mark.rag


def test_verify_amount_ge_passed():
    item = verify_amount_ge("MAT-1", "8000万元", Decimal("50000000"))
    assert item.passed is True
    assert item.field == "amount"


def test_verify_amount_ge_failed():
    item = verify_amount_ge("MAT-1", "3000万元", Decimal("50000000"))
    assert item.passed is False
    assert item.note == ""  # 明确不满足，非待补


def test_verify_amount_ge_unparseable_blocked():
    # 中文大写金额无法解析 → blocked（待核实），不得推断
    item = verify_amount_ge("MAT-1", "壹仟万元整", Decimal("50000000"))
    assert item.passed is False
    assert "无法解析" in item.note


def test_verify_level_ge_semantics():
    assert verify_level_ge("MAT-1", "一级", "二级").passed is True
    assert verify_level_ge("MAT-1", "二级", "一级").passed is False
    assert verify_level_ge("MAT-1", "贰级", "二级").passed is True  # 大写归一化
    unknown = verify_level_ge("MAT-1", "待核实", "二级")
    assert unknown.passed is False
    assert "无法判定" in unknown.note


def test_verify_date_window():
    assert verify_date_window("MAT-1", "2025-10-30", after="2022-09-01").passed is True
    out = verify_date_window("MAT-1", "2020-05-01", after="2022-09-01")
    assert out.passed is False
    blocked = verify_date_window("MAT-1", "近期", after="2022-09-01")
    assert blocked.passed is False
    assert "无法解析" in blocked.note


def test_verify_validity_expired_failed():
    item = verify_validity("MAT-1", valid_until="2025-01-01T00:00:00+00:00", as_of="2025-10-30")
    assert item.passed is False
    assert "过期" in item.note


def test_verify_validity_not_yet_effective_failed():
    item = verify_validity("MAT-1", valid_from="2026-01-01T00:00:00+00:00", as_of="2025-10-30")
    assert item.passed is False
    assert "未生效" in item.note


def test_verify_validity_in_range_passed():
    item = verify_validity(
        "MAT-1",
        valid_from="2024-01-01T00:00:00+00:00",
        valid_until="2026-01-01T00:00:00+00:00",
        as_of="2025-10-30",
    )
    assert item.passed is True


def test_verify_hash():
    assert verify_hash("MAT-1", "a" * 64, "a" * 64).passed is True
    assert verify_hash("MAT-1", "b" * 64, "a" * 64).passed is False
    missing = verify_hash("MAT-1", None, "a" * 64)
    assert missing.passed is False
    assert "缺失哈希" in missing.note


def test_verify_candidates_all_passed():
    candidates = [{"evidence_ref": "MAT-1", "level": "一级", "amount": "80000000.00",
                   "date": "2024-06-01", "valid_from": "2024-01-01", "valid_until": "2026-01-01",
                   "content_hash": "a" * 64}]
    result = verify_candidates(candidates, required={
        "level_min": "二级", "amount_min": Decimal("50000000"),
        "date_after": "2022-09-01", "valid_as_of": "2025-10-30",
        "content_hash": "a" * 64,
    })
    assert result.all_passed is True
    assert len(result.passed) >= 5  # 五项约束全部通过


def test_verify_candidates_explicit_failure():
    candidates = [{"evidence_ref": "MAT-1", "level": "三级"}]
    result = verify_candidates(candidates, required={"level_min": "二级"})
    assert result.all_passed is False
    # 明确不满足进 failed（not_satisfied 语义），不得归为待补
    assert result.failed and result.failed[0].passed is False
    assert result.blocked == []


def test_verify_candidates_no_candidates_blocked():
    result = verify_candidates([], required={"level_min": "二级"})
    assert result.all_passed is False
    assert result.blocked and "无候选证据" in result.blocked[0].note


def test_verify_candidates_unparseable_blocked():
    candidates = [{"evidence_ref": "MAT-1", "level": "待核实"}]
    result = verify_candidates(candidates, required={"level_min": "二级"})
    assert result.all_passed is False
    assert result.blocked and result.failed == []