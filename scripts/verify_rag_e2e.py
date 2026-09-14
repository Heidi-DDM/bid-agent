#!/usr/bin/env python3
"""真库 RAG 端到端验证（安全的、可清理的自动化版本）。

链路：写入带随机前缀的合成 Material → index_material（真实本地 BGE-M3 embedding）
→ hybrid_search / vector 检索 → 断言命中、项目隔离、run 幂等 → 只清理本次合成数据。

安全约束：绝不 TRUNCATE 任何业务表，绝不删除非本次运行产生的资料、分片或检索快照。
即使运行失败，也会在 finally 中按随机 material/project ID 精确清理本次测试数据。

前置：
  1. embedding 服务运行：bash embedding_serve/start.sh start
  2. PostgreSQL 迁移到 head，pgvector 扩展可用
  3. 运行：DATABASE_URL=... MODEL_BASE_URL=http://127.0.0.1:8001 \\
     EMBEDDING_MODEL=bge-m3-local PGVECTOR_ENABLED=true \\
     .venv/bin/python scripts/verify_rag_e2e.py

通过标准：索引 3 个 1024 维真向量；向量检索命中正确项目；跨项目材料不泄漏；
相同查询复用同一 retrieval_run。此脚本仅验证 embedding/召回；reranker 另由模型资产
可用后的 /v1/rerank 冒烟和 RAG 集成测试验证。
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

from runtime.db.models import KnowledgeChunk, KnowledgeEmbedding, Material, RetrievalRun
from runtime.rag.chunker import ParsedPage
from runtime.rag.filters import Actor, build_visibility_predicate
from runtime.rag.indexer import _embed, index_material
from runtime.rag.retriever import hybrid_search
from runtime.rag.schemas import SearchRequest

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent"
)
AS_OF = "2025-10-30T00:00:00+08:00"
RUN_SUFFIX = uuid.uuid4().hex[:12]
PROJECT_A = f"E2E-{RUN_SUFFIX}-A"
PROJECT_B = f"E2E-{RUN_SUFFIX}-B"
MATERIAL_A = f"E2E-MAT-{RUN_SUFFIX}-A"
MATERIAL_B = f"E2E-MAT-{RUN_SUFFIX}-B"
MATERIAL_IDS = (MATERIAL_A, MATERIAL_B)


def pages(*paras: str) -> list[ParsedPage]:
    return [ParsedPage(page_no=1, paragraphs=list(paras))]


def add_material(session: Session, material_id: str, project_id: str) -> None:
    session.add(
        Material(
            material_id=material_id,
            version=1,
            material_type="evidence_file",
            source_type="uploaded",
            owner_type="enterprise",
            classification="internal",
            permission_scope="enterprise_read",
            content_hash=("a" if material_id == MATERIAL_A else "b") * 64,
            parse_status="parsed",
            status="active",
            evidence_refs=[],
            data_owner="rag_e2e_synthetic",
            project_id=project_id,
        )
    )
    session.commit()


def cleanup(engine) -> None:
    """仅删除本次随机 ID 对应的合成记录，绝不碰业务资料。"""
    with engine.begin() as conn:
        chunk_ids = select(KnowledgeChunk.chunk_id).where(KnowledgeChunk.material_id.in_(MATERIAL_IDS))
        conn.execute(delete(KnowledgeEmbedding).where(KnowledgeEmbedding.chunk_id.in_(chunk_ids)))
        conn.execute(delete(KnowledgeChunk).where(KnowledgeChunk.material_id.in_(MATERIAL_IDS)))
        conn.execute(delete(RetrievalRun).where(RetrievalRun.project_id.in_((PROJECT_A, PROJECT_B))))
        conn.execute(
            delete(Material).where(
                Material.material_id.in_(MATERIAL_IDS), Material.version == 1
            )
        )


def main() -> None:
    engine = create_engine(DATABASE_URL)
    try:
        with Session(engine) as session:
            add_material(session, MATERIAL_A, PROJECT_A)
            add_material(session, MATERIAL_B, PROJECT_B)
            result_a = index_material(
                session,
                MATERIAL_A,
                1,
                parsed_pages=pages(
                    "投标人须具备建筑工程施工总承包二级及以上资质",
                    "近三年合同金额累计不少于 1.2 亿元",
                ),
            )
            result_b = index_material(
                session,
                MATERIAL_B,
                1,
                parsed_pages=pages("市政公用工程施工总承包一级"),
            )
            print(f"[1] 索引 A: created={result_a.created} | B: created={result_b.created}")
            assert result_a.created == 2 and result_b.created == 1

            own_chunks = session.scalars(
                select(KnowledgeChunk).where(KnowledgeChunk.material_id.in_(MATERIAL_IDS))
            ).all()
            own_chunk_ids = [chunk.chunk_id for chunk in own_chunks]
            own_vectors = session.execute(
                select(KnowledgeEmbedding.embedding_dim).where(
                    KnowledgeEmbedding.chunk_id.in_(own_chunk_ids),
                    KnowledgeEmbedding.embedding_model == "bge-m3-local",
                )
            ).all()
            print(f"[2] 本次 current chunks={len(own_chunks)} | vectors={len(own_vectors)}")
            assert len(own_chunks) == 3 and len(own_vectors) == 3
            assert {row[0] for row in own_vectors} == {1024}

            request = SearchRequest(
                query="建筑工程施工总承包二级资质",
                knowledge_layers=["L3_enterprise"],
                project_id=PROJECT_A,
                as_of=AS_OF,
                top_k=10,
            )
            visibility = build_visibility_predicate(Actor(role="data_admin", actor="rag-e2e"), request)
            first = hybrid_search(session, request, visibility, role="data_admin", embed_query_fn=_embed)
            second = hybrid_search(session, request, visibility, role="data_admin", embed_query_fn=_embed)
            print(f"[3] hybrid items={[(item.material_id, round(item.retrieval_score, 4)) for item in first.items]}")
            assert first.retrieval_run_id == second.retrieval_run_id
            assert {item.material_id for item in first.items} == {MATERIAL_A}, "项目隔离失败：测试 B 被召回"
            assert first.candidate_only is True and first.items[0].citation.startswith(f"{MATERIAL_A}:")

            vector_request = SearchRequest(
                query="具备建筑工程施工总承包二级及以上资质",
                knowledge_layers=["L3_enterprise"],
                project_id=PROJECT_A,
                as_of=AS_OF,
                top_k=5,
                retrieval_mode="vector",
            )
            vector_visibility = build_visibility_predicate(
                Actor(role="data_admin", actor="rag-e2e"), vector_request
            )
            vector_result = hybrid_search(
                session, vector_request, vector_visibility, role="data_admin", embed_query_fn=_embed
            )
            print(
                f"[4] vector items={[(item.material_id, round(item.retrieval_score, 4)) for item in vector_result.items]}"
            )
            assert vector_result.items and vector_result.items[0].material_id == MATERIAL_A
            assert vector_result.items[0].retrieval_score > 0.5

            own_runs = session.scalars(
                select(RetrievalRun).where(RetrievalRun.project_id == PROJECT_A)
            ).all()
            print(f"[5] 本次 retrieval_runs={len(own_runs)}（hybrid 幂等 1 + vector 1）")
            assert len(own_runs) == 2
            print("\n=== 端到端全部通过 ===")
    finally:
        cleanup(engine)
        engine.dispose()
        print("本次合成测试数据已按随机 ID 精确清理")


if __name__ == "__main__":
    main()
