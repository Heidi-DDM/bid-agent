"""R022 企业资料导入与核验服务（F022 §2 / F006 §4-§6）。

范围：
- 导入归一化：占位符 → __待补__；Excel 序列日期 → ISO（F006 §6.7 v1.2，
  基准 1899-12-30，越界标 __待核实__）；文本日期归一。
- 批量导入（幂等）：资质/业绩/建造师(→Managers+Personnel)/职称(→Personnel)/
  岗位证书(→Personnel)；无证据默认 pending_verification，不静默转 active。
- 核验队列与流转：pending_verification → active（写 verified_at + 审计）/
  rejected；过期自动 expired；无 evidence_refs 不可转 active；重导入 =
  同业务键 + 原 rejected/archived 行 → 开新版本记录（幂等键避开）。

设计原则（F022 §4/§5）：
- 每条记录必须有 material_id / evidence_refs / data_owner；无证据不可转 active。
- 缺失字段一律 __待补__，不推断、不编造（AGENTS.md 强制规则 1/2）。
- 所有流转写审计（audit_data.audit_events 仅追加）；导入批次回滚不删审计。
- 服务函数不 commit（由路由/调用方统一提交），与 api_service 风格一致。
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.core.errors import ApiError
from runtime.db.models import (
    EvidenceFile,
    Manager,
    Performance,
    Personnel,
    Qualification,
)
from runtime.db.api_service import audit

# ── 归一化工具（纯函数，可单测）──────────────────────────────

_PLACEHOLDER_RE = re.compile(r"后续补充|不用填|待补|占位|待核实|无$|^$|None|null")
# 注意：__待补__ / __待核实__ 是系统标记值，不作为占位符输入再归一


def normalize_placeholder(v: Any) -> Any:
    """空值/占位符统一为 None（入库时写 __待补__）；'是'/'否'等保留。

    设计：归一结果是 None → 落库时字段写 '__待补__'（保持 AGENTS 显式缺省），
    但日期字段 None 应写 '__待核实__'？—— 日期列是 Date 类型不能存字符串，
    因此日期归一函数单独处理，返回 None 表示缺失（落库 NULL = 待补语义）。
    注意：list/dict（active_projects/evidence_refs/eligible_project_types 等
    JSONB 列）原样返回，绝不做 str()（否则空 list 变字符串 '[]'，JSONB 语义破坏）。
    """
    if v is None:
        return None
    if isinstance(v, (list, dict, bool, int, float)):
        return v
    s = str(v).strip()
    if not s:
        return None
    # 纯字符串占位符判定
    if _PLACEHOLDER_RE.fullmatch(s):
        return None
    return s


def excel_serial_to_iso(v: Any) -> str | None:
    """Excel 序列日期 → ISO 日期字符串；无法解析 → None（落库 NULL = 待补）。

    F006 §6.7 v1.2：Excel 1900 日期系统基准 1899-12-30；越界（<2000 或
    >2100）不推断，返回 None（调用方记待核实/待补）。
    文本日期 '2025-10-30' / '2025/10/30' 原样归一。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # 已是 ISO / 常见文本日期
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    # Excel 序列号（可能带 .0 小数）
    try:
        serial = float(s)
    except ValueError:
        return None
    if serial.is_integer() is False and serial > 60000:
        return None  # 非日期数值
    d = date(1899, 12, 30) + timedelta(days=int(round(serial)))
    if not (date(2000, 1, 1) <= d <= date(2100, 12, 31)):
        return None  # 越界 → 待核实（不推断）
    return d.isoformat()


def parse_cert_level(level_raw: Any) -> tuple[str | None, str | None]:
    """'一级建造师' → (一级建造师, 一级)；无法解析 → (None, None)。

    reg_cert_type 需要完整类型串（Manager.reg_cert_type 非空 NOT NULL），
    cert_level 取等级短词。
    """
    s = normalize_placeholder(level_raw)
    if s is None:
        return None, None
    m = re.match(r"^(一级|二级|正高|副高|中级|初级|助理)", s)
    grade = m.group(1) if m else None
    return s, grade


