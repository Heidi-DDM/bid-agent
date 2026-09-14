# F025 §7 / docs/07 方案 §8：RAG 真库集成测试（integration 标记）
# 覆盖：索引幂等（不误标 stale/不重复 chunk）、版本变更旧 chunk 失效、
#       检索 fail-closed（无索引拒绝、L2/L3 缺项目上下文拒绝）、
#       项目过滤不泄漏、retrieval_runs 幂等复用。
# 向量由测试注入确定性 embed_fn（本地 embedding 推理由 R021/R022 解析器接入前不伪造）。
#
# 运行方式（沙盒外终端，需先 bash scripts/setup_local_env.sh 完成建库+迁移）：
#   export DATABASE_URL="postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent"
#   python -m pytest runtime/tests/test_rag_integration.py -m integration -v
import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from runtime.db.models import KnowledgeChunk, Material, RetrievalRun
from runtime.rag.chunker import ParsedPage
from runtime.rag.filters import Actor, VisibilityError, build_visibility_predicate
from runtime.rag.indexer import index_material
from runtime.rag.retriever import KnowledgeNotReadyError, hybrid_search
from runtime.rag.schemas import SearchRequest

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DATABASE_URL,
    reason="集成测试需要真实 PostgreSQL：请设置 DATABASE_URL 后执行",
)

_DIM = 1024
_AS_OF = "2025-10-30T00:00:00+08:00"


def _embed(text: str) -> list[float]:
    """测试注入的确定性向量（非零，pgvector cosine 可用；真实 embedding 走本地模型）。"""
    seed = sum(ord(c) for c in text)
    return [0.0005 + (seed + i) % 3 * 0.0001 for i in range(_DIM)]


@pytest.fixture()
def session(monkeypatch):
    # 真库环境允许向量索引/检索（生产由 .env 的 PGVECTOR_ENABLED 控制）
    monkeypatch.setenv("PGVECTOR_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embed")
    monkeypatch.setenv("EMBEDDING_DIM", str(_DIM))
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE knowledge_data.knowledge_chunks, knowledge_data.knowledge_embeddings, "
                "knowledge_data.retrieval_runs, public_data.materials RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as s:
        yield s


def _material(session, material_id="MAT-RAG-1", version=1, content_hash="a" * 64,
              project_id="ND-2025") -> None:
    session.add(Material(
        material_id=material_id, version=version, material_type="evidence_file",
        source_type="uploaded", owner_type="enterprise", classification="internal",
        permission_scope="enterprise_read", content_hash=content_hash,
        parse_status="parsed", status="active", evidence_refs=[], data_owner="测试",
        project_id=project_id,
    ))
    session.commit()


def _pages(*paragraphs: str) -> list[ParsedPage]:
    return [ParsedPage(page_no=1, paragraphs=list(paragraphs))]


@needs_db
def test_index_idempotent_keeps_current(session):
    _material(session)
    pages = _pages("具备建筑工程施工总承包二级及以上资质", "近三年合同金额累计不少于 1.2 亿元")
    first = index_material(session, "MAT-RAG-1", 1, parsed_pages=pages, embed_fn=_embed)
    second = index_material(session, "MAT-RAG-1", 1, parsed_pages=pages, embed_fn=_embed)

    assert first.created == 2 and second.created == 0 and second.skipped == 2
    # 幂等重索引不得把自身 current 误标 stale（否则 current 归零、检索误报未就绪）
    assert second.stale_marked == 0
    chunks = session.scalars(select(KnowledgeChunk)).all()
    assert len(chunks) == 2
    assert all(c.index_status == "current" for c in chunks)


@needs_db
def test_new_version_marks_old_stale(session):
    _material(session, version=1, content_hash="a" * 64)
    index_material(session, "MAT-RAG-1", 1, parsed_pages=_pages("旧版本条款"), embed_fn=_embed)
    _material(session, version=2, content_hash="b" * 64)
    result = index_material(session, "MAT-RAG-1", 2, parsed_pages=_pages("澄清后的新条款"), embed_fn=_embed)

    assert result.stale_marked == 1
    statuses = {c.material_version: c.index_status for c in session.scalars(select(KnowledgeChunk)).all()}
    assert statuses == {1: "stale", 2: "current"}


@needs_db
def test_search_fail_closed_without_index(session):
    _material(session)
    request = SearchRequest(query="建筑工程施工总承包", knowledge_layers=["L3_enterprise"],
                            project_id="ND-2025", as_of=_AS_OF)
    vis = build_visibility_predicate(Actor(role="data_admin", actor="测试"), request)
    with pytest.raises(KnowledgeNotReadyError):
        hybrid_search(session, request, vis, role="data_admin", embed_query_fn=_embed)


