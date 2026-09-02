# F020 §2/§7：任务编排规则纯逻辑测试
# 覆盖：未上传完整招标文件不得建解析任务、解析成功只触发一次首次匹配、
# 结果页只读不隐式创建 run、补录核验后重算幂等、非满分不得创建审批。
from __future__ import annotations

import pytest

from runtime.core import orchestration
from runtime.core.orchestration import OrchestrationError


def test_ensure_tender_document_present_ok():
    materials = [
        {"material_type": "announcement", "parse_status": "pending"},
        {"material_type": "tender_document", "parse_status": "parsed"},
    ]
    orchestration.ensure_tender_document_present(materials)  # 不抛


def test_ensure_tender_document_present_missing_raises():
    materials = [{"material_type": "announcement", "parse_status": "pending"}]
    with pytest.raises(OrchestrationError):
        orchestration.ensure_tender_document_present(materials)


def test_ensure_tender_document_present_empty_raises():
    with pytest.raises(OrchestrationError):
        orchestration.ensure_tender_document_present([])


def test_should_trigger_first_match_only_once():
    materials = [{"material_type": "tender_document", "parse_status": "parsed"}]
    assert orchestration.should_trigger_first_match(materials, existing_runs=0) is True
    # 已有 run -> 不再触发
    assert orchestration.should_trigger_first_match(materials, existing_runs=1) is False


def test_should_trigger_first_match_requires_parsed():
    materials = [{"material_type": "tender_document", "parse_status": "pending"}]
    assert orchestration.should_trigger_first_match(materials, existing_runs=0) is False
    # 解析失败/部分解析不触发（manual_review 不静默跳过）
    for status in ("failed", "partial", "manual_review"):
        materials[0]["parse_status"] = status
        assert orchestration.should_trigger_first_match(materials, 0) is False


def test_result_query_is_readonly():
    assert orchestration.result_query_is_readonly() is True


def test_recalculate_idempotent_same_evidence():
    latest = {"evidence_version": "ev-1"}
    assert orchestration.ensure_recalculate_idempotent("ev-1", latest) is True
    assert orchestration.ensure_recalculate_idempotent("ev-2", latest) is False
    assert orchestration.ensure_recalculate_idempotent("ev-1", None) is False


def test_admission_eligible_for_approval():
    assert orchestration.admission_eligible_for_approval({"internal_admission_eligible": True}) is True
    assert orchestration.admission_eligible_for_approval({"internal_admission_eligible": False}) is False
    assert orchestration.admission_eligible_for_approval({}) is False


def test_parse_event_idempotent():
    processed: set[str] = set()
    assert orchestration.parse_event_idempotent("evt-1", processed) is True
    assert orchestration.parse_event_idempotent("evt-1", processed) is False  # 幂等
    assert orchestration.parse_event_idempotent("evt-2", processed) is True


def test_recalculate_marks_previous_result_stale_in_service_contract():
    # The database implementation is exercised by the integration suite; this assertion
    # documents the required service entry point for the route-level contract.
    from runtime.db import api_service

    assert callable(api_service.mark_admission_results_stale)