def parse_on_site(v: Any) -> tuple[str, str | None]:
    """在施状态 → (availability, on_site_project)。'是'→occupied；'否'→available。

    availability 列口径（F007 §6）：available / occupied / planning / __待补__。
    占位符 → ('__待补__', None)；带项目名的'是'行同时给出项目（Manager.active_projects）。
    """
    s = normalize_placeholder(v)
    if s is None:
        return "__待补__", None
    if s.startswith("是"):
        return "occupied", None
    if s.startswith("否"):
        return "available", None
    return "__待补__", None


def _iso_to_date(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


def _iso_to_dt(v: str | None) -> datetime | None:
    """ISO 日期/时间串 → aware datetime；解析失败 → None（待补不推断）。"""
    d = _iso_to_date(v)
    if d is None:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _resolve_import_state(row: dict, raw: dict) -> tuple[str, datetime | None]:
    """导入行状态解析（F006 §6.5 / F022 §2.7）：

    - 行级显式 status=active 且带证据（evidence_refs 非空）+ verified_at
      （存量已核验资料重导入，如脱敏证据快照）→ 保留 active + 原核验时间；
    - 否则一律 pending_verification（现导入默认待核验，无证据不可转 active）。
    """
    evidence = raw.get("evidence_refs") or []
    want_active = str(raw.get("status") or row.get("status") or "") == "active"
    verified_raw = raw.get("verified_at") or row.get("verified_at")
    verified_at = _iso_to_dt(excel_serial_to_iso(verified_raw)) if verified_raw else None
    if want_active and evidence and verified_at is not None:
        return "active", verified_at
    return "pending_verification", None


def _display_mask(name: str | None) -> str | None:
    """姓名脱敏：保留首字，其余 *（F006 §4.3 / F022 §4 列表脱敏）。"""
    s = normalize_placeholder(name)
    if s is None:
        return None
    if len(s) <= 1:
        return s
    return s[0] + "*" * (len(s) - 1)


# ── 幂等业务键 ─────────────────────────────────────────────

def _key_part(v: Any) -> str:
    """key 片段归一：None/空 与 '__待补__' 等价（落库的占位符不破坏幂等键）。"""
    s = str(v or "").strip()
    if s in ("__待补__", "__待核实__"):
        return ""
    return s


def _qualification_key(row: dict) -> str:
    return "|".join([
        _key_part(row.get("category")),
        _key_part(row.get("level")),
        _key_part(row.get("specialty")),
        _key_part(row.get("issuer")),
    ])


def _performance_key(row: dict) -> str:
    return "|".join([
        _key_part(row.get("project_name")),
        _key_part(row.get("owner_org")),
    ])


def _personnel_key(row: dict, category: str) -> str:
    return "|".join([
        _key_part(category),
        _key_part(row.get("name")),
        _key_part(row.get("specialty")),
        _key_part(row.get("cert_no")),
    ])


def _manager_key(row: dict) -> str:
    return "|".join([
        _key_part(row.get("reg_cert_no")),
        _key_part(row.get("display_name")),
    ])


# ── 批量导入（幂等）────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def import_qualifications(
    session: Session, rows: Iterable[dict], *, data_owner: str, actor: str,
    source: str = "manual", material_id: str | None = None,
) -> dict:
    """批量导入资质（幂等：category+level+specialty+issuer 已存在则跳过）。

    rows 支持键：category/level/specialty/valid_from/valid_until/issuer/
    evidence_refs/data_owner；缺省字段写 __待补__（String 列）或 NULL（Date）。
    默认 status=pending_verification（F006 §6.5：现有清单默认待核验）。
    """
    existing = set()
    for q in session.scalars(select(Qualification)).all():
        existing.add(_qualification_key({
            "category": q.category, "level": q.level,
            "specialty": q.specialty, "issuer": q.issuer,
        }))
    created = skipped = 0
    for raw in rows:
        row = {k: normalize_placeholder(v) for k, v in raw.items()}
        key = _qualification_key(row)
        if key in existing:
            skipped += 1
            continue
        q = Qualification(
            qualification_id=f"Q-{_now_utc().strftime('%s')}-{created+1:04d}-{uuid.uuid4().hex[:4]}",
            material_id=material_id or raw.get("material_id"),
            category=row.get("category") or "__待补__",
            level=row.get("level") or "__待补__",
            specialty=row.get("specialty") or "__待补__",
            valid_from=_iso_to_date(excel_serial_to_iso(raw.get("valid_from"))),
            valid_until=_iso_to_date(excel_serial_to_iso(raw.get("valid_until"))),
            issuer=row.get("issuer") or "__待补__",
            evidence_refs=raw.get("evidence_refs") or [],
            data_owner=row.get("data_owner") or data_owner,
            status=_resolve_import_state(row, raw)[0],
            verified_at=_resolve_import_state(row, raw)[1],
        )
        session.add(q)
        audit(session, actor=actor, action="enterprise.qualification.import",
              basis=f"source={source} category={q.category}",
              outcome=f"status={q.status}", object_ref=q.qualification_id)
        existing.add(key)
        created += 1
    return {"kind": "qualifications", "created": created, "skipped": skipped}


