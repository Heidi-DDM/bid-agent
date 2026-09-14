"""记录 RAG 重排策略与模型，保证检索结果可审计回放。

Revision ID: 0013_r025_reranker_audit
Revises: 0012_candidate_detail_summary
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_r025_reranker_audit"
down_revision = "0012_candidate_detail_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "retrieval_runs",
        sa.Column("ranking_strategy", sa.String(length=64), nullable=True),
        schema="knowledge_data",
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("reranker_model", sa.String(length=128), nullable=True),
        schema="knowledge_data",
    )
    op.execute(
        "UPDATE knowledge_data.retrieval_runs "
        "SET ranking_strategy = CASE retrieval_mode "
        "WHEN 'keyword' THEN 'keyword' WHEN 'vector' THEN 'vector' ELSE 'hybrid_rrf' END "
        "WHERE ranking_strategy IS NULL"
    )
    op.alter_column(
        "retrieval_runs",
        "ranking_strategy",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default="hybrid_rrf",
        schema="knowledge_data",
    )


def downgrade() -> None:
    op.drop_column("retrieval_runs", "reranker_model", schema="knowledge_data")
    op.drop_column("retrieval_runs", "ranking_strategy", schema="knowledge_data")
