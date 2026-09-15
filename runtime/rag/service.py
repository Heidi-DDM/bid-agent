# F025 / 方案 §8.1：RAG 服务编排（索引/检索/回放的事务边界）
# 职责：
# - 索引：幂等创建 knowledge_index 任务（worker 执行 indexer.index_material）；
# - 检索：可见性 → 混合检索 → retrieval_runs 快照；
# - 回放：按 retrieval_run_id 还原候选 chunk 与过滤条件（F024 §2）。
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.core import config
from runtime.db import worker_service
from runtime.db.models import RetrievalRun
from runtime.rag.schemas import SearchRequest

logger = logging.getLogger("runtime.rag.service")


class RagServiceError(Exception):
    """RAG 服务编排错误（映射 409/503）。"""


class KnowledgeNotReadyError(RagServiceError):
    """索引/向量未就绪（409 knowledge_not_ready）。"""


def build_search_idempotency_key(request: SearchRequest, index_version: str, as_of: str) -> str:
    """方案 §8.3：search:{query_hash}:{filters_hash}:{index_version}:{as_of}"""
    q = hashlib.sha256(request.query.encode("utf-8")).hexdigest()
    f = hashlib.sha256(
        repr(
            (
                sorted(request.knowledge_layers),
                request.project_id,
                request.lot_id,
                request.top_k,
                request.retrieval_mode,
            )
        ).encode("utf-8")
    ).hexdigest()
    return hashlib.sha256(f"search:{q}:{f}:{index_version}:{as_of}".encode("utf-8")).hexdigest()


def create_index_job(session: Session, *, material_id: str, version: int,
                     project_id: str | None = None,
                     retry_failed: bool = False) -> tuple[object, bool]:
    """创建 knowledge_index 任务（幂等：方案 §8.3 index:... 键）。"""
    from runtime.rag.indexer import build_index_idempotency_key

    model_name = config.embedding_model() or "embedding"
    content_hash = ""
    if project_id:
        from runtime.db.models import Material

        material = session.get(Material, (material_id, version))
        if material is not None:
            content_hash = material.content_hash
    idx_version = f"{model_name}:{content_hash[:12]}:{version}"
    key = build_index_idempotency_key(material_id, version, content_hash, model_name, idx_version)
    return worker_service.create_job(
        session,
        kind="knowledge_index",
        input_ref=f"{material_id}:{version}",
        project_id=project_id,
        idempotency_key=key,
        retry_failed=retry_failed,
    )


def replay_run(session: Session, retrieval_run_id: str) -> dict:
    """按 retrieval_run_id 回放检索运行（候选 chunk + 过滤条件，F024 §2）。"""
    run = session.get(RetrievalRun, retrieval_run_id)
    if run is None:
        raise KeyError(f"检索运行不存在: {retrieval_run_id}")
    return {
        "retrieval_run_id": run.run_id,
        "query": run.query_text,
        "knowledge_layers": run.knowledge_layers,
        "permission_scope": run.permission_scope,
        "project_id": run.project_id,
        "lot_id": run.lot_id,
        "as_of": run.as_of,
        "top_k": run.top_k,
        "retrieval_mode": run.retrieval_mode,
        "ranking_strategy": run.ranking_strategy,
        "reranker_model": run.reranker_model,
        "index_version": run.index_version,
        "candidate_chunk_ids": run.candidate_chunk_ids,
        "latency_ms": run.latency_ms,
        "status": run.status,
    }


def index_status(session: Session, material_id: str, version: int) -> dict:
    """材料版本索引状态（GET /knowledge/indexes/{material_id}/{version}）。"""
    from runtime.db.models import KnowledgeChunk

    chunks = session.scalars(
        select(KnowledgeChunk).where(
            KnowledgeChunk.material_id == material_id,
            KnowledgeChunk.material_version == version,
        )
    ).all()
    current = [c for c in chunks if c.index_status == "current"]
    stale = [c for c in chunks if c.index_status == "stale"]
    return {
        "material_id": material_id,
        "material_version": version,
        "indexed": len(current) > 0,
        "chunk_count": len(chunks),
        "current_count": len(current),
        "stale_count": len(stale),
        "index_version": current[0].index_version if current else None,
        "embedding_model": current[0].embedding_model if current else None,
    }