def import_performances(
    session: Session, rows: Iterable[dict], *, data_owner: str, actor: str,
    source: str = "manual", material_id: str | None = None,
) -> dict:
    """批量导入业绩（幂等：project_name+owner_org 已存在则跳过）。

    支持键：project_name/project_type/specialty/contract_amount(万元→元 乘1e4)/
    completed_at/awarded_at/owner_org/scale_metrics(面积 area)/evidence_refs。
    """
    existing = set()
    for p in session.scalars(select(Performance)).all():
        existing.add(_performance_key({
            "project_name": p.project_name, "owner_org": p.owner_org,
        }))
    created = skipped = 0
    for raw in rows:
        row = {k: normalize_placeholder(v) for k, v in raw.items()}
        key = _performance_key(row)
        if key in existing:
            skipped += 1
            continue
        amount_wan = raw.get("contract_amount_wan") or raw.get("contract_amount")
        scale = raw.get("scale_metrics") or {}
        if isinstance(raw.get("scale"), (int, float, str)) and str(raw.get("scale", "")).strip():
            scale.setdefault("area", float(raw["scale"]) if str(raw["scale"]).replace(".", "", 1).isdigit() else None)
        p = Performance(
            performance_id=f"PF-{_now_utc().strftime('%s')}-{created+1:04d}-{uuid.uuid4().hex[:4]}",
            material_id=material_id or raw.get("material_id"),
            project_name=row.get("project_name") or "__待补__",
            project_type=row.get("project_type") or "__待补__",
            specialty=row.get("specialty") or "__待补__",
            contract_amount=(float(amount_wan) * 1e4 if amount_wan is not None
                             else None),
            completed_at=_iso_to_date(excel_serial_to_iso(raw.get("completed_at"))),
            awarded_at=_iso_to_date(excel_serial_to_iso(raw.get("awarded_at"))),
            owner_org=row.get("owner_org") or "__待补__",
            scale_metrics=scale or None,
            evidence_refs=raw.get("evidence_refs") or [],
            data_owner=row.get("data_owner") or data_owner,
            status=_resolve_import_state(row, raw)[0],
            verified_at=_resolve_import_state(row, raw)[1],
        )
        session.add(p)
        audit(session, actor=actor, action="enterprise.performance.import",
              basis=f"source={source} project={p.project_name}",
              outcome=f"status={p.status}", object_ref=p.performance_id)
        existing.add(key)
        created += 1
    return {"kind": "performances", "created": created, "skipped": skipped}


