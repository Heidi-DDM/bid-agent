# F025 §7 / 方案 §8.1：索引流水线（material → chunk + embedding）
# 幂等：index:{material_id}:{version}:{content_hash}:{embedding_model}:{index_version}；
# 旧版本/澄清/证据变更 → 旧 chunk 标记 stale，不得驱动审批；
# pgvector/embedding 不可用时任务必须失败（retryable/manual_review），不伪造成功。
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.core import config
from runtime.core.errors import ApiError
from runtime.db.models import KnowledgeChunk, KnowledgeEmbedding, Material
from runtime.rag.schemas import ChunkDraft, IndexResult

logger = logging.getLogger("runtime.rag.indexer")


class IndexingError(Exception):
    """索引失败（转任务 retryable/manual_review）。"""


class VectorNotReadyError(IndexingError):
    """pgvector/embedding 不可用（F025 §8.1 不得假装成功）。"""


class HashMismatchBlocked(IndexingError):
    """内容哈希与材料版本不一致，阻断索引（F025 §8.1 原文不可变）。"""


def build_index_idempotency_key(
    material_id: str, version: int, content_hash: str, embedding_model: str, index_version: str
) -> str:
    """方案 §8.3 幂等键：SHA-256(index:...)"""
    raw = f"index:{material_id}:{version}:{content_hash}:{embedding_model}:{index_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def index_version_for(material: Material) -> str:
    """索引版本：embedding 模型 + 内容哈希前缀（材料版本变化 → 新索引版本）。"""
    return f"{config.embedding_model() or 'embedding'}:{material.content_hash[:12]}:{material.version}"


def _check_vector_ready(session: Session, embed_fn=None) -> None:
    if not config.pgvector_enabled():
        raise VectorNotReadyError("PGVECTOR_ENABLED=false，禁止创建成功索引（F025 §7）")
    if not config.embedding_model():
        raise VectorNotReadyError("EMBEDDING_MODEL 未配置，禁止创建成功索引")
    if embed_fn is not None:
        return  # 注入的 embedding 函数由调用方负责可用性（测试/适配器）
    # embedding 服务就绪探测（本地模型适配器）
    from runtime.core import model

    if not model.check_embedding_allowed():
        raise VectorNotReadyError("本地 embedding 模型不可用/非内网，禁止创建成功索引")


def _embed(text: str) -> list[float]:
    """本地 embedding 调用（F025 §5：L2/L3 向量化不出内网）。

    经本地模型适配器调用 OpenAI 兼容 /v1/embeddings（bge-m3 等本地模型）；
    服务不可用/非内网时抛出明确错误，由任务层转 retryable，
    不得返回占位向量冒充成功（方案 §7.2 第 5 条降级原则）。
    """
    from runtime.core import model

    if not model.check_embedding_allowed():
        raise VectorNotReadyError("embedding 服务不可用（本地/内网），任务应 retryable")
    result = model.embed(text, timeout=config.readyz_timeout_seconds())
    if not result.ok:
        raise VectorNotReadyError(f"embedding 调用失败: {result.error}")
    vector = result.data
    if not isinstance(vector, list) or not vector:
        raise VectorNotReadyError(f"embedding 返回非法/空向量: {type(vector).__name__}")
    return vector


def _mark_stale(
    session: Session, material_id: str, version: int, embedding_model: str,
    *, exclude_index_version: str | None = None,
) -> int:
    """旧版本/旧索引标记 stale；返回标记数量。

    exclude_index_version：本次索引的版本不标记（幂等重索引不误标自身，
    否则重复执行后 current 归零，检索将误报 knowledge_not_ready）。
    """
    clauses = [
        KnowledgeChunk.material_id == material_id,
        KnowledgeChunk.embedding_model == embedding_model,
        KnowledgeChunk.index_status == "current",
    ]
    if exclude_index_version is not None:
        from sqlalchemy import or_

        clauses.append(
            or_(
                KnowledgeChunk.material_version != version,
                KnowledgeChunk.index_version != exclude_index_version,
            )
        )
    rows = session.scalars(select(KnowledgeChunk).where(*clauses)).all()
    for row in rows:
        row.index_status = "stale"
        row.updated_at = datetime.now(timezone.utc)
    return len(rows)


