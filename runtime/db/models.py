# F019 §3 analysis_jobs / F018 §4.2：任务持久化模型
# 幂等键唯一约束：job_id 主键 + (kind, input_ref, project_id) 唯一索引。
# 本文件在缺少 SQLAlchemy 环境下不 import（由迁移/服务层按需引入），
# 保证纯逻辑模块 py_compile 与测试不依赖第三方库。
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# pgvector 向量列（F025 §7）：未安装 pgvector 时模型定义降级为文本占位，
# 保证纯逻辑/沙盒测试可 import；真实向量索引必须安装 pgvector（requirements-dev.txt）。
try:  # pragma: no cover - 依赖探测
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover
    Vector = None  # type: ignore[assignment,misc]


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# JSON 在 PostgreSQL 落为 jsonb、其余方言（sqlite 测试）落为 json
JSONB = JSON().with_variant(postgresql.JSONB, "postgresql")


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
    # 执行结果摘要（announcement.search 的逐源结果 [{source_id,status,count,note}]，
    # 供 GET 轮询如实展示；其他 kind 暂不使用）
    result_summary: Mapped[list | None] = mapped_column(JSONB)
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


# ============================================================
# F019 §2/§3：数据库逻辑分层（public_data / enterprise_data /
# admission_data / audit_data）与核心表。
# 字段契约：F003 §4.1 Material 15 字段、F006 §4、F007 §4、
# F008 §4、F009 §4；本文件只规定存储实现，不改写业务契约。
# ============================================================