def import_personnel(
    session: Session, rows: Iterable[dict], *, category: str, data_owner: str,
    actor: str, source: str = "manual",
) -> dict:
    """批量导入人员（职称/岗位证书/建造师，F006 §4.3）。

    category ∈ registered_builder / technical_title / post_certificate。
    建造师额外写 Manager 视图（R022-③ 演示农大 M-ND-01 可走此链）。
    支持键：name/organization/category/specialty/cert_level/cert_no/valid_from/
    valid_until/issuer/on_site/on_site_project/phone/evidence_refs。
    """
    existing = set()
    for p in session.scalars(select(Personnel)).all():
        existing.add(_personnel_key({
            "name": p.name, "specialty": p.specialty, "cert_no": p.cert_no,
        }, category))
    created = skipped = 0
    for raw in rows:
        row = {k: normalize_placeholder(v) for k, v in raw.items()}
        key = _personnel_key(row, category)
        if key in existing:
            skipped += 1
            continue
        p = Personnel(
            personnel_id=f"P-{_now_utc().strftime('%s')}-{created+1:04d}-{uuid.uuid4().hex[:4]}",
            name=row.get("name") or "__待补__",
            organization=row.get("organization"),
            category=category,
            specialty=row.get("specialty"),
            cert_level=row.get("cert_level"),
            cert_no=row.get("cert_no") or "__待补__",
            valid_from=_iso_to_date(excel_serial_to_iso(raw.get("valid_from"))),
            valid_until=_iso_to_date(excel_serial_to_iso(raw.get("valid_until"))),
            issuer=row.get("issuer"),
            on_site=row.get("on_site"),
            on_site_project=row.get("on_site_project"),
            phone=row.get("phone"),
            evidence_refs=raw.get("evidence_refs") or [],
            data_owner=row.get("data_owner") or data_owner,
            status=_resolve_import_state(row, raw)[0],
            verified_at=_resolve_import_state(row, raw)[1],
        )
        session.add(p)
        audit(session, actor=actor, action=f"enterprise.{category}.import",
              basis=f"source={source} name={p.name}",
              outcome=f"status={p.status}", object_ref=p.personnel_id)
        existing.add(key)
        created += 1
    return {"kind": category, "created": created, "skipped": skipped}


def import_managers(
    session: Session, rows: Iterable[dict], *, data_owner: str, actor: str,
    source: str = "manual",
) -> dict:
    """批量导入项目经理名录（F007 §4 ProjectManagerProfile / F022 §3）。

    支持键：display_name(或 name)/organization/specialty(可列表)/reg_cert_type/
    reg_cert_no/cert_level/cert_valid_until/edu_safety_status/availability/
    active_projects/performance_refs/evidence_refs/b_cert_no(0008 迁移后)。
    display_name 脱敏展示；reg_cert_no 缺失写 __待补__（NOT NULL 列）。
    """
    existing = set()
    for m in session.scalars(select(Manager)).all():
        existing.add(_manager_key({
            "display_name": m.display_name, "reg_cert_no": m.reg_cert_no,
        }))
    created = skipped = 0
    for raw in rows:
        row = {k: normalize_placeholder(v) for k, v in raw.items()}
        # display_name：调用方负责脱敏（可传已脱敏名或 ID）；仅 fallback 裸 name 时才 mask
        disp = row.get("display_name")
        if disp is None and raw.get("name"):
            disp = _display_mask(raw["name"])
        disp = normalize_placeholder(disp)
        key = _manager_key({"display_name": disp or "", "reg_cert_no": row.get("reg_cert_no") or ""})
        if key in existing:
            skipped += 1
            continue
        specialty = row.get("specialty") or raw.get("specialty")
        if isinstance(specialty, list):
            specialty = "/".join(x for x in specialty if x)
        cert_type_raw = row.get("reg_cert_type") or raw.get("reg_cert_type") or "一级建造师"
        cert_type, cert_grade = parse_cert_level(cert_type_raw)
        availability = row.get("availability") or raw.get("availability") or "__待补__"
        active_projects = row.get("active_projects") or []
        cert_until = _iso_to_date(excel_serial_to_iso(
            row.get("cert_valid_until") or raw.get("cert_valid_until")))
        m = Manager(
            manager_id=f"PM-{_now_utc().strftime('%s')}-{created+1:04d}-{uuid.uuid4().hex[:4]}",
            display_name=disp or "__待补__",
            organization=row.get("organization") or "某建设集团",
            specialty=specialty or "__待补__",
            reg_cert_type=cert_type or "一级建造师",
            reg_cert_no=row.get("reg_cert_no") or "__待补__",
            b_cert_no=raw.get("b_cert_no") or row.get("b_cert_no"),
            cert_level=cert_grade or row.get("cert_level") or "__待补__",
            cert_valid_until=cert_until,  # NULL=待补（0008 后 nullable；不伪造占位日期）
            edu_safety_status=row.get("edu_safety_status") or "pending",
            eligible_project_types=row.get("eligible_project_types") or [],
            active_projects=active_projects or None,
            availability=availability,
            performance_refs=row.get("performance_refs") or [],
            evidence_refs=raw.get("evidence_refs") or [],
            data_owner=row.get("data_owner") or data_owner,
            status=_resolve_import_state(row, raw)[0],
            verified_at=_resolve_import_state(row, raw)[1],
        )
        session.add(m)
        audit(session, actor=actor, action="enterprise.manager.import",
              basis=f"source={source} name={m.display_name}",
              outcome=f"status={m.status}", object_ref=m.manager_id)
        existing.add(key)
        created += 1
    return {"kind": "managers", "created": created, "skipped": skipped}


