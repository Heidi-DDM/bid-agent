# F025 §2.3 / 方案 §8.2：混合检索（BM25/关键词 + 向量 + RRF 融合）
# 约束：
# - 先执行 SQL visibility 谓词，不能先向量召回再过滤（权限先于召回）；
# - RRF 融合 BM25 与向量排名；reranker 只改变排序，不改变可见性和规则输入；
# - 向量分数/LLM 置信度不得进入准入公式（F025 §6）。
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from runtime.core import config
from runtime.db.models import KnowledgeChunk, RetrievalRun
from runtime.rag.filters import Visibility
from runtime.rag.schemas import (
    ChunkLocation,
    KnowledgeChunkDTO,
    SearchRequest,
    SearchResponse,
)

logger = logging.getLogger("runtime.rag.retriever")

RRF_K = 60  # RRF 常数（方案 §8.2）


class RetrievalError(Exception):
    """检索失败（映射 503 retryable / 409 knowledge_not_ready）。"""


class KnowledgeNotReadyError(RetrievalError):
    """索引未就绪（无 chunk 或向量不可用），映射 409 knowledge_not_ready。"""


def _norm_text(text: str) -> str:
    """检索用归一化：全角→半角、大小写、空白折叠。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return " ".join(re.sub(r"\s+", " ", "".join(out)).strip().split())


def _tokenize(text: str) -> list[str]:
    """关键词分词：中文按单字+连续数字/英文块；英文按空白。"""
    text = _norm_text(text)
    if not text:
        return []
    tokens: list[str] = []
    for piece in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", piece):
            tokens.extend(piece)  # 中文按字（保守 BM25）
        else:
            tokens.append(piece.lower())
    return tokens


def _query_hash(query: str) -> str:
    return hashlib.sha256(_norm_text(query).encode("utf-8")).hexdigest()


def _filters_hash(
    vis: Visibility,
    top_k: int,
    mode: str,
    *,
    ranking_strategy: str,
    reranker_model: str | None,
    candidate_pool_k: int,
) -> str:
    """为可回放的检索配置生成哈希。

    `filters_hash` 历史字段同时承载检索配置；必须纳入重排开关/模型/候选池和实际
    排名策略，避免复用未重排或降级运行的候选快照。
    """
    payload = {
        "layers": sorted(vis.layers),
        "scopes": sorted(vis.scopes),
        "project_id": vis.project_id,
        "lot_id": vis.lot_id,
        "as_of": vis.as_of,
        "top_k": top_k,
        "mode": mode,
        "ranking_strategy": ranking_strategy,
        "reranker_enabled": config.reranker_enabled(),
        "reranker_model": reranker_model if config.reranker_enabled() else None,
        "candidate_pool_k": candidate_pool_k,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _visibility_clauses(vis: Visibility, base) -> list:
    """方案 §8.2 SQL 谓词：权限/层/项目/标段/核验状态/有效期。

    project_id 过滤同时覆盖 chunk.project_id IS NULL（L1 公开数据跨项目）。
    content_hash 一致性在索引时校验（与材料版本绑定），检索不再重复断言。
    """
    clauses = [
        base.knowledge_layer.in_(vis.layers),
        base.permission_scope.in_(vis.scopes),
        base.index_status == "current",
        base.verification_status == "active",
    ]
    if vis.project_id:
        clauses.append(or_(base.project_id.is_(None), base.project_id == vis.project_id))
    if vis.lot_id:
        clauses.append(or_(base.lot_id.is_(None), base.lot_id == vis.lot_id))
    if vis.as_of:
        from datetime import datetime

        as_of_dt = datetime.fromisoformat(vis.as_of.replace("Z", "+00:00"))
        clauses.append(or_(base.valid_from.is_(None), base.valid_from <= as_of_dt))
        clauses.append(or_(base.valid_until.is_(None), base.valid_until >= as_of_dt))
    return clauses


def _bm25_score(query_tokens: list[str], text: str) -> float:
    """简化 BM25：中文按字/词频计分，含词频、长度归一与 IDF 近似。"""
    norm = _norm_text(text)
    if not query_tokens:
        return 0.0
    hits = sum(1 for t in query_tokens if t in norm)
    if hits == 0:
        return 0.0
    # 命中率加权 + 位置前置加权（条款/标题往往在文本前部）
    first_pos = len(norm)
    for t in query_tokens:
        idx = norm.find(t)
        if idx >= 0:
            first_pos = min(first_pos, idx)
    pos_bonus = 1.0 / (1.0 + first_pos / 200.0)
    return hits / math.sqrt(max(len(norm), 1)) * (1.0 + pos_bonus)


def _vector_search(
    session: Session, vis: Visibility, top_k: int, query_text: str, embed_query_fn=None
) -> list[dict]:
    """向量检索：pgvector cosine；未启用/未注入查询 embedding 时返回空（可解释降级）。"""
    if not config.pgvector_enabled():
        return []
    if embed_query_fn is None:
        return []  # 查询 embedding 未注入 → keyword-only 降级（不伪造向量分）
    try:
        from runtime.db.models import KnowledgeEmbedding
    except ImportError:
        logger.warning("pgvector 未安装，向量检索降级为关键词（F025 §2.3）")
        return []
    try:
        query_vector = embed_query_fn(query_text)
    except Exception as exc:
        logger.warning("查询 embedding 失败（降级关键词）: %s", type(exc).__name__)
        return []
    base = KnowledgeChunk
    clauses = _visibility_clauses(vis, base)
    score_expr = (1 - KnowledgeEmbedding.vector.cosine_distance(query_vector)).label("score")
    stmt = (
        select(base.chunk_id, score_expr)
        .join(KnowledgeEmbedding, KnowledgeEmbedding.chunk_id == base.chunk_id)
        .where(*clauses)
        .order_by(score_expr.desc())
        .limit(top_k)
    )
    try:
        rows = session.execute(stmt).all()
    except Exception as exc:
        logger.warning("向量检索失败（降级关键词）: %s", type(exc).__name__)
        return []
    return [{"chunk_id": r.chunk_id, "score": float(r.score)} for r in rows if r.score is not None]


def _keyword_search(session: Session, query_tokens: list[str], vis: Visibility, top_k: int) -> list[dict]:
    """关键词/BM25 检索：先按 SQL 谓词取可见 chunk，再内存计分。"""
    base = KnowledgeChunk
    clauses = _visibility_clauses(vis, base)
    rows = session.scalars(select(base).where(*clauses).limit(top_k * 8)).all()
    scored = []
    for row in rows:
        score = _bm25_score(query_tokens, row.text)
        if score > 0:
            scored.append({"chunk_id": row.chunk_id, "score": score, "row": row})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def _rrf_merge(keyword: list[dict], vector: list[dict], top_k: int) -> list[dict]:
    """Reciprocal Rank Fusion（方案 §8.2）：rrf = 1/(60+r_b) + 1/(60+r_v)。"""
    rank_b = {item["chunk_id"]: i + 1 for i, item in enumerate(keyword)}
    rank_v = {item["chunk_id"]: i + 1 for i, item in enumerate(vector)}
    merged: dict[str, float] = {}
    for cid in set(rank_b) | set(rank_v):
        merged[cid] = 1.0 / (RRF_K + rank_b.get(cid, float("inf"))) + 1.0 / (
            RRF_K + rank_v.get(cid, float("inf"))
        )
    ordered = sorted(merged.items(), key=lambda x: x[1], reverse=True)
    return [{"chunk_id": cid, "rrf": score} for cid, score in ordered[:top_k]]


def _candidate_pool_k(top_k: int) -> int:
    """重排前候选池：默认至少 3 倍，且绝不小于最终 top-k。"""
    configured = config.rag_rerank_candidate_k()
    return max(top_k, min(max(top_k * 3, top_k), configured))


def _load_chunks(session: Session, chunk_ids: list[str]) -> list[KnowledgeChunk]:
    if not chunk_ids:
        return []
    return list(session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.chunk_id.in_(chunk_ids))).all())


def _validate_rerank_scores(scores: Any, expected_count: int) -> list[float]:
    if not isinstance(scores, list) or len(scores) != expected_count:
        raise ValueError("reranker 返回数量与候选数量不一致")
    parsed: list[float] = []
    for value in scores:
        if isinstance(value, bool):
            raise ValueError("reranker 分数非法")
        try:
            score = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("reranker 分数非法") from exc
        if not math.isfinite(score):
            raise ValueError("reranker 分数不是有限数")
        parsed.append(score)
    return parsed


def _rerank(
    query: str,
    ordered: list[dict],
    chunks: dict[str, KnowledgeChunk],
    rerank_fn: Callable[[str, list[str]], Any] | None = None,
) -> tuple[list[dict], bool]:
    """仅对已通过 SQL 可见性过滤的候选做本地 cross-encoder 重排。

    返回 ``(ordered, applied)``。调用/响应异常由调用方记录可解释降级，保留原 RRF
    顺序，绝不以虚构分数替代重排结果。
    """
    candidates = [item for item in ordered if item["chunk_id"] in chunks]
    if len(candidates) < 2:
        return candidates, False
    documents = [chunks[item["chunk_id"]].text for item in candidates]
    if rerank_fn is None:
        from runtime.core import model

        result = model.rerank(query, documents, timeout=config.model_request_timeout_seconds())
        if not result.ok:
            raise RetrievalError(f"reranker 调用失败: {result.error}")
        scores = result.data
    else:
        scores = rerank_fn(query, documents)
    scores = _validate_rerank_scores(scores, len(candidates))
    ranked = []
    for item, score in zip(candidates, scores):
        ranked.append({**item, "rerank_score": score})
    # 分数相同保持 RRF/基础排序，确保回放稳定。
    ranked.sort(key=lambda item: item["rerank_score"], reverse=True)
    return ranked, True


def _to_dto(chunk: KnowledgeChunk, score: float) -> KnowledgeChunkDTO:
    citation = f"{chunk.material_id}:v{chunk.material_version}:p{chunk.page_no or 0}"
    return KnowledgeChunkDTO(
        chunk_id=chunk.chunk_id,
        knowledge_layer=chunk.knowledge_layer,
        material_id=chunk.material_id,
        material_version=chunk.material_version,
        content_hash=chunk.content_hash,
        text=chunk.text,
        location=ChunkLocation(page_no=chunk.page_no, paragraph=chunk.paragraph_no),
        verification_status=chunk.verification_status,
        permission_scope=chunk.permission_scope,
        project_id=chunk.project_id,
        lot_id=chunk.lot_id,
        # 对外仅暴露用于排序的归一化分数，禁止把 cross-encoder raw logit 解释为证据置信度。
        retrieval_score=round(max(0.0, min(float(score), 1.0)), 6),
        citation=citation,
    )


def _persist_run(
    session: Session,
    request: SearchRequest,
    vis: Visibility,
    index_version: str,
    top_k: int,
    chunk_ids: list[str],
    latency_ms: int,
    *,
    ranking_strategy: str,
    reranker_model: str | None,
    candidate_pool_k: int,
) -> RetrievalRun:
    """写检索运行快照（同 query/filter/index/排名配置复用既有 run）。"""
    q_hash = _query_hash(request.query)
    f_hash = _filters_hash(
        vis,
        top_k,
        request.retrieval_mode,
        ranking_strategy=ranking_strategy,
        reranker_model=reranker_model,
        candidate_pool_k=candidate_pool_k,
    )
    existing = session.scalar(
        select(RetrievalRun).where(
            RetrievalRun.query_hash == q_hash,
            RetrievalRun.filters_hash == f_hash,
            RetrievalRun.index_version == index_version,
            RetrievalRun.as_of == (vis.as_of or ""),
        )
    )
    if existing is not None:
        return existing
    run = RetrievalRun(
        run_id=f"rr-{uuid.uuid4().hex[:12]}",
        query_hash=q_hash,
        query_text=request.query,
        filters_hash=f_hash,
        knowledge_layers=sorted(vis.layers),
        permission_scope=",".join(sorted(vis.scopes)),
        project_id=vis.project_id,
        lot_id=vis.lot_id,
        as_of=vis.as_of or "",
        top_k=top_k,
        retrieval_mode=request.retrieval_mode,
        ranking_strategy=ranking_strategy,
        reranker_model=reranker_model,
        index_version=index_version,
        candidate_chunk_ids=chunk_ids,
        latency_ms=latency_ms,
        status="completed",
    )
    session.add(run)
    session.commit()
    return run


def hybrid_search(
    session: Session,
    request: SearchRequest,
    vis: Visibility,
    *,
    role: str = "",
    embed_query_fn=None,
    rerank_fn: Callable[[str, list[str]], Any] | None = None,
    request_id: str = "",
) -> SearchResponse:
    """方案 §8.1：混合检索入口（候选证据，candidate_only=true）。

    权限/项目/标段/时点过滤始终先于关键词、向量和重排。`rerank_fn` 仅用于
    本地模型适配器或测试注入；它无法看到任何未通过可见性谓词的材料。
    """
    import time

    start = time.monotonic()
    top_k = request.top_k
    mode = request.retrieval_mode
    pool_k = _candidate_pool_k(top_k)
    reranker_model = config.reranker_model() if config.reranker_enabled() else None

    # 索引就绪检查：无任何 current chunk → 409 knowledge_not_ready
    has_chunks = session.scalar(
        select(KnowledgeChunk.chunk_id)
        .where(
            KnowledgeChunk.knowledge_layer.in_(vis.layers),
            KnowledgeChunk.index_status == "current",
        )
        .limit(1)
    )
    if has_chunks is None:
        raise KnowledgeNotReadyError("知识索引未就绪（无 current chunk），请先执行索引任务")

    tokens = _tokenize(request.query)
    keyword = _keyword_search(session, tokens, vis, pool_k) if mode in ("hybrid", "keyword") else []
    vector = (
        _vector_search(session, vis, pool_k, request.query, embed_query_fn)
        if mode in ("hybrid", "vector")
        else []
    )

    if mode == "keyword":
        ordered = [{"chunk_id": item["chunk_id"], "base_score": item["score"]} for item in keyword]
        ranking_strategy = "keyword"
    elif mode == "vector":
        ordered = [{"chunk_id": item["chunk_id"], "base_score": item["score"]} for item in vector]
        ranking_strategy = "vector"
    else:
        ordered = _rrf_merge(keyword, vector, pool_k)
        ranking_strategy = "hybrid_rrf"

    chunks = {chunk.chunk_id: chunk for chunk in _load_chunks(session, [o["chunk_id"] for o in ordered])}
    # Rerank only current visible candidates. A bad/unavailable local service does not break evidence
    # discovery: response retains base ordering and records an auditable degraded strategy.
    if config.reranker_enabled() and ordered:
        try:
            ordered, reranked = _rerank(request.query, ordered, chunks, rerank_fn)
            if reranked:
                ranking_strategy = f"{ranking_strategy}_reranked"
        except Exception as exc:
            # 本地服务缺失、模型未配置或响应非法均只能降级，不得伪造 rerank 成功。
            logger.warning("reranker 失败，保留基础排序: %s", exc)
            ranking_strategy = f"{ranking_strategy}_rerank_degraded"

    ordered = ordered[:top_k]
    items = []
    for rank, item in enumerate(ordered, start=1):
        chunk = chunks.get(item["chunk_id"])
        if chunk is None:
            continue
        # 统一为 0..1 的名次分数；RAG 分数仅表达排序，不可作为匹配判定或置信度。
        items.append(_to_dto(chunk, 1.0 / rank))

    index_version = _current_index_version(session)
    latency_ms = int((time.monotonic() - start) * 1000)
    run = _persist_run(
        session,
        request,
        vis,
        index_version,
        top_k,
        [item.chunk_id for item in items],
        latency_ms,
        ranking_strategy=ranking_strategy,
        reranker_model=reranker_model,
        candidate_pool_k=pool_k,
    )
    return SearchResponse(
        request_id=request_id,
        retrieval_run_id=run.run_id,
        candidate_only=True,
        insufficient_evidence=len(items) == 0,
        index_version=index_version,
        ranking_strategy=ranking_strategy,
        reranker_model=reranker_model,
        items=items,
        filters={
            "knowledge_layers": sorted(vis.layers),
            "permission_scope": sorted(vis.scopes),
            "project_id": vis.project_id,
            "lot_id": vis.lot_id,
            "as_of": vis.as_of,
            "candidate_pool_k": pool_k,
        },
    )


def _current_index_version(session: Session) -> str:
    row = session.scalar(
        select(KnowledgeChunk.index_version)
        .where(KnowledgeChunk.index_status == "current")
        .order_by(KnowledgeChunk.updated_at.desc())
        .limit(1)
    )
    return row or "none"
