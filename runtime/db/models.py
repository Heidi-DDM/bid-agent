# F019 §3 analysis_jobs / F018 §4.2：任务持久化模型
# 幂等键唯一约束：job_id 主键 + (kind, input_ref, project_id) 唯一索引。
# 本文件在缺少 SQLAlchemy 环境下不 import（由迁移/服务层按需引入），
# 保证纯逻辑模块 py_compile 与测试不依赖第三方库。
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    input_ref: Mapped[str | None] = mapped_column(String(512))
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    runner_id: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_analysis_jobs_idempotency_key"),
        Index("ix_analysis_jobs_kind_input_project", "kind", "input_ref", "project_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AnalysisJob {self.job_id} {self.kind} {self.status}>"