def import_evidence_file(
    session: Session, *, file_type: str, object_uri: str, source_hash: str,
    uploaded_by: str, material_id: str | None = None, page_no: int | None = None,
    ocr_confidence: float | None = None, classification: str = "internal",
    valid_until: str | None = None,
) -> EvidenceFile:
    """登记证据文件元数据（F019 §3 evidence_files）。低置信度进入复核队列由
    ocr.review 端点处理；本函数只落元数据，不静默改业务记录状态。"""
    ev = EvidenceFile(
        evidence_id=f"E-{_now_utc().strftime('%s')}",
        material_id=material_id,
        file_type=file_type,
        object_uri=object_uri,
        source_hash=source_hash,
        page_no=page_no,
        ocr_confidence=ocr_confidence,
        classification=classification,
        uploaded_by=uploaded_by,
        valid_until=_iso_to_date(excel_serial_to_iso(valid_until)) if valid_until else None,
    )
    session.add(ev)
    return ev


# ── 核验队列与流转（F022 §2.7 / §5）────────────────────────

_MODEL_BY_KIND = {
    "qualifications": Qualification,
    "performances": Performance,
    "personnel": Personnel,
    "managers": Manager,
}
_KIND_LABEL = {
    "qualifications": ("qualification_id", "category"),
    "performances": ("performance_id", "project_name"),
    "personnel": ("personnel_id", "name"),
    "managers": ("manager_id", "display_name"),
}


def _kind_model(kind: str):
    model = _MODEL_BY_KIND.get(kind)
    if model is None:
        raise ApiError("invalid_request", f"未知资料类型: {kind}（允许 {sorted(_MODEL_BY_KIND)}）")
    return model


