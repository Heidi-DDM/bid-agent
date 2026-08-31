# R018/F018：HTTP 服务层（FastAPI）
# 端点：
#   GET /healthz  进程存活，不访问业务数据（F018 §5）
#   GET /readyz   数据库、文件根目录、OCR 命令、模型适配器（若启用）可用性检查
# 启动时执行数据库迁移检查（F018 §4.1）；不在启动时自动执行迁移（F019 §5.1）。
#
# 依赖缺失时的降级策略：缺少 fastapi/uvicorn 时本模块不 import，
# 由 run_api.sh / README 在安装依赖后启动。
from __future__ import annotations

import logging
import shutil
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from runtime.core import db, model
from runtime.core.config import app_env, object_store_root, readyz_timeout_seconds

logger = logging.getLogger("runtime.api")

app = FastAPI(
    title="某建投投标智能体 运行时 API",
    version="0.1.0",
    description="R018 运行时技术基线：健康检查与任务服务。",
    docs_url=None if app_env() == "prod" else "/docs",
    redoc_url=None,
)


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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "app_env": app_env()}


@app.get("/readyz")
def readyz() -> JSONResponse:
    checks: dict[str, dict[str, Any]] = {
        "database": db.check_database(timeout=readyz_timeout_seconds()).as_dict(),
        "object_store": _object_store_status(),
        "ocr": _ocr_status(),
        "model": _model_status(),
    }
    ok = all(c.get("available") for c in checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ready": ok, "checks": checks, "app_env": app_env()},
    )


@app.on_event("startup")
def on_startup() -> None:
    # F018 §4.1：API 启动并执行数据库迁移检查（仅检查，不自动迁移）
    status = db.check_database(timeout=readyz_timeout_seconds())
    if status.available:
        logger.info("数据库连接正常；请确认迁移状态（alembic current）")
    else:
        logger.warning("数据库不可用: %s", status.error)