class ParseCandidate(Base):
    """R021/F021 §2.7：招标解析候选与人工复核表（public_data schema）。

    解析器产出 RuleCandidate/MainCardCandidate 暂存本表，投标专员复核
    （approved/rejected/revised）后才写入 RuleSet/Requirement/FieldTrace；
    复核前不产生任何规则与字段（F021 人工确认门禁）。
    """

    __tablename__ = "parse_candidates"
    __table_args__ = (
        UniqueConstraint("candidate_id", "material_id", "version",
                         name="uq_parse_candidates_idem"),
        Index("ix_parse_candidates_material", "material_id", "version"),
        Index("ix_parse_candidates_project", "project_id"),
        Index("ix_parse_candidates_status", "status"),
        {"schema": "public_data"},
    )
    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    material_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # rule_candidate / main_card_field
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    # pending / approved / rejected / revised
    reviewer: Mapped[str | None] = mapped_column(String(128))
    revised_payload: Mapped[dict | None] = mapped_column(JSONB)
    review_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Project(Base):
    """F019 §3 projects：多版本材料按 project_id 挂接（F003 §4.2）。"""

    __tablename__ = "projects"
    __table_args__ = {"schema": "public_data"}

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_name: Mapped[str] = mapped_column(String(256), nullable=False)
    tender_document_ref: Mapped[str | None] = mapped_column(String(64))  # 当前招标文件 material_id
    admission_status: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Material(Base):
    """F019 §3 materials：F003 §4.1 Material 15 字段，(material_id, version) 唯一，版本只增。

    每版本一行；material_versions 保存不可变内容引用（object_uri/content_hash）。
    raw 不可覆盖：应用层不提供修改 version/content_hash 的更新路径（F003 §4.2）。
    """

    __tablename__ = "materials"
    __table_args__ = (
        # F019 §3：(material_id, version) 复合主键，每版本一行、版本只增
        PrimaryKeyConstraint("material_id", "version", name="pk_materials_id_version"),
        Index("ix_materials_owner_type", "owner_type"),
        Index("ix_materials_project_id", "project_id"),
        {"schema": "public_data"},
    )

    material_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    material_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # announcement / tender_document / qualification_cert / performance_record / personnel_cert / evidence_file
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # official_platform / agency / uploaded / internal / manual_entry
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)  # public / enterprise
    classification: Mapped[str] = mapped_column(String(16), nullable=False)  # public / internal / confidential
    permission_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    # public_read / enterprise_read / restricted / approver_only
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256，防篡改
    parse_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    # pending / parsed / partial / failed / manual_review
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    # active / expired / archived / invalid
    valid_until: Mapped[date | None] = mapped_column(Date)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # ref[]
    data_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class MaterialVersion(Base):
    """F019 §3 material_versions：不可变内容索引，content_hash 只增不改（无 UPDATE 应用路径）。"""

    __tablename__ = "material_versions"
    __table_args__ = (
        # F019 §3：与 materials 同构，复合主键 + 内容哈希索引
        PrimaryKeyConstraint("material_id", "version", name="pk_material_versions_id_version"),
        Index("ix_material_versions_hash", "content_hash"),
        {"schema": "public_data"},
    )

    material_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    object_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AnnouncementCandidate(Base):
    """R004/F020：公告搜索候选（public_data schema，manual_trigger 搜索结果暂存）。

    搜索任务只抓列表页 → 候选落本表（标题/来源/链接等列表页可确证事实；
    region/publish_date 等列表页不标注的字段一律 NULL=待补，不推断）；
    投标专员逐条确认后由 announcement.import_detail 任务抓详情原文入库
    （material 固化 + Project 建档），import_status 流转 pending → imported/failed。
    """

    __tablename__ = "announcement_candidates"
    __table_args__ = (
        Index("ix_announcement_candidates_job", "search_job_id"),
        Index("ix_announcement_candidates_project", "project_id"),
        Index("ix_announcement_candidates_import", "import_status"),
        {"schema": "public_data"},
    )
    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    search_job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(64))
    publish_date: Mapped[date | None] = mapped_column(Date)
    project_id: Mapped[str | None] = mapped_column(String(64))
    import_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # pending / imported / failed
    import_job_id: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class FieldTrace(Base):
    """F019 §3 field_traces：字段级溯源（F005：clause/source_link/assertion/confidence）。

    非空字段必须至少一条引用；source_hash 回链原文 hash。
    """

    __tablename__ = "field_traces"
    __table_args__ = (Index("ix_field_traces_object_field", "object_id", "field_key"), {"schema": "public_data"})

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 解析对象引用（material/requirement 等）
    field_key: Mapped[str] = mapped_column(String(128), nullable=False)
    clause: Mapped[str] = mapped_column(Text, nullable=False)  # 条款引用
    source_link: Mapped[str | None] = mapped_column(String(512))
    assertion: Mapped[str] = mapped_column(Text, nullable=False)  # 原文断言
    confidence: Mapped[str | None] = mapped_column(String(16))  # F005 §4.3 enum: confirmed/high/medium/low
    source_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class EvidenceFile(Base):
    """F019 §3 evidence_files / F006 §4.4：私有证据元数据（OCR 产物扩展）。"""

    __tablename__ = "evidence_files"
    __table_args__ = (Index("ix_evidence_files_material_id", "material_id"), {"schema": "enterprise_data"})

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_id: Mapped[str | None] = mapped_column(String(64))
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)  # 证书扫描件/合同/中标通知书/验收单
    object_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # 原文 hash，不可变
    page_no: Mapped[int | None] = mapped_column(Integer)  # OCR 页码定位
    ocr_confidence: Mapped[float | None] = mapped_column(Float)  # 低置信度 → 人工复核
    review_status: Mapped[str | None] = mapped_column(String(24))  # 0009：NULL/approved/rejected/pending_review/revised
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(16), nullable=False, default="internal")  # internal/confidential
    uploaded_by: Mapped[str] = mapped_column(String(128), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Qualification(Base):
    """F019 §3 qualifications / F006 §4.1：企业资质。缺证据默认 pending_verification。"""

    __tablename__ = "qualifications"
    __table_args__ = (
        Index("ix_qualifications_category_status", "category", "status"),
        {"schema": "enterprise_data"},
    )

    qualification_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_id: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(128), nullable=False)  # 建筑工程施工总承包/设计甲级/ISO 认证等
    level: Mapped[str | None] = mapped_column(String(32))  # 特级/一级/甲级
    specialty: Mapped[str | None] = mapped_column(String(128))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    issuer: Mapped[str | None] = mapped_column(String(256))
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    data_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_verification", index=True)
    # active / expired / pending_verification / archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Performance(Base):
    """F019 §3 performances / F006 §4.2：企业业绩。"""

    __tablename__ = "performances"
    __table_args__ = (Index("ix_performances_project_type", "project_type"), {"schema": "enterprise_data"})

    performance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_id: Mapped[str | None] = mapped_column(String(64))
    project_name: Mapped[str] = mapped_column(String(256), nullable=False)
    project_type: Mapped[str] = mapped_column(String(128), nullable=False)
    specialty: Mapped[str | None] = mapped_column(String(128))
    contract_amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    completed_at: Mapped[date | None] = mapped_column(Date)
    awarded_at: Mapped[date | None] = mapped_column(Date)
    owner_org: Mapped[str | None] = mapped_column(String(256))
    scale_metrics: Mapped[dict | None] = mapped_column(JSONB)  # 面积/长度/装机等，按招标要求配置
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    data_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_verification", index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # active / expired / pending_verification / archived；verified_at <= 判定时点才可入证据快照（F022 §2.3）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Manager(Base):
    """F019 §6 验收 / F007 §4 ProjectManagerProfile：可投标项目经理名录。

    在 F006 Personnel 之上建立经理视角，硬条件验证（F007 §6.1）所需字段全量持久化；
    不生成不存在的经理或证书资料（F007 §6.3），缺失字段一律 pending_verification。
    """

    __tablename__ = "managers"
    __table_args__ = (
        Index("ix_managers_status", "status"),
        Index("ix_managers_specialty", "specialty"),
        {"schema": "enterprise_data"},
    )

    manager_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # 如 PM-0001
    personnel_id: Mapped[str | None] = mapped_column(String(64))  # 回链 F006 Personnel（可选）
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)  # 默认脱敏展示（如 张**）
    organization: Mapped[str] = mapped_column(String(256), nullable=False)
    specialty: Mapped[str] = mapped_column(String(128), nullable=False)  # 建筑工程/市政/公路等
    reg_cert_type: Mapped[str] = mapped_column(String(32), nullable=False)  # 一级建造师/二级建造师等
    reg_cert_no: Mapped[str] = mapped_column(String(128), nullable=False)  # 唯一，格式校验；缺失待补
    cert_level: Mapped[str] = mapped_column(String(32), nullable=False)  # 一级/二级
    b_cert_no: Mapped[str | None] = mapped_column(String(128))  # 安全B证编号（0008 补齐；缺失=NULL=待补）
    cert_valid_until: Mapped[date | None] = mapped_column(Date)  # 缺失=NULL=待补（0008 改 nullable）
    edu_safety_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # valid / expiring / expired / pending
    eligible_project_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # 与项目类型匹配
    region_restriction: Mapped[str | None] = mapped_column(String(128))  # 如仅限河北省
    performance_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # 回链 F006
    active_projects: Mapped[list | None] = mapped_column(JSONB)  # 项目ID + 预计结束
    expected_available_at: Mapped[date | None] = mapped_column(Date)
    availability: Mapped[str] = mapped_column(String(16), nullable=False, default="available")
    # available / occupied / planning
    credit_penalty_status: Mapped[dict | None] = mapped_column(JSONB)  # 失信/处罚（有证据才维护）
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # 注册证书/社保/继续教育/业绩
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_verification", index=True)
    # active / unavailable / expired / pending_verification / archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Personnel(Base):
    """F019 §3 personnel / F006 §4.3：人员资料（建造师/职称/岗位），F007 在其上做项目经理视角。"""

    __tablename__ = "personnel"
    __table_args__ = (
        Index("ix_personnel_category_status", "category", "status"),
        Index("ix_personnel_name", "name"),
        {"schema": "enterprise_data"},
    )

    personnel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), nullable=False)  # 脱敏展示
    organization: Mapped[str | None] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    # registered_builder / technical_title / post_certificate / other
    specialty: Mapped[str | None] = mapped_column(String(128))  # 建筑/市政/机电/公路/水利水电等
    cert_level: Mapped[str | None] = mapped_column(String(32))  # 一级/二级建造师、正高/副高/中级/初级
    cert_no: Mapped[str | None] = mapped_column(String(128))  # 缺失标"待补"
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    issuer: Mapped[str | None] = mapped_column(String(256))
    on_site: Mapped[str | None] = mapped_column(String(16))  # 是/否/待核实
    on_site_project: Mapped[str | None] = mapped_column(String(256))
    phone: Mapped[str | None] = mapped_column(String(32))  # 仅授权用途
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    data_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_verification", index=True)
    # active / expired / pending_verification / archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class RuleSet(Base):
    """F019 §3 rule_sets / F008 §4.5：规则快照，(project_id, version) 唯一，版本不可覆盖。"""

    __tablename__ = "rule_sets"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_rule_sets_project_version"),
        {"schema": "admission_data"},
    )

    rule_set_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)  # 语义化版本
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 完整快照（Requirement/公式/默认缺失处置/准入政策）
    diff: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Requirement(Base):
    """F019 §3 requirements / F008 §4.1-4.3：三类要求（hard/scored/action）全字段集。"""

    __tablename__ = "requirements"
    __table_args__ = (
        Index("ix_requirements_rule_set", "rule_set_id"),
        Index("ix_requirements_req_type", "req_type"),
        {"schema": "admission_data"},
    )

    requirement_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rule_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    req_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # hard_requirement / scored_requirement / action_requirement
    category: Mapped[str | None] = mapped_column(String(128))
    lot_id: Mapped[str | None] = mapped_column(String(64))
    clause_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    assertion: Mapped[str] = mapped_column(Text, nullable=False)  # 逐字摘录
    rule: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 结构化条件表达式
    evidence_required: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    as_of: Mapped[str | None] = mapped_column(String(64))  # bid_deadline 或明确日期；不得默认当前时间
    missing_action: Mapped[str] = mapped_column(String(24), nullable=False, default="blocked_missing_data")
    # blocked_missing_data / manual_review
    failure_effect: Mapped[str | None] = mapped_column(String(32))  # not_qualified / blocked_hard_requirement
    priority: Mapped[int | None] = mapped_column(Integer)
    logic_group: Mapped[str | None] = mapped_column(String(64))
    operator: Mapped[str | None] = mapped_column(String(16))  # all / any / at_least_n
    consortium_role: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # none / leader / member
    max_score: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[float | None] = mapped_column(Float)
    score_nature: Mapped[str | None] = mapped_column(String(16))  # objective / subjective
    score_formula: Mapped[dict | None] = mapped_column(JSONB)
    score_inputs: Mapped[list | None] = mapped_column(JSONB)  # 经授权录入的报价输入等
    reuse_policy: Mapped[str | None] = mapped_column(String(32))
    required_by_stage: Mapped[str | None] = mapped_column(String(24))
    # approval_ready / submission_ready / submitted / opened
    action_status: Mapped[str | None] = mapped_column(String(24))
    # not_started / ready / completed / overdue / not_applicable
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MatchRun(Base):
    """F019 §3 match_runs：一次匹配运行（gate/diagnostic），记录输入快照引用。

    R025 扩展（docs/07 方案 §3.5）：retrieval_run_id / index_version / candidate_chunk_ids /
    structured_verification / evidence_snapshot_hash —— RAG 候选证据与结构化核验结果
    必须随快照保存，向量分数不进入准入公式。
    """

    __tablename__ = "match_runs"
    __table_args__ = (
        Index("ix_match_runs_project", "project_id"),
        Index("ix_match_runs_rule_set", "rule_set_id"),
        {"schema": "admission_data"},
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lot_id: Mapped[str | None] = mapped_column(String(64))
    as_of: Mapped[str] = mapped_column(String(64), nullable=False)  # 判定时点（不得默认当前时间）
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="gate")  # gate / diagnostic
    coverage: Mapped[dict | None] = mapped_column(JSONB)  # declared / executed / complete
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="completed")
    # ---- R025：RAG 候选与结构化核验快照（F023 §2.3/§6） ----
    retrieval_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    index_version: Mapped[str | None] = mapped_column(String(64))
    candidate_chunk_ids: Mapped[list | None] = mapped_column(JSONB)
    structured_verification: Mapped[dict | None] = mapped_column(JSONB)
    evidence_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MatchItem(Base):
    """F019 §3 match_items / F008 §4.4 MatchMatrix：逐条匹配结果（机器可读判定链）。"""

    __tablename__ = "match_items"
    __table_args__ = (
        Index("ix_match_items_run", "run_id"),
        Index("ix_match_items_requirement", "requirement_id"),
        {"schema": "admission_data"},
    )

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requirement_id: Mapped[str] = mapped_column(String(64), nullable=False)
    match_result: Mapped[str] = mapped_column(String(24), nullable=False)
    # satisfied / partial / not_satisfied / unverifiable / manual_review
    score: Mapped[float | None] = mapped_column(Float)  # 不可计算时 null，不得填 0 冒充
    max_score: Mapped[float | None] = mapped_column(Float)
    missing_items: Mapped[list | None] = mapped_column(JSONB)
    match_reason: Mapped[dict | None] = mapped_column(JSONB)  # 表达式/输入/证据/计算结果判定链
    evidence_refs: Mapped[list | None] = mapped_column(JSONB)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_version_refs: Mapped[list | None] = mapped_column(JSONB)  # 澄清/版本追溯
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AdmissionResult(Base):
    """F019 §3 admission_results / F008 §4.6：准入结果，不可变快照。"""

    __tablename__ = "admission_results"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_admission_results_run_id"),
        Index("ix_admission_results_project", "project_id"),
        {"schema": "admission_data"},
    )

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lot_id: Mapped[str | None] = mapped_column(String(64))
    rule_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    qualification_result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scoring_result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    operational_readiness: Mapped[dict] = mapped_column(JSONB, nullable=False)
    internal_admission_result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    internal_admission_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_score: Mapped[float | None] = mapped_column(Float)
    max_total_score: Mapped[float | None] = mapped_column(Float)
    score_gap_items: Mapped[list | None] = mapped_column(JSONB)
    blocked_items: Mapped[list | None] = mapped_column(JSONB)
    pending_items: Mapped[list | None] = mapped_column(JSONB)
    review_items: Mapped[list | None] = mapped_column(JSONB)
    manager_matches: Mapped[list | None] = mapped_column(JSONB)  # 主推荐/备选 + F007 硬条件结果
    explanation: Mapped[list | None] = mapped_column(JSONB)
    result_freshness: Mapped[str] = mapped_column(String(16), nullable=False, default="current")  # current / stale
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="final")  # 不可变快照
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Approval(Base):
    """F019 §3 approvals / F009 §4.1：审批记录，终态不可重复决策。"""

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_project", "project_id"), {"schema": "admission_data"})

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approver: Mapped[str] = mapped_column(String(128), nullable=False)  # 经营负责人
    decision: Mapped[str | None] = mapped_column(String(16))  # approved / rejected / waived
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)  # 驳回必填
    admission_result_ref: Mapped[str | None] = mapped_column(String(64))  # 回链 F008
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")  # pending / decided / blocked_waiver_expired / archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Waiver(Base):
    """F019 §3 waivers / F009 §4.2：人工豁免，过期自动失效回阻断。"""

    __tablename__ = "waivers"
    __table_args__ = (Index("ix_waivers_project", "project_id"), {"schema": "admission_data"})

    waiver_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    authorizer: Mapped[str] = mapped_column(String(128), nullable=False)  # 审批人
    reason: Mapped[str] = mapped_column(Text, nullable=False)  # 必填
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # 必填（在途材料/受理回执）
    valid_until: Mapped[date] = mapped_column(Date, nullable=False)  # 必填，过期自动失效
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    covered_items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # 具体豁免哪项
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active / expired / revoked
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditEvent(Base):
    """F019 §3 audit_events：仅追加审计，不提供 UPDATE/DELETE 应用路径（F009 §9）。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_object_ref", "object_ref"),
        Index("ix_audit_events_actor", "actor"),
        {"schema": "audit_data"},
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)  # 谁
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)  # 何时
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # 什么动作
    basis: Mapped[str | None] = mapped_column(Text)  # 依据
    outcome: Mapped[str | None] = mapped_column(String(256))  # 结论
    object_ref: Mapped[str | None] = mapped_column(String(256))  # 对象引用
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ============================================================
# F025 / R025：RAG 三层知识库（knowledge_data schema，docs/07 方案 §3.2）
# 原文/结构化事实仍存 public_data/enterprise_data/admission_data；
# 本 schema 只保存可重建的分片、向量索引与检索运行快照，用于证据发现与回放。
# ============================================================


class KnowledgeChunk(Base):
    """F025 §3.1 knowledge_chunks：统一 KnowledgeChunk。

    幂等：同一 (material_id, material_version, content_hash, embedding_model,
    index_version, chunk_seq) 只产生一行；重建索引时按唯一键跳过。
    材料新版本/澄清/证据核验变更 → 旧行 index_status 标记 stale，不得驱动审批。
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        # 唯一键即索引幂等键（方案 §8.3 index:... 的落库表达）
        UniqueConstraint(
            "material_id",
            "material_version",
            "content_hash",
            "embedding_model",
            "index_version",
            "chunk_seq",
            name="uq_knowledge_chunks_index_idem",
        ),
        Index("ix_knowledge_chunks_layer_status", "knowledge_layer", "verification_status"),
        Index("ix_knowledge_chunks_project", "project_id"),
        Index("ix_knowledge_chunks_material", "material_id", "material_version"),
        Index("ix_knowledge_chunks_index_version", "index_version"),
        {"schema": "knowledge_data"},
    )

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chunk_seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 材料内分片序号（父子顺序）
    knowledge_layer: Mapped[str] = mapped_column(String(16), nullable=False)
    # L1_public / L2_tender / L3_enterprise
    material_id: Mapped[str] = mapped_column(String(64), nullable=False)
    material_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    section_title: Mapped[str | None] = mapped_column(String(256))  # 文档/章节/条款父子层级
    text: Mapped[str] = mapped_column(Text, nullable=False)
    field_refs: Mapped[list | None] = mapped_column(JSONB)  # F005/F006 已核验字段引用
    page_no: Mapped[int | None] = mapped_column(Integer)  # 页码定位（PDF/DOCX/OCR）
    paragraph_no: Mapped[int | None] = mapped_column(Integer)  # 段落序号
    ocr_confidence: Mapped[float | None] = mapped_column(Float)  # OCR 分片置信度
    project_id: Mapped[str | None] = mapped_column(String(64))
    lot_id: Mapped[str | None] = mapped_column(String(64))
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)  # public / enterprise
    permission_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    # public_read / enterprise_read / restricted / approver_only（继承材料，不得提升）
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False)
    # pending_verification / active / expired / invalid
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version: Mapped[str] = mapped_column(String(64), nullable=False)
    index_status: Mapped[str] = mapped_column(String(16), nullable=False, default="current", index=True)
    # current / stale
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class KnowledgeEmbedding(Base):
    """F025 §7 knowledge_embeddings：chunk 向量，可重建、可回放。

    唯一键 (chunk_id, embedding_model, index_version) 防重复索引；
    pgvector 不可用时不得写入成功记录（任务进入 retryable/manual_review）。
    """

    __tablename__ = "knowledge_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "chunk_id",
            "embedding_model",
            "index_version",
            name="uq_knowledge_embeddings_chunk_model_version",
        ),
        {"schema": "knowledge_data"},
    )

    embedding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    index_version: Mapped[str] = mapped_column(String(64), nullable=False)
    vector: Mapped[object | None] = mapped_column(
        Vector(1024) if Vector is not None else Text, nullable=False  # type: ignore[assignment]
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RetrievalRun(Base):
    """F025 §7 retrieval_runs：每次检索运行快照，可回放、可审计。

    幂等：同一 (query_hash, filters_hash, index_version, as_of) 只保留一个 run；
    重复相同查询复用既有 run（方案 §11.3）。
    """

    __tablename__ = "retrieval_runs"
    __table_args__ = (
        UniqueConstraint(
            "query_hash",
            "filters_hash",
            "index_version",
            "as_of",
            name="uq_retrieval_runs_query_filters",
        ),
        Index("ix_retrieval_runs_project", "project_id"),
        {"schema": "knowledge_data"},
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256(query)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)  # 回放用途；日志不落原文
    filters_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256(过滤条件)
    knowledge_layers: Mapped[list] = mapped_column(JSONB, nullable=False)
    permission_scope: Mapped[str | None] = mapped_column(String(255))  # 逗号连接的可见 scope 集（≤4 个，audit 用途）
    project_id: Mapped[str | None] = mapped_column(String(64))
    lot_id: Mapped[str | None] = mapped_column(String(64))
    as_of: Mapped[str] = mapped_column(String(64), nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="hybrid")
    # hybrid / keyword / vector
    index_version: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")
    # completed / failed / retryable
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)