@needs_db
def test_l3_requires_project_context(session):
    # L2/L3 缺 project_id/as_of → 直接拒绝（方案 §3.4 fail-closed）
    request = SearchRequest(query="建筑工程施工总承包", knowledge_layers=["L3_enterprise"])
    with pytest.raises(VisibilityError):
        build_visibility_predicate(Actor(role="data_admin", actor="测试"), request)


@needs_db
def test_retrieval_run_reuse_and_project_isolation(session):
    _material(session, material_id="MAT-A", project_id="ND-2025")
    _material(session, material_id="MAT-B", project_id="ND-OTHER")
    index_material(session, "MAT-A", 1, parsed_pages=_pages("具备建筑工程施工总承包二级及以上资质"), embed_fn=_embed)
    index_material(session, "MAT-B", 1, parsed_pages=_pages("市政公用工程施工总承包一级"), embed_fn=_embed)

    request = SearchRequest(query="建筑工程施工总承包", knowledge_layers=["L3_enterprise"],
                            project_id="ND-2025", as_of=_AS_OF, top_k=10)
    vis = build_visibility_predicate(Actor(role="data_admin", actor="测试"), request)
    r1 = hybrid_search(session, request, vis, role="data_admin", embed_query_fn=_embed)
    r2 = hybrid_search(session, request, vis, role="data_admin", embed_query_fn=_embed)

    assert r1.retrieval_run_id == r2.retrieval_run_id  # 同 query/filter/index/as_of 复用
    assert len(session.scalars(select(RetrievalRun)).all()) == 1
    assert r1.candidate_only is True
    assert {i.material_id for i in r1.items} == {"MAT-A"}  # 项目隔离：MAT-B 不泄漏
    assert r1.items[0].citation.startswith("MAT-A:")

@needs_db
def test_reranker_reorders_rrf_pool_and_records_strategy(session, monkeypatch):
    """Cross-encoder 只能看到 SQL 可见的候选；成功后才以其结果截取 top-k。"""
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANKER_MODEL", "test-reranker-local")
    monkeypatch.setenv("RAG_RERANK_CANDIDATE_K", "6")
    _material(session)
    index_material(
        session,
        "MAT-RAG-1",
        1,
        parsed_pages=_pages("建筑工程施工总承包资质说明", "目标条款：建筑工程施工总承包二级及以上"),
        embed_fn=_embed,
    )
    request = SearchRequest(
        query="建筑工程施工总承包二级", knowledge_layers=["L3_enterprise"],
        project_id="ND-2025", as_of=_AS_OF, top_k=1,
    )
    vis = build_visibility_predicate(Actor(role="data_admin", actor="测试"), request)
    seen_documents: list[str] = []

    def fake_rerank(_query: str, documents: list[str]) -> list[float]:
        seen_documents.extend(documents)
        return [0.1 if "资质说明" in doc else 0.9 for doc in documents]

    result = hybrid_search(
        session, request, vis, role="data_admin", embed_query_fn=_embed, rerank_fn=fake_rerank
    )

    assert len(seen_documents) == 2
    assert result.items[0].text.startswith("目标条款")
    assert result.ranking_strategy == "hybrid_rrf_reranked"
    assert result.reranker_model == "test-reranker-local"
    run = session.get(RetrievalRun, result.retrieval_run_id)
    assert run.ranking_strategy == "hybrid_rrf_reranked"
    assert run.reranker_model == "test-reranker-local"


@needs_db
def test_reranker_failure_keeps_rrf_result_and_is_auditable(session, monkeypatch):
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANKER_MODEL", "test-reranker-local")
    _material(session)
    index_material(
        session, "MAT-RAG-1", 1,
        parsed_pages=_pages("建筑工程施工总承包资质说明", "建筑工程施工总承包二级及以上"),
        embed_fn=_embed,
    )
    request = SearchRequest(
        query="建筑工程施工总承包二级", knowledge_layers=["L3_enterprise"],
        project_id="ND-2025", as_of=_AS_OF, top_k=2,
    )
    vis = build_visibility_predicate(Actor(role="data_admin", actor="测试"), request)

    def unavailable(_query: str, _documents: list[str]) -> list[float]:
        raise RuntimeError("local reranker unavailable")

    result = hybrid_search(
        session, request, vis, role="data_admin", embed_query_fn=_embed, rerank_fn=unavailable
    )

    assert len(result.items) == 2
    assert result.ranking_strategy == "hybrid_rrf_rerank_degraded"
    run = session.get(RetrievalRun, result.retrieval_run_id)
    assert run.ranking_strategy == "hybrid_rrf_rerank_degraded"
