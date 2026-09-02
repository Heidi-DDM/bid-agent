# F025 §4：RAG 检索请求/响应与 KnowledgeChunk Schema（Pydantic）
# 契约见 docs/03-功能规格/F025 §4 与 docs/07 方案 §9。
# 约束：检索请求必须显式提供 knowledge_layers + permission_scope；L2/L3 必须 project_id + as_of；
# 响应永远 candidate_only=true，禁止 satisfied/qualified/approved_for_bidding 等判定字段。
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 三层知识库（F025 §3.1 / ADR-002 §2.1）
L1_PUBLIC = "L1_public"
L2_TENDER = "L2_tender"
L3_ENTERPRISE = "L3_enterprise"
KNOWLEDGE_LAYERS = frozenset({L1_PUBLIC, L2_TENDER, L3_ENTERPRISE})

# 检索模式（F025 §2.3 混合检索）
RETRIEVAL_MODES = ("hybrid", "keyword", "vector")

# 权限范围（F003 §6.2 / F025 §3.1）
PERMISSION_SCOPES = ("public_read", "enterprise_read", "restricted", "approver_only")

# 核验状态（F025 §3.1）
VERIFICATION_STATUSES = ("pending_verification", "active", "expired", "invalid")

# 检索运行状态
RETRIEVAL_RUN_STATUSES = ("completed", "failed", "retryable")


class SearchRequest(BaseModel):
    """POST /api/v1/knowledge/search 请求体（方案 §9.1）。

    规则：
    - knowledge_layers 必填且非空，禁止跨层无条件混检（F025 §3.1）；
    - L2_tender / L3_enterprise 必须提供 project_id 与 as_of；
    - 有标段时（lot_id 提供）检索限定标段；
    - permission_scope 由服务端按角色推导，请求体不得自报。
    """

    query: str = Field(..., min_length=1, max_length=1000, description="检索文本（原文/条款/证据描述）")
    knowledge_layers: list[str] = Field(..., min_length=1, max_length=3)
    project_id: Optional[str] = None
    lot_id: Optional[str] = None
    as_of: Optional[str] = None  # ISO 8601 时点；L2/L3 必填
    top_k: int = Field(20, ge=1, le=100)
    retrieval_mode: Literal["hybrid", "keyword", "vector"] = "hybrid"

    @field_validator("knowledge_layers")
    @classmethod
    def _validate_layers(cls, v: list[str]) -> list[str]:
        unknown = [x for x in v if x not in KNOWLEDGE_LAYERS]
        if unknown:
            raise ValueError(f"未知 knowledge_layer: {unknown}，允许 {sorted(KNOWLEDGE_LAYERS)}")
        return v

    @field_validator("as_of")
    @classmethod
    def _validate_as_of(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"as_of 必须为 ISO 8601 时间: {v}")
        return v

    def requires_project_context(self) -> bool:
        """L2/L3 查询必须带项目上下文（方案 §3.4）。"""
        return bool({L2_TENDER, L3_ENTERPRISE} & set(self.knowledge_layers))


class ChunkLocation(BaseModel):
    """分片定位：页码/段落或 OCR 坐标（F025 §3.1）。"""

    page_no: Optional[int] = None
    paragraph: Optional[int] = None
    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None


class KnowledgeChunkDTO(BaseModel):
    """检索结果项（方案 §9.2）。"""

    chunk_id: str
    knowledge_layer: str
    material_id: str
    material_version: int
    content_hash: str
    text: str
    location: ChunkLocation
    verification_status: str
    permission_scope: str
    project_id: Optional[str] = None
    lot_id: Optional[str] = None
    retrieval_score: float = Field(ge=0.0, le=1.0)
    citation: str  # material_id:version:p{page}


class SearchResponse(BaseModel):
    """检索响应：永远 candidate_only=true（F025 §4.2）。"""

    request_id: str
    retrieval_run_id: str
    candidate_only: bool = True
    insufficient_evidence: bool = False
    index_version: str
    items: list[KnowledgeChunkDTO] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)


class IndexResult(BaseModel):
    """一次材料版本索引的产物（方案 §8.1）。"""

    material_id: str
    material_version: int
    index_version: str
    embedding_model: str
    created: int = 0
    skipped: int = 0
    failed: int = 0
    total_chunks: int = 0
    stale_marked: int = 0


class ChunkDraft(BaseModel):
    """分片草稿（chunker 产物，尚未落库）。"""

    chunk_seq: int
    knowledge_layer: str
    material_id: str
    material_version: int
    content_hash: str
    section_title: Optional[str] = None
    text: str
    page_no: Optional[int] = None
    paragraph_no: Optional[int] = None
    ocr_confidence: Optional[float] = None
    project_id: Optional[str] = None
    lot_id: Optional[str] = None
    owner_type: str
    permission_scope: str
    classification: str
    verification_status: str = "pending_verification"
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    field_refs: list[str] = Field(default_factory=list)