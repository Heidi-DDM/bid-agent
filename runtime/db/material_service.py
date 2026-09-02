# F019 §4：材料导入/版本化 repository 层（SQLAlchemy）
# 流程（F018 §4.3 / F019 §4）：临时目录 -> SHA-256 -> 格式/大小校验 ->
# 原子移动（objects.ingest）-> 写 materials（新版本行）+ material_versions（不可变内容引用）+ 审计。
# 规则：
# - 内容重复 -> 返回既有版本（created=False），不新建（F019 §4）；
# - 内容变化 -> 版本递增，旧版本保留可回放；
# - raw 不可覆盖：本模块不提供更新 content_hash/version 的路径（F003 §4.2）；
# - 哈希复核失败 -> 阻断并写审计事件（F019 §4）。
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.core import versioning
from runtime.core.objects import (
    HashMismatchError,
    ObjectStoreError,
    UploadResult,
    ingest,
    sha256_file,
    verify_object,
)
from runtime.db.models import AuditEvent, Material, MaterialVersion

logger = logging.getLogger("runtime.materials")

MATERIAL_TYPES = {
    "announcement", "tender_document", "qualification_cert",
    "performance_record", "personnel_cert", "evidence_file",
}
SOURCE_TYPES = {"official_platform", "agency", "uploaded", "internal", "manual_entry"}
OWNER_TYPES = {"public", "enterprise"}
CLASSIFICATIONS = {"public", "internal", "confidential"}
PERMISSION_SCOPES = {"public_read", "enterprise_read", "restricted", "approver_only"}


class MaterialError(Exception):
    pass


class ValidationError(MaterialError):
    pass


class HashMismatchBlocked(MaterialError):
    """哈希复核失败：任务阻断并已写审计事件（F019 §4）。"""


@dataclass
class ImportedMaterial:
    material: Material
    version_record: MaterialVersion
    created: bool
    duplicate_of: Optional[int] = None  # created=False 时的既有版本


@dataclass
class AuditRecord:
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _validate_enums(**kwargs: str) -> None:
    checks = {
        "material_type": (kwargs.get("material_type"), MATERIAL_TYPES),
        "source_type": (kwargs.get("source_type"), SOURCE_TYPES),
        "owner_type": (kwargs.get("owner_type"), OWNER_TYPES),
        "classification": (kwargs.get("classification"), CLASSIFICATIONS),
        "permission_scope": (kwargs.get("permission_scope"), PERMISSION_SCOPES),
    }
    for name, (value, allowed) in checks.items():
        if value not in allowed:
            raise ValidationError(f"不支持的值 {name}={value!r}，允许 {sorted(allowed)}")


def _audit(
    session: Session,
    *,
    actor: str,
    action: str,
    basis: str | None = None,
    outcome: str | None = None,
    object_ref: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_id=uuid.uuid4().hex[:16],
        actor=actor,
        at=datetime.now(timezone.utc),
        action=action,
        basis=basis,
        outcome=outcome,
        object_ref=object_ref,
    )
    session.add(event)
    return event


def _existing_hashes(session: Session, material_id: str) -> dict[int, str]:
    rows = session.execute(
        select(MaterialVersion.version, MaterialVersion.content_hash)
        .where(MaterialVersion.material_id == material_id)
        .order_by(MaterialVersion.version)
    ).all()
    return {version: content_hash for version, content_hash in rows}


