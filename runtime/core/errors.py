# F020 §6：统一错误码与错误响应结构
# 纯逻辑模块（无第三方依赖），供路由层与测试复用。
# 错误响应统一为：{"request_id": ..., "error": {"code": ..., "message": ..., "detail": ...}}
from __future__ import annotations

from typing import Any, Optional

# 错误码 -> HTTP 状态（F020 §6 至少覆盖以下 11 类 + R025 新增）
ERROR_CODES: dict[str, int] = {
    "invalid_request": 400,
    "unauthorized": 401,          # R024：未认证/凭据无效（F020 §5.1 认证）
    "forbidden": 403,
    "not_found": 404,
    "duplicate_material": 409,
    "hash_mismatch": 409,
    "knowledge_not_ready": 409,
    "unsupported_format": 415,
    "parse_failed": 422,
    "manual_review_required": 422,
    "invalid_state_transition": 409,
    "dependency_unavailable": 503,
    "internal_error": 500,
}

VALID_CODES = frozenset(ERROR_CODES)


class ApiError(Exception):
    """业务异常：携带 F020 错误码、用户可读消息与可选细节。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: Optional[dict[str, Any]] = None,
        status_code: Optional[int] = None,
    ) -> None:
        if code not in VALID_CODES:
            raise ValueError(f"未知错误码: {code}，允许 {sorted(VALID_CODES)}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.status_code = status_code or ERROR_CODES[code]

    def as_dict(self, request_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return {"request_id": request_id, "error": body}


def error_response(request_id: str, code: str, message: str, *, detail: Optional[dict] = None) -> dict[str, Any]:
    """构造统一错误响应（测试/内部使用；路由层统一由异常处理器输出）。"""
    return ApiError(code, message, detail=detail).as_dict(request_id)