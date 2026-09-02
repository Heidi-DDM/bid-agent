"""R025 迁移：knowledge_data schema 与 RAG 三类表（F025 §7 / docs/07 方案 §3.2）

Revision ID: 0004_r025_knowledge
Revises: 0003_r020_approval_state_width
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_r025_knowledge"
down_revision = "0003_r020_approval_state_width"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")

try:  # pragma: no cover - 依赖探测
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover
    Vector = None  # type: ignore[assignment,misc]

# 向量列：安装 pgvector 后用真实 vector 类型；未安装时回退 Text（仅迁移占位，索引任务会失败）
_VECTOR_TYPE = Vector(1024) if Vector is not None else sa.Text()  # type: ignore[assignment]


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS knowledge_data")

    # ---- knowledge_chunks：统一 KnowledgeChunk（F025 §3.1） ----
    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", sa.String(64), primary_key=True),
        sa.Column("chunk_seq", sa.Integer(), nullable=False),
        sa.Column("knowledge_layer", sa.String(16), nullable=False),
        sa.Column("material_id", sa.String(64), nullable=False),
        sa.Column("material_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("section_title", sa.String(256), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("field_refs", JSONB, nullable=True),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("paragraph_no", sa.Integer(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("lot_id", sa.String(64), nullable=True),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("permission_scope", sa.String(24), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("verification_status", sa.String(24), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding_model", sa.String(64), nullable=False),
        sa.Column("index_version", sa.String(64), nullable=False),
        sa.Column("index_status", sa.String(16), nullable=False, server_default="current"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "material_id",
            "material_version",
            "content_hash",
            "embedding_model",
            "index_version",
            "chunk_seq",
            name="uq_knowledge_chunks_index_idem",
        ),
        schema="knowledge_data",
    )
    op.create_index(
        "ix_knowledge_chunks_layer_status", "knowledge_chunks",
        ["knowledge_layer", "verification_status"], schema="knowledge_data",
    )
    op.create_index(
        "ix_knowledge_chunks_project", "knowledge_chunks", ["project_id"],
        schema="knowledge_data",
    )
    op.create_index(
        "ix_knowledge_chunks_material", "knowledge_chunks",
        ["material_id", "material_version"], schema="knowledge_data",
    )
    op.create_index(
        "ix_knowledge_chunks_index_version", "knowledge_chunks", ["index_version"],
        schema="knowledge_data",
    )

    # ---- knowledge_embeddings：chunk 向量（幂等唯一键防重复索引） ----
    op.create_table(
        "knowledge_embeddings",
        sa.Column("embedding_id", sa.String(64), primary_key=True),
        sa.Column("chunk_id", sa.String(64), nullable=False),
        sa.Column("embedding_model", sa.String(64), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("index_version", sa.String(64), nullable=False),
        sa.Column("vector", _VECTOR_TYPE, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "chunk_id", "embedding_model", "index_version",
            name="uq_knowledge_embeddings_chunk_model_version",
        ),
        schema="knowledge_data",
    )

    # ---- retrieval_runs：检索运行快照（幂等复用既有 run） ----
    op.create_table(
        "retrieval_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("filters_hash", sa.String(64), nullable=False),
        sa.Column("knowledge_layers", JSONB, nullable=False),
        sa.Column("permission_scope", sa.String(255), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("lot_id", sa.String(64), nullable=True),
        sa.Column("as_of", sa.String(64), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("retrieval_mode", sa.String(16), nullable=False, server_default="hybrid"),
        sa.Column("index_version", sa.String(64), nullable=False),
        sa.Column("candidate_chunk_ids", JSONB, nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="completed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "query_hash", "filters_hash", "index_version", "as_of",
            name="uq_retrieval_runs_query_filters",
        ),
        schema="knowledge_data",
    )
    op.create_index(
        "ix_retrieval_runs_project", "retrieval_runs", ["project_id"],
        schema="knowledge_data",
    )

    # ---- match_runs 扩展：RAG 候选与结构化核验快照（方案 §3.5） ----
    op.add_column(
        "match_runs",
        sa.Column("retrieval_run_id", sa.String(64), nullable=True),
        schema="admission_data",
    )
    op.add_column(
        "match_runs",
        sa.Column("index_version", sa.String(64), nullable=True),
        schema="admission_data",
    )
    op.add_column(
        "match_runs",
        sa.Column("candidate_chunk_ids", JSONB, nullable=True),
        schema="admission_data",
    )
    op.add_column(
        "match_runs",
        sa.Column("structured_verification", JSONB, nullable=True),
        schema="admission_data",
    )
    op.add_column(
        "match_runs",
        sa.Column("evidence_snapshot_hash", sa.String(64), nullable=True),
        schema="admission_data",
    )
    op.create_index(
        "ix_match_runs_retrieval_run", "match_runs", ["retrieval_run_id"],
        schema="admission_data",
    )


def downgrade() -> None:
    op.drop_index("ix_match_runs_retrieval_run", table_name="match_runs", schema="admission_data")
    op.drop_column("match_runs", "retrieval_run_id", schema="admission_data")
    op.drop_column("match_runs", "index_version", schema="admission_data")
    op.drop_column("match_runs", "candidate_chunk_ids", schema="admission_data")
    op.drop_column("match_runs", "structured_verification", schema="admission_data")
    op.drop_column("match_runs", "evidence_snapshot_hash", schema="admission_data")

    op.drop_table("retrieval_runs", schema="knowledge_data")
    op.drop_table("knowledge_embeddings", schema="knowledge_data")
    op.drop_table("knowledge_chunks", schema="knowledge_data")
    op.execute("DROP SCHEMA IF EXISTS knowledge_data CASCADE")