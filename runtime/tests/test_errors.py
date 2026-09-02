# F020 §6：统一错误码与错误响应结构测试
from __future__ import annotations

import pytest

from runtime.core.errors import ApiError, ERROR_CODES, error_response


def test_error_codes_coverage():
    # F020 §6 至少 11 类错误码
    required = {
        "invalid_request", "forbidden", "not_found", "duplicate_material",
        "hash_mismatch", "unsupported_format", "parse_failed",
        "manual_review_required", "invalid_state_transition",
        "dependency_unavailable", "internal_error",
    }
    assert required <= set(ERROR_CODES)


def test_http_status_mapping():
    assert ERROR_CODES["invalid_request"] == 400
    assert ERROR_CODES["forbidden"] == 403
    assert ERROR_CODES["not_found"] == 404
    assert ERROR_CODES["unsupported_format"] == 415
    assert ERROR_CODES["invalid_state_transition"] == 409
    assert ERROR_CODES["internal_error"] == 500


def test_api_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        ApiError("unknown_code", "msg")


def test_api_error_as_dict():
    err = ApiError("not_found", "项目不存在: ND-2025")
    body = err.as_dict("r-1")
    assert body["request_id"] == "r-1"
    assert body["error"] == {"code": "not_found", "message": "项目不存在: ND-2025"}
    assert err.status_code == 404


def test_api_error_with_detail():
    err = ApiError("invalid_request", "参数错误", detail={"field": "comment"})
    body = err.as_dict("r-2")
    assert body["error"]["detail"] == {"field": "comment"}


def test_error_response_helper():
    body = error_response("r-3", "forbidden", "无权限")
    assert body["request_id"] == "r-3"
    assert body["error"]["code"] == "forbidden"