def _persist_chunks(
    session: Session, drafts: list[ChunkDraft], index_version: str, embed_fn=None
) -> tuple[int, int, int]:
    """批量 upsert chunk + embedding（幂等：唯一键跳过既有）。

    embed_fn：可注入的 embedding 函数（测试用）；默认走本地模型适配器。
    """
    embed_fn = embed_fn or _embed
    created = skipped = failed = 0
    for draft in drafts:
        existing = session.scalar(
            select(KnowledgeChunk).where(
                KnowledgeChunk.material_id == draft.material_id,
                KnowledgeChunk.material_version == draft.material_version,
                KnowledgeChunk.content_hash == draft.content_hash,
                KnowledgeChunk.embedding_model == config.embedding_model(),
                KnowledgeChunk.index_version == index_version,
                KnowledgeChunk.chunk_seq == draft.chunk_seq,
            )
        )
        if existing is not None:
            skipped += 1
            continue
        try:
            vector = embed_fn(draft.text)
        except IndexingError:
            failed += 1
            continue  # 不吞异常：由调用方决定整体失败
        chunk = KnowledgeChunk(
            chunk_id=f"CH-{uuid.uuid4().hex[:14]}",
            chunk_seq=draft.chunk_seq,
            knowledge_layer=draft.knowledge_layer,
            material_id=draft.material_id,
            material_version=draft.material_version,
            content_hash=draft.content_hash,
            section_title=draft.section_title,
            text=draft.text,
            field_refs=draft.field_refs or None,
            page_no=draft.page_no,
            paragraph_no=draft.paragraph_no,
            ocr_confidence=draft.ocr_confidence,
            project_id=draft.project_id,
            lot_id=draft.lot_id,
            owner_type=draft.owner_type,
            permission_scope=draft.permission_scope,
            classification=draft.classification,
            verification_status=draft.verification_status,
            valid_from=draft.valid_from,
            valid_until=draft.valid_until,
            embedding_model=config.embedding_model(),
            index_version=index_version,
            index_status="current",
        )
        session.add(chunk)
        session.flush()
        session.add(
            KnowledgeEmbedding(
                embedding_id=f"EMB-{uuid.uuid4().hex[:14]}",
                chunk_id=chunk.chunk_id,
                embedding_model=config.embedding_model(),
                embedding_dim=config.embedding_dim(),
                index_version=index_version,
                vector=vector,
            )
        )
        created += 1
    return created, skipped, failed


def index_material(
    session: Session,
    material_id: str,
    version: int,
    *,
    index_version: str | None = None,
    parsed_pages: list | None = None,
    embed_fn=None,
) -> IndexResult:
    """方案 §8.1：索引一个材料版本。

    步骤：读取不可变 Material → 校验 content_hash → 检查向量就绪 →
    生成带定位 chunk → 批量 upsert（唯一键幂等）→ 旧版本标记 stale。

    parsed_pages：F021/F022 解析产物（ParsedPage 列表）；缺失时抛可解释失败，
    不得用空文本建索引。
    """
    material = session.get(Material, (material_id, version))
    if material is None:
        raise ApiError("not_found", f"材料不存在: {material_id}:v{version}")
    if material.parse_status not in ("parsed", "pending"):
        # 仅允许已解析/待解析材料索引（F021 解析完成后索引）
        raise ApiError("invalid_state_transition", f"材料 {material_id}:v{version} 未解析，不能索引")

    _check_vector_ready(session, embed_fn)

    idx_version = index_version or index_version_for(material)
    drafts = _build_drafts(session, material, parsed_pages)
    if not drafts:
        raise IndexingError(f"材料 {material_id}:v{version} 无可用解析产物，索引失败（可解释失败）")

    stale = _mark_stale(
        session, material_id, version, config.embedding_model(),
        exclude_index_version=idx_version,
    )
    created, skipped, failed = _persist_chunks(session, drafts, idx_version, embed_fn)
    session.commit()

    result = IndexResult(
        material_id=material_id,
        material_version=version,
        index_version=idx_version,
        embedding_model=config.embedding_model(),
        created=created,
        skipped=skipped,
        failed=failed,
        total_chunks=len(drafts),
        stale_marked=stale,
    )
    if failed:
        # 部分失败：不吞异常，任务进入 retryable（方案 §3.3）
        raise IndexingError(f"索引部分失败 created={created} failed={failed}")
    logger.info(
        "索引完成 material=%s:%s idx=%s created=%s skipped=%s",
        material_id, version, idx_version, created, skipped,
    )
    return result


def _build_drafts(session: Session, material: Material, parsed_pages: list | None) -> list[ChunkDraft]:
    """从解析产物生成 ChunkDraft（解析产物缺失 → 空列表，由调用方转可解释失败）。"""
    if not parsed_pages:
        return []
    from runtime.rag.chunker import chunk_document

    return chunk_document(
        material.material_id,
        material.version,
        material.content_hash,
        parsed_pages,
        knowledge_layer=_layer_for_material(material),
        owner_type=material.owner_type,
        permission_scope=material.permission_scope,
        classification=material.classification,
        project_id=material.project_id,
        verification_status="active" if material.status == "active" else "pending_verification",
        valid_until=material.valid_until,
    )


def _layer_for_material(material: Material) -> str:
    """材料类型 → 知识层（方案 §3.3：L1 公告/L2 招标/L3 企业资料）。"""
    if material.owner_type == "enterprise":
        return "L3_enterprise"
    if material.material_type == "tender_document":
        return "L2_tender"
    return "L1_public"