def list_verification_queue(
    session: Session, *, kind: str | None = None, status: str = "pending_verification",
    limit: int = 200,
) -> list[dict]:
    """核验队列：pending_verification（默认）或 rejected/active/expired 记录。"""
    queue = []
    kinds = [kind] if kind else list(_MODEL_BY_KIND)
    for k in kinds:
        model = _kind_model(k)
        pk_col, label_col = _KIND_LABEL[k]
        rows = session.scalars(
            select(model).where(getattr(model, "status") == status).order_by(getattr(model, pk_col))
        ).all()
        for r in rows:
            queue.append({
                "kind": k,
                "id": getattr(r, pk_col),
                "label": str(getattr(r, label_col)),
                "status": r.status,
                "verified_at": r.verified_at.isoformat() if r.verified_at else None,
                "evidence_refs": r.evidence_refs or [],
                "data_owner": r.data_owner,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
            if len(queue) >= limit:
                return queue
    return queue


def verify_records(
    session: Session, *, kind: str, ids: list[str], action: str, actor: str,
    comment: str | None = None,
) -> dict:
    """批量核验：pending_verification → active（填 verified_at+审计）/ rejected。

    - approve：无 evidence_refs 禁止转 active（F022 §2.6「无证据不可转 active」），
      抛 ApiError 不部分提交。
    - reject：置 rejected（保留记录，可重导入），写审计。
    - 过期自动 expired：valid_until < today 的 approve 会先落 expired 并提示。
    """
    if action not in ("approve", "reject"):
        raise ApiError("invalid_request", f"action 必须为 approve/reject，收到 {action}")
    model = _kind_model(kind)
    pk_col, label_col = _KIND_LABEL[kind]
    now = _now_utc()
    today = now.date()
    changed = 0
    skipped = 0
    no_evidence: list[str] = []
    expired: list[str] = []
    for oid in ids:
        obj = session.get(model, oid)
        if obj is None or obj.status not in ("pending_verification", "rejected", "active"):
            skipped += 1
            continue
        if obj.status == "active":
            skipped += 1
            continue
        if action == "approve":
            refs = obj.evidence_refs or []
            if not refs:
                no_evidence.append(str(getattr(obj, label_col)))
                continue
            # 过期自动 expired（F006 §6.2 / F022 §2.7）
            valid_until = getattr(obj, "valid_until", None)
            if valid_until is not None and valid_until < today:
                obj.status = "expired"
                obj.verified_at = now
                audit(session, actor=actor, action=f"enterprise.{kind}.verify",
                      basis=f"id={oid} 证据齐但已过期", outcome="expired",
                      object_ref=oid)
                expired.append(str(getattr(obj, label_col)))
                changed += 1
                continue
            obj.status = "active"
            obj.verified_at = now
            audit(session, actor=actor, action=f"enterprise.{kind}.verify",
                  basis=f"id={oid}{(' 备注: ' + comment) if comment else ''}",
                  outcome="active", object_ref=oid)
            changed += 1
        else:  # reject
            obj.status = "rejected"
            audit(session, actor=actor, action=f"enterprise.{kind}.verify",
                  basis=f"id={oid}{(' 原因: ' + comment) if comment else ''}",
                  outcome="rejected", object_ref=oid)
            changed += 1
    if no_evidence:
        raise ApiError(
            "invalid_request",
            f"{kind} 无证据不可转 active（F022 无证据不可参与匹配）: {no_evidence[:5]}"
            + (f" 等 {len(no_evidence)} 条" if len(no_evidence) > 5 else ""),
        )
    return {"kind": kind, "changed": changed, "skipped": skipped,
            "expired": expired, "no_evidence_blocked": len(no_evidence)}


def reimport_records(
    session: Session, *, kind: str, ids: list[str], actor: str,
) -> dict:
    """重导入：把 rejected/expired 记录重新置回 pending_verification（新核验周期）。

    设计：不删除历史（审计仅追加，F022 §2 批次可回滚但审计不可删），
    通过置回 pending 让同业务键的后续导入幂等键可复用；verified_at 清空
    （旧核验不再有效，须重新核验）。
    """
    model = _kind_model(kind)
    now = _now_utc()
    changed = 0
    for oid in ids:
        obj = session.get(model, oid)
        if obj is None or obj.status not in ("rejected", "expired"):
            continue
        obj.status = "pending_verification"
        obj.verified_at = None
        audit(session, actor=actor, action=f"enterprise.{kind}.reimport",
              basis=f"id={oid}", outcome="pending_verification", object_ref=oid)
        changed += 1
    return {"kind": kind, "reimported": changed}


def expire_overdue(session: Session) -> int:
    """把 active 且 valid_until < today 的记录置 expired（F006 §6.2 自动过期）。

    路由层定时/核验前调用；返回过期条数。不触发审计以外的副作用。
    """
    today = _now_utc().date()
    total = 0
    for kind, model in _MODEL_BY_KIND.items():
        if not hasattr(model, "valid_until"):
            continue
        rows = session.scalars(
            select(model).where(
                getattr(model, "status") == "active",
                getattr(model, "valid_until").isnot(None),
                getattr(model, "valid_until") < today,
            )
        ).all()
        for r in rows:
            r.status = "expired"
            audit(session, actor="system", action=f"enterprise.{kind}.expire",
                  basis=f"valid_until={r.valid_until} < today={today}",
                  outcome="expired", object_ref=str(getattr(r, _KIND_LABEL[kind][0])))
            total += 1
    return total
