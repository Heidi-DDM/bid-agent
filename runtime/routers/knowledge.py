# R025/F025：知识库 API（docs/07 方案 §3.4）
# POST /api/v1/knowledge/search                         —— 权限过滤的混合检索（candidate_only）
# GET  /api/v1/knowledge/indexes/{material_id}/{version}—— 材料版本索引状态（只读）
# GET  /api/v1/retrieval-runs/{retrieval_run_id}        —— 检索运行回放（只读）
# 约束：
# - 查询必须显式提供 knowledge_layers；L2/L3 必须 project_id + as_of（422）；
# - 越权 403 + 审计；索引未就绪 409 knowledge_not_ready；服务异常 503 retryable；
# - 响应永远 candidate_only=true，禁止 satisfied/qualified/approved_for_bidding 等判定字段；
# - 任何 GET/POST 检索不得创建 match run 或改变项目状态（F020 §2.1）。
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from runtime.core.errors import ApiError
from runtime.db import api_service
from runtime.rag import filters, retriever, service
from runtime.rag.filters import Actor, VisibilityError
from runtime.rag.indexer import _embed as _query_embed  # 真实查询向量（本地 bge-m3，服务不可用由 retriever 降级 keyword）
from runtime.rag.retriever import KnowledgeNotReadyError, RetrievalError
from runtime.rag.schemas import SearchRequest, SearchResponse
from runtime.rag.service import RagServiceError
from runtime.routers.deps import get_actor, get_db, get_request_id, get_role

logger = logging.getLogger("runtime.routers.knowledge")

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


@router.post("/knowledge/search")
def knowledge_search(
    body: SearchRequest,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> SearchResponse:
    """权限过滤的混合检索（方案 §9.1/§9.2）。"""
    # 角色可见层与权限推导（fail-closed）
    try:
        vis = filters.build_visibility_predicate(Actor(role=role, actor=actor), body)
    except VisibilityError as exc:
        api_service.audit(
            session, actor=actor, action="knowledge.search_denied",
            basis=f"layers={body.knowledge_layers}",
            outcome=str(exc), object_ref=body.project_id,
        )
        session.commit()
        raise ApiError("forbidden", str(exc))
    if body.requires_project_context() and (not body.project_id or not body.as_of):
        raise ApiError("invalid_request", "L2/L3 检索必须提供 project_id 与 as_of（方案 §3.4）")

    try:
        response = retriever.hybrid_search(
            session, body, vis, role=role, request_id=request_id,
            embed_query_fn=_query_embed,
        )
    except KnowledgeNotReadyError as exc:
        raise ApiError("knowledge_not_ready", str(exc))
    except RetrievalError as exc:
        logger.warning("检索服务异常 %s: %s", type(exc).__name__, exc)
        raise ApiError("dependency_unavailable", "检索服务暂不可用（retryable）")
    api_service.audit(
        session, actor=actor, action="knowledge.search",
        basis=f"layers={sorted(vis.layers)} scopes={sorted(vis.scopes)}",
        outcome=f"runs={response.retrieval_run_id} items={len(response.items)}",
        object_ref=body.project_id,
    )
    session.commit()
    return response


@router.get("/knowledge/indexes/{material_id}/{version}")
def knowledge_index_status(
    material_id: str,
    version: int,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """材料版本索引状态（只读，不创建索引任务；方案 §3.4）。"""
    from runtime.core import rbac

    if not rbac.has_permission(role, "material", "read"):
        raise ApiError("forbidden", f"角色 {role} 无 material:read 权限")
    status = service.index_status(session, material_id, version)
    status["request_id"] = request_id
    return status


@router.get("/retrieval-runs/{retrieval_run_id}")
def retrieval_run_detail(
    retrieval_run_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """检索运行回放（只读；方案 §3.4 回放候选条款/证据）。"""
    try:
        run = service.replay_run(session, retrieval_run_id)
    except KeyError:
        raise ApiError("not_found", f"检索运行不存在: {retrieval_run_id}")
    run["request_id"] = request_id
    return run