def import_material(
    session: Session,
    *,
    src_path: str,
    material_id: str,
    material_type: str,
    source_type: str,
    owner_type: str,
    classification: str,
    permission_scope: str,
    data_owner: str,
    store_root: str,
    project_id: Optional[str] = None,
    valid_until: Optional[date] = None,
    actor: str = "system",
    virus_check: Optional[Callable[[str], None]] = None,
) -> ImportedMaterial:
    """导入材料（文件 -> 对象存储 -> 版本记录）。

    内容重复：返回既有版本（created=False）；内容变化：版本递增。
    校验失败/对象存储异常：不写任何记录并回滚。
    """
    _validate_enums(
        material_type=material_type,
        source_type=source_type,
        owner_type=owner_type,
        classification=classification,
        permission_scope=permission_scope,
    )
    if not data_owner:
        raise ValidationError("data_owner 必填（数据责任人制，F003 §4.1）")

    digest = sha256_file(src_path)  # 先算哈希，防篡改与去重（F019 §4）
    existing = _existing_hashes(session, material_id)
    decision = versioning.decide_version(existing, digest)

    if not decision.created:
        current = session.scalar(
            select(Material).where(
                Material.material_id == material_id,
                Material.version == decision.existing_version,
            )
        )
        version_record = session.scalar(
            select(MaterialVersion).where(
                MaterialVersion.material_id == material_id,
                MaterialVersion.version == decision.existing_version,
            )
        )
        _audit(session, actor=actor, action="material.duplicate",
               basis=f"content_hash={digest[:16]}...", outcome=f"reuse_v{decision.existing_version}",
               object_ref=material_id)
        session.commit()
        return ImportedMaterial(material=current, version_record=version_record,
                                created=False, duplicate_of=decision.existing_version)

    # 新版本：对象存储落盘（原子移动）后再写版本记录
    uploaded: UploadResult = ingest(
        src_path,
        owner_type=owner_type,
        material_id=material_id,
        version=decision.version,
        store_root=store_root,
        material_type=material_type,
        virus_check=virus_check,
    )

    material = Material(
        material_id=material_id,
        version=decision.version,
        material_type=material_type,
        source_type=source_type,
        owner_type=owner_type,
        classification=classification,
        permission_scope=permission_scope,
        content_hash=uploaded.stored.content_hash,
        parse_status="pending",
        status=versioning.material_status(valid_until, date.today()),
        valid_until=valid_until,
        evidence_refs=[],
        data_owner=data_owner,
        project_id=project_id,
    )
    version_record = MaterialVersion(
        material_id=material_id,
        version=decision.version,
        object_uri=uploaded.stored.object_uri,
        content_hash=uploaded.stored.content_hash,
    )
    session.add(material)
    session.add(version_record)
    _audit(session, actor=actor, action="material.import",
           basis=f"sha256={digest[:16]}...", outcome=f"v{decision.version}",
           object_ref=material_id)
    session.commit()
    return ImportedMaterial(material=material, version_record=version_record, created=True)


def current_material(session: Session, material_id: str) -> Optional[Material]:
    """当前版本（最高版本号）的 Material 元数据。"""
    return session.scalar(
        select(Material)
        .where(Material.material_id == material_id)
        .order_by(Material.version.desc())
        .limit(1)
    )


def list_versions(session: Session, material_id: str) -> list[MaterialVersion]:
    """版本历史（旧版本可回放，F019 §6 验收②）。"""
    return list(
        session.scalars(
            select(MaterialVersion)
            .where(MaterialVersion.material_id == material_id)
            .order_by(MaterialVersion.version)
        )
    )


def get_version(session: Session, material_id: str, version: int) -> Optional[MaterialVersion]:
    return session.scalar(
        select(MaterialVersion).where(
            MaterialVersion.material_id == material_id,
            MaterialVersion.version == version,
        )
    )


def update_metadata(
    session: Session,
    material_id: str,
    *,
    parse_status: Optional[str] = None,
    data_owner: Optional[str] = None,
    verified_at: Optional[datetime] = None,
    valid_until: Optional[date] = None,
    actor: str = "system",
) -> Optional[Material]:
    """更新元数据（parse_status/data_owner/verified_at/valid_until）。

    content_hash 与 version 不可更新（raw 不可覆盖，F003 §4.2）；
    这里只允许修改当前版本行的元数据，不提供内容变更路径。
    """
    material = current_material(session, material_id)
    if material is None:
        return None
    changed: list[str] = []
    if parse_status is not None:
        material.parse_status = parse_status
        changed.append(f"parse_status={parse_status}")
    if data_owner is not None:
        material.data_owner = data_owner
        changed.append(f"data_owner={data_owner}")
    if verified_at is not None:
        material.verified_at = verified_at
        changed.append(f"verified_at={verified_at.isoformat()}")
    if valid_until is not None:
        material.valid_until = valid_until
        material.status = versioning.material_status(valid_until, date.today())
        changed.append(f"valid_until={valid_until.isoformat()}")
    if changed:
        _audit(session, actor=actor, action="material.metadata_update",
               basis="; ".join(changed), outcome="updated", object_ref=material_id)
        session.commit()
    return material


def verify_material(
    session: Session,
    material_id: str,
    *,
    store_root: str,
    actor: str = "system",
) -> bool:
    """哈希复核（F019 §6 验收④）：复核所有版本对象文件。

    任一文件缺失或哈希不一致 -> 阻断并写审计事件（HashMismatchBlocked），
    匹配任务不得启动（调用方应据此阻断）。
    """
    version_records = list_versions(session, material_id)
    if not version_records:
        return True
    for record in version_records:
        if not verify_object(store_root, record.object_uri):
            _audit(session, actor=actor, action="material.hash_mismatch",
                   basis=f"object_uri={record.object_uri}",
                   outcome="blocked", object_ref=material_id)
            session.commit()
            raise HashMismatchBlocked(
                f"哈希复核失败：{material_id} v{record.version}（object_uri={record.object_uri}），已阻断并审计"
            )
    _audit(session, actor=actor, action="material.verify",
           basis=f"versions={len(version_records)}", outcome="hash_ok", object_ref=material_id)
    session.commit()
    return True