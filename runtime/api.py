# R018/F018 + R020/F020：HTTP 服务层（FastAPI）
# 端点：
#   GET  /healthz                进程存活，不访问业务数据（F018 §5）
#   GET  /readyz                 数据库、文件根目录、OCR 命令、模型适配器（若启用）可用性检查
#   /api/v1/**                   F020 业务 API（材料/搜索推送/入库/解析/匹配/队列/审批/审计/企业资料）
# 启动时执行数据库迁移检查（F018 §4.1）；不在启动时自动执行迁移（F019 §5.1）。
#
# 依赖缺失时的降级策略：缺少 fastapi/uvicorn 时本模块不 import，
# 由 run_api.sh / README 在安装依赖后启动。
from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from runtime.routers import approvals, auth, enterprise, intake, knowledge, materials, match, ocr, parse, projects
from runtime.routers.deps import request_id
from runtime.core import db, model
from runtime.core.config import app_env, object_store_root, readyz_timeout_seconds
from runtime.core.errors import ApiError

logger = logging.getLogger("runtime.routers")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # F018 §4.1：API 启动并执行数据库迁移检查（仅检查，不自动迁移）
    status = db.check_database(timeout=readyz_timeout_seconds())
    if status.available:
        logger.info("数据库连接正常；请确认迁移状态（alembic current）")
    else:
        logger.warning("数据库不可用: %s", status.error)
    yield


app = FastAPI(
    title="某建投投标智能体 运行时 API",
    version="0.1.0",
    description="R018 运行时技术基线 + R020 业务 API：健康检查、任务服务、材料/搜索推送/入库/解析/匹配/审批/审计。",
    docs_url=None if app_env() == "prod" else "/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# 挂载 F020 业务路由
app.include_router(auth.router)
app.include_router(materials.router)
app.include_router(intake.router)
app.include_router(projects.router)
app.include_router(match.router)
app.include_router(approvals.router)
app.include_router(enterprise.router)
app.include_router(ocr.router)
app.include_router(knowledge.router)
app.include_router(parse.router)


# ---------- 统一错误处理（F020 §6） ----------

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    rid = request_id(request)
    return JSONResponse(status_code=exc.status_code, content=exc.as_dict(rid))


@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError) -> JSONResponse:
    rid = request_id(request)
    body = {"code": "forbidden", "message": str(exc)}
    return JSONResponse(status_code=403, content={"request_id": rid, "error": body})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    rid = request_id(request)
    body = {
        "code": "invalid_request",
        "message": "请求参数校验失败",
        "detail": exc.errors(),
    }
    return JSONResponse(status_code=422, content={"request_id": rid, "error": body})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = request_id(request)
    logger.exception("未处理异常 %s %s: %s", request.method, request.url.path, type(exc).__name__)
    body = {"code": "internal_error", "message": "服务内部错误，请稍后重试"}
    return JSONResponse(status_code=500, content={"request_id": rid, "error": body})


# ---------- 健康检查（R018） ----------

def _object_store_status() -> dict[str, Any]:
    root = object_store_root()
    try:
        from pathlib import Path

        Path(root).mkdir(parents=True, exist_ok=True)
        probe = Path(root) / ".readyz-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"available": True, "root": root}
    except OSError as exc:
        return {"available": False, "root": root, "error": f"{type(exc).__name__}: {exc}"}


def _ocr_status() -> dict[str, Any]:
    pdftotext = shutil.which("pdftotext")
    tesseract = shutil.which("tesseract")
    return {
        "pdftotext": bool(pdftotext),
        "tesseract": bool(tesseract),
        "available": bool(pdftotext and tesseract),
    }


def _model_status() -> dict[str, Any]:
    if not model.check_model_allowed():
        # 模型未配置时不参与就绪判定（F018 §5“模型适配器（若启用）”；
        # 已配置但不可达才算失败，见 runtime/README.md §8）
        return {"enabled": False, "available": True, "reason": "未配置 MODEL_BASE_URL，跳过检查"}
    result = model.health(timeout=readyz_timeout_seconds())
    return {"enabled": True, "available": result.ok, "error": result.error}


def _knowledge_status() -> dict[str, Any]:
    """RAG 就绪检查（docs/07 方案 §3.1）：pgvector、本地 embedding、DeepSeek public-only。

    - PGVECTOR_ENABLED=true 时探测数据库 pgvector 扩展，缺失/不可达视为未就绪；
    - 未配置 EMBEDDING_MODEL 视为未启用（跳过）；已启用但本地模型不可达视为未就绪；
    - DEEPSEEK_PUBLIC_ONLY=false 或 DEEPSEEK_BASE_URL 缺失时 /readyz 失败（fail-closed）。
    """
    from runtime.core import config

    status: dict[str, Any] = {
        "pgvector": {"enabled": config.pgvector_enabled()},
        "embedding": {"enabled": bool(config.embedding_model())},
        "deepseek_public_only": {"enabled": config.deepseek_enabled()},
    }
    if config.pgvector_enabled():
        try:
            from sqlalchemy import create_engine, text

            engine = create_engine(config.database_url(), pool_pre_ping=True)
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                ).first()
            engine.dispose()
            status["pgvector"]["available"] = row is not None
            if row is None:
                status["pgvector"]["error"] = "pgvector 扩展未安装（CREATE EXTENSION vector）"
        except Exception as exc:
            status["pgvector"]["available"] = False
            status["pgvector"]["error"] = f"{type(exc).__name__}: {exc}"
    else:
        status["pgvector"]["available"] = True
        status["pgvector"]["reason"] = "PGVECTOR_ENABLED=false，跳过检查"
    if config.embedding_model():
        result = model.health(timeout=readyz_timeout_seconds())
        status["embedding"]["available"] = result.ok
        if result.error:
            status["embedding"]["error"] = result.error
    else:
        status["embedding"]["available"] = True
        status["embedding"]["reason"] = "未配置 EMBEDDING_MODEL，跳过检查"
    if config.deepseek_enabled():
        ok = model.check_deepseek_allowed()
        status["deepseek_public_only"]["available"] = ok
        if not ok:
            status["deepseek_public_only"]["error"] = (
                "DEEPSEEK_PUBLIC_ONLY=false 或 DEEPSEEK_BASE_URL 未配置/不合规"
            )
    else:
        status["deepseek_public_only"]["available"] = True
        status["deepseek_public_only"]["reason"] = "DEEPSEEK_ENABLED=false，跳过检查"
    return status


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "app_env": app_env()}


def _check_group_ok(c: dict[str, Any]) -> bool:
    """就绪聚合：knowledge 为嵌套检查组（pgvector/embedding/deepseek_public_only），
    子项全部 available 才算就绪；其余检查项看自身 available。"""
    if not c.get("available") and any("available" in v for v in c.values() if isinstance(v, dict)):
        return all(v.get("available") for v in c.values() if isinstance(v, dict))
    return bool(c.get("available"))


@app.get("/readyz")
def readyz() -> JSONResponse:
    checks: dict[str, dict[str, Any]] = {
        "database": db.check_database(timeout=readyz_timeout_seconds()).as_dict(),
        "object_store": _object_store_status(),
        "ocr": _ocr_status(),
        "model": _model_status(),
        "knowledge": _knowledge_status(),
    }
    ok = all(_check_group_ok(c) for c in checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ready": ok, "checks": checks, "app_env": app_env()},
    )
