#!/usr/bin/env python3
"""真库 RAG 端到端验证（背景清单 C 步的自动化版本）。

链路：插 Material → index_material（真实 bge-m3 embedding，经本地 8001 服务）
      → hybrid_search / vector 检索 → 断言命中/项目隔离/run 幂等 → 清理。

前置：
  1. embedding 服务运行中：embedding_serve/bash start.sh start
  2. PG 迁移到 head（0004_r025_knowledge），pgvector 扩展已装
  3. 环境：unset PYTHONPATH（否则 python3.14 会错 import 他解释器版本的包）
     export MODEL_BASE_URL=http://127.0.0.1:8001 EMBEDDING_MODEL=bge-m3-local
     export EMBEDDING_DIM=1024 PGVECTOR_ENABLED=true
  4. 运行：项目 .venv/bin/python scripts/verify_rag_e2e.py

通过标准：索引 3×1024 维真向量；vector 检索 top1 cosine > 0.5；
MAT-B（其他项目）不泄漏；重复查询复用同一 retrieval_run。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from runtime.db.models import KnowledgeChunk, KnowledgeEmbedding, Material, RetrievalRun
from runtime.rag.chunker import ParsedPage
from runtime.rag.filters import Actor, build_visibility_predicate
from runtime.rag.indexer import _embed, index_material
from runtime.rag.retriever import hybrid_search
from runtime.rag.schemas import SearchRequest

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent",
)
AS_OF = "2025-10-30T00:00:00+08:00"

engine = create_engine(DATABASE_URL)
with engine.begin() as conn:
    conn.execute(text(
        "TRUNCATE knowledge_data.knowledge_chunks, knowledge_data.knowledge_embeddings, "
        "knowledge_data.retrieval_runs, public_data.materials RESTART IDENTITY CASCADE"))

with Session(engine) as s:
    def material(mid, project):
        s.add(Material(material_id=mid, version=1, material_type="evidence_file",
                       source_type="uploaded", owner_type="enterprise",
                       classification="internal", permission_scope="enterprise_read",
                       content_hash="a" * 64, parse_status="parsed", status="active",
                       evidence_refs=[], data_owner="e2e", project_id=project))
        s.commit()

    def pages(*paras):
        return [ParsedPage(page_no=1, paragraphs=list(paras))]

    # ---- 1. 两条材料：MAT-A 匹配查询；MAT-B（其他项目）不应泄漏 ----
    material("MAT-A", "ND-2025")
    material("MAT-B", "ND-OTHER")
    r_a = index_material(s, "MAT-A", 1, parsed_pages=pages(
        "投标人须具备建筑工程施工总承包二级及以上资质", "近三年合同金额累计不少于 1.2 亿元"))
    r_b = index_material(s, "MAT-B", 1, parsed_pages=pages("市政公用工程施工总承包一级"))
    print(f"[1] 索引 MAT-A: created={r_a.created} | MAT-B: created={r_b.created}")
    assert r_a.created == 2 and r_b.created == 1

    n_emb = s.scalar(select(KnowledgeChunk).where(
        KnowledgeChunk.embedding_model == "bge-m3-local",
        KnowledgeChunk.index_status == "current"))
    n_vec = s.execute(text("SELECT count(*) FROM knowledge_data.knowledge_embeddings "
                           "WHERE embedding_model='bge-m3-local'")).scalar()
    print(f"[2] current chunk 存在={n_emb is not None} | 向量总数={n_vec}")
    assert n_emb is not None and n_vec == 3
    dims = s.execute(text("SELECT DISTINCT embedding_dim FROM knowledge_data.knowledge_embeddings")).all()
    print(f"[3] 向量维度={[d[0] for d in dims]}")
    assert dims == [(1024,)]

    # ---- 2. 混合检索（真实查询向量）----
    req = SearchRequest(query="建筑工程施工总承包二级资质", knowledge_layers=["L3_enterprise"],
                        project_id="ND-2025", as_of=AS_OF, top_k=10)
    vis = build_visibility_predicate(Actor(role="data_admin", actor="e2e"), req)
    r1 = hybrid_search(s, req, vis, role="data_admin", embed_query_fn=_embed)
    r2 = hybrid_search(s, req, vis, role="data_admin", embed_query_fn=_embed)
    print(f"[4] hybrid items={[(i.material_id, round(i.retrieval_score, 4)) for i in r1.items]}")
    print(f"[5] run 幂等: 复用={r1.retrieval_run_id == r2.retrieval_run_id}")
    assert r1.retrieval_run_id == r2.retrieval_run_id
    assert {i.material_id for i in r1.items} == {"MAT-A"}, "项目隔离失败：MAT-B 泄漏!"
    assert r1.candidate_only is True
    assert r1.items[0].citation.startswith("MAT-A:")
    # hybrid 走 RRF 融合（1/(60+rank)），分数天然低；纯 vector 模式才暴露真实 cosine 分
    assert r1.items[0].retrieval_score > 0.005

    # ---- 2b. 纯向量检索：验证 bge-m3 语义命中（cosine 分应显著）----
    req_v = SearchRequest(query="具备建筑工程施工总承包二级及以上资质", knowledge_layers=["L3_enterprise"],
                          project_id="ND-2025", as_of=AS_OF, top_k=5, retrieval_mode="vector")
    vis_v = build_visibility_predicate(Actor(role="data_admin", actor="e2e"), req_v)
    rv = hybrid_search(s, req_v, vis_v, role="data_admin", embed_query_fn=_embed)
    print(f"[5b] vector items={[(i.material_id, round(i.retrieval_score, 4), i.text[:22]) for i in rv.items]}")
    assert len(rv.items) >= 1 and rv.items[0].retrieval_score > 0.5, "向量语义检索分数异常低"
    assert rv.items[0].material_id == "MAT-A"

    runs = s.scalars(select(RetrievalRun)).all()
    print(f"[6] retrieval_runs 记录数={len(runs)}（hybrid 幂等 1 + vector 1）")
    assert len(runs) == 2
    print("\n=== 端到端全部通过 ===")

# 清理
with engine.begin() as conn:
    conn.execute(text(
        "TRUNCATE knowledge_data.knowledge_chunks, knowledge_data.knowledge_embeddings, "
        "knowledge_data.retrieval_runs, public_data.materials RESTART IDENTITY CASCADE"))
print("测试数据已清理")
