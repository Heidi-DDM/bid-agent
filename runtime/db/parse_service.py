# R021/F021 §2.7：解析候选服务层
# 职责：
#   1) store_candidates —— 解析器产出的 RuleCandidate/MainCardCandidate 落库 parse_candidates
#      （幂等：同一 (candidate_id, material_id, version) 不重复插入；状态 pending）
#   2) review 决策 —— 投标专员 approved/rejected/revised（F005 §8 人工修正留痕：谁/何时/改了什么/依据）
#   3) 确认后写入 —— approved 的规则候选 → RuleSet/Requirement；主卡字段候选 → FieldTrace
#      （历史版本不可覆盖：RuleSet (project_id,version) 唯一；Requirement.rule_set_id 归属快照）
#   4) material.parse_status 流转：pending → parsed（全部 hard 候选确认后）/
#      manual_review（存在 rejected/缺候选待补）；不静默跳过、不编造。
#   5) grouped_requirements —— F021 §2.1 v1.3 解析结果聚合：写规则集前=parse_candidates 视图，
#      确认后=Requirement(+FieldTrace) 视图，按六组业务语言确定性分组（09-优化方案 §3.3）。
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from runtime.db.models import FieldTrace, Material, ParseCandidate, Requirement, RuleSet

CANDIDATE_KIND_RULE = "rule_candidate"
CANDIDATE_KIND_FIELD = "main_card_field"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_REVISED = "revised"
# F021 §2.1 v1.5：「确认本文件无此条款」——仅 missing_marker 候选允许；确认写入时不产出
# Requirement，但进 RuleSet.snapshot.not_applicable[]（审计可查“谁确认了本文件没有这一条”）
STATUS_NOT_APPLICABLE = "not_applicable"

TERMINAL_STATUSES = (STATUS_APPROVED, STATUS_REJECTED, STATUS_REVISED, STATUS_NOT_APPLICABLE)

MISSING = "__待补__"
# 逐字校验可用的路由类型；扫描件/图片走 OCR，文本噪声大，放行并在 payload 标 verbatim_check=skipped_ocr
VERBATIM_CHECKABLE_KINDS = ("text_pdf", "docx", "doc")

# 硬性必查类别（缺失候选必须有人工处理才能 parsed；与 extractor 的 missing 语义一致）
HARD_MISSING_CATEGORIES = {"资质", "人员", "财务", "信用", "联合体", "响应性", "保证金"}


class ParseServiceError(Exception):
    pass


class CandidateNotFound(ParseServiceError):
    pass


class CandidateAlreadyDecided(ParseServiceError):
    pass


class RuleSetExists(ParseServiceError):
    """规则集已存在且非同一草案（历史版本不可覆盖）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── 落库 ──────────────────────────────────────────────────────────────


def store_candidates(
    session: Session,
    *,
    project_id: str,
    material_id: str,
    version: int,
    kind: str,
    candidates: list[dict],
    actor: str = "parse.worker",
) -> tuple[int, int]:
    """候选落库（幂等）。返回 (created, skipped)。

    candidate_id 全局主键：规则候选取 requirement_id（extractor 已带 material 前缀）；
    主卡候选的 field_key（project_name 等）跨材料必相同 → 前缀 material_id 避免撞库
    （2026-09-03 MAT-TEST-001 实测：rule 候选 ND-H-001-draft 与 MAT-ND-TENDER 撞主键）。
    """
    created = 0
    skipped = 0
    for cand in candidates:
        raw_id = cand.get("requirement_id") or cand.get("field_key") or ""
        if not raw_id:
            continue
        cand_id = raw_id if kind == CANDIDATE_KIND_RULE else f"{material_id}:{raw_id}"
        existing = session.scalar(
            select(ParseCandidate).where(
                ParseCandidate.candidate_id == cand_id,
                ParseCandidate.material_id == material_id,
                ParseCandidate.version == version,
            )
        )
        if existing is not None:
            skipped += 1
            continue
        session.add(ParseCandidate(
            candidate_id=cand_id,
            material_id=material_id,
            version=version,
            project_id=project_id,
            kind=kind,
            payload=cand,
            status=STATUS_PENDING,
            created_at=_now(),
            updated_at=_now(),
        ))
        created += 1
    session.commit()
    return created, skipped


# ── 查询 ──────────────────────────────────────────────────────────────


def list_candidates(
    session: Session,
    *,
    project_id: str | None = None,
    material_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[dict]:
    q = select(ParseCandidate).order_by(ParseCandidate.created_at)
    if project_id:
        q = q.where(ParseCandidate.project_id == project_id)
    if material_id:
        q = q.where(ParseCandidate.material_id == material_id)
    if status:
        q = q.where(ParseCandidate.status == status)
    if kind:
        q = q.where(ParseCandidate.kind == kind)
    rows = session.scalars(q).all()
    return [_row_dict(r) for r in rows]


def _row_dict(row: ParseCandidate) -> dict:
    return {
        "candidate_id": row.candidate_id,
        "material_id": row.material_id,
        "version": row.version,
        "project_id": row.project_id,
        "kind": row.kind,
        "payload": row.payload,
        "status": row.status,
        "reviewer": row.reviewer,
        "revised_payload": row.revised_payload,
        "review_note": row.review_note,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "created_at": row.created_at.isoformat(),
    }


def pending_summary(session: Session, *, project_id: str, material_id: str) -> dict:
    """复核进度（前端展示）：总数/已决/待决 + 需重新决策的遗留行（已通过的 missing 占位）。"""
    total = session.scalar(
        select(func.count()).select_from(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
        )
    )
    pending = session.scalar(
        select(func.count()).select_from(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.status == STATUS_PENDING,
        )
    )
    redo = len(legacy_approved_missing_ids(session, project_id=project_id, material_id=material_id))
    return {"total": total or 0, "pending": pending or 0,
            "decided": (total or 0) - (pending or 0),
            "needs_redecision": redo}


# ── 复核决策（F005 §8 留痕） ─────────────────────────────────────────


def _is_missing(kind: str, payload: dict) -> bool:
    """候选是否为「锚点未命中/未定位到原文」占位（规则看 assertion，字段看 value）。"""
    if payload.get("missing_marker"):
        return True
    if kind == CANDIDATE_KIND_RULE:
        return (payload.get("assertion") or "") in ("", MISSING)
    return (payload.get("value") or "") == MISSING


# 逐字比对前要剥掉的不可见字符（PDF 文本层与阅读器复制常见的零宽/软连字符/控制符）
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 粘贴伪影：单独成行的页码/页眉（如「41」「- 41 -」）——跨页复制时页脚会被带进摘录
_PASTE_PAGE_NO_RE = re.compile(r"\s*[-—–—]?\s*\d{1,4}\s*[-—–]?\s*")


def _norm_nospace(s: str | None) -> str:
    """比对归一：去不可见字符 → 去全部空白 → NFKC（全角数字/字母/括号等编码变体归一）。
    仅用于「是否逐字」的判定与页码定位，不改写落库的原文摘录。"""
    import unicodedata

    cleaned = _INVISIBLE_RE.sub("", s or "")
    cleaned = "".join(cleaned.split())
    return unicodedata.normalize("NFKC", cleaned)


def _strip_paste_artifacts(quote: str) -> str:
    """去掉用户粘贴里单独成行的页码（全行都被剥掉时回退原文，避免把摘录剥空）。"""
    lines = quote.splitlines()
    kept = [ln for ln in lines if not _PASTE_PAGE_NO_RE.fullmatch(ln)]
    return "\n".join(kept) if kept else quote


def _strip_page_number_lines(text: str, *, head: bool, tail: bool) -> str:
    """跨页拼接前剥掉页眉/页脚的纯数字行（PDF 页码会把跨页句子隔断）。"""
    lines = (text or "").split("\n")
    if tail:
        while lines and lines[-1].strip().isdigit():
            lines.pop()
    if head:
        while lines and lines[0].strip().isdigit():
            lines.pop(0)
    return "\n".join(lines)


def _verbatim_in_pages(pages: list[tuple[int, str]], quote: str, page_no: int) -> tuple[bool, str | None, int | None]:
    """纯函数：quote 去空白后是否为第 page_no 页原文子串（允许相邻页与跨页句子）。

    返回 (ok, 拒绝原因, 实际所在页)。实际页与填写页不同（填错一页 / 句子跨页）时返回真实起始页，
    调用方以此落库，不把错误页码写进规则。pages = [(page_no, 整页文本)]。"""
    nq = _norm_nospace(_strip_paste_artifacts(quote))
    if not nq:
        return False, "摘录为空", None
    raw = {p: t for p, t in pages}
    if page_no not in raw:
        return False, f"页码 {page_no} 不存在（原文共 {len(pages)} 页）", None
    norm = {p: _norm_nospace(t) for p, t in pages}
    if nq in norm[page_no]:
        return True, None, page_no
    # 页码给偏（印刷页码 vs 物理页序不一致、或数错）：全文找真实所在页（2026-09-15 实测
    # 「第 41 页」报错即此类）——逐字性由内容保证，页码元数据以校验结果为准落库
    for p in sorted(norm):
        if p != page_no and nq in norm[p]:
            return True, None, p
    # 句子跨页：给定页相邻对优先，其次全文相邻对（剥页脚页码行后拼接）
    order = sorted(raw)
    pairs = [(page_no - 1, page_no), (page_no, page_no + 1)] + list(zip(order, order[1:]))
    for a, b in pairs:
        if a in raw and b in raw:
            joined = _norm_nospace(_strip_page_number_lines(raw[a], head=False, tail=True)
                                   + _strip_page_number_lines(raw[b], head=True, tail=False))
            if nq in joined:
                return True, None, a
    return False, f"摘录不是第 {page_no} 页原文的逐字子串（请从原文复制，不要改写；跨页请分段）", None


# 进程内原文页文本缓存（按 content_hash；130 页文本 PDF 路由约 1-2s，逐条复核不重复解析）
_PAGES_CACHE: dict[str, tuple[str, list[tuple[int, str]]]] = {}
_PAGES_CACHE_MAX = 8


def _material_pages(session: Session, material_id: str, version: int) -> tuple[str | None, list[tuple[int, str]]]:
    """原文逐页文本 (route.kind, [(page_no, text)])；原文不可得 → (None, [])。"""
    from pathlib import Path

    from runtime.core.config import object_store_root
    from runtime.db.models import MaterialVersion

    mv = session.get(MaterialVersion, (material_id, version))
    if mv is None:
        return None, []
    cached = _PAGES_CACHE.get(mv.content_hash)
    if cached is not None:
        return cached
    path = Path(object_store_root()) / mv.object_uri
    if not path.is_file():
        return None, []
    from runtime.parsing.router import route_document

    route = route_document(str(path))
    pages = [(p.page_no, "\n".join(getattr(p, "paragraphs", []) or [])) for p in (route.pages or [])]
    if len(_PAGES_CACHE) >= _PAGES_CACHE_MAX:
        _PAGES_CACHE.pop(next(iter(_PAGES_CACHE)))
    _PAGES_CACHE[mv.content_hash] = (route.kind, pages)
    return route.kind, pages


def verify_verbatim(session: Session, *, material_id: str, version: int,
                    quote: str, page_no: int) -> dict:
    """人工定位/修正摘录的逐字校验（F021 §2.1 v1.5，禁止编造）。

    返回 {"ok", "check", "reason"}：check ∈ verified / skipped_ocr（扫描件放行）；
    原文不可得或不在页 → ok=False（fail-closed，与解析链“原文缺失不得伪造产物”同口径）。
    """
    kind, pages = _material_pages(session, material_id, version)
    if kind is None or not pages:
        return {"ok": False, "check": "unavailable", "page_no": None,
                "reason": "原文不可得，无法做逐字校验（材料版本/对象缺失）"}
    if kind not in VERBATIM_CHECKABLE_KINDS:
        return {"ok": True, "check": "skipped_ocr", "page_no": page_no, "reason": None}
    ok, reason, found = _verbatim_in_pages(pages, quote, page_no)
    return {"ok": ok, "check": "verified" if ok else "failed",
            "page_no": found if ok else None, "reason": reason}


def _merge_rule_revision(payload: dict, revised: dict, *, is_missing: bool, check: str) -> dict:
    """规则候选的 revised_payload = 原 payload + 人工修正（assertion/page_no/clause_ref），
    确认写入时直接作为 Requirement 来源；missing 占位转为已定位（confidence=low）。"""
    from runtime.parsing.extractor import _rule_type_for

    merged = dict(payload)
    merged["assertion"] = str(revised["assertion"]).strip()[:400]
    if revised.get("page_no") is not None:
        merged["page_no"] = int(revised["page_no"])
    clause = (revised.get("clause_ref") or "").strip() if isinstance(revised.get("clause_ref"), str) else None
    if clause:
        merged["clause_ref"] = clause
    elif is_missing:
        merged["clause_ref"] = None  # 不保留锚点基线文件的条款提示（在本文件里是错误位置）
    if revised.get("value") is not None:
        merged["value"] = revised["value"]
    merged["missing_marker"] = False
    merged["confidence"] = "low" if is_missing else (payload.get("confidence") or "low")
    rule = dict(payload.get("rule") or {})
    rule["located_by"] = "human"
    if rule.get("type") == "missing" and rule.get("anchor_key"):
        rule["type"] = _rule_type_for(rule["anchor_key"])
    merged["rule"] = rule
    merged["verbatim_check"] = check
    merged["note"] = None
    return merged


def decide_candidate(
    session: Session,
    *,
    candidate_id: str,
    material_id: str,
    version: int,
    decision: str,
    reviewer: str,
    review_note: str | None = None,
    revised_payload: dict | None = None,
    verbatim_checker=None,
) -> dict:
    """投标专员复核（F021 §2.1 v1.5）：approved / rejected / revised / not_applicable。

    - approved：已定位候选确认无误；对 missing 占位候选**拒绝**（此前确认写入时被静默跳过）。
    - not_applicable：确认本文件无此条款，仅 missing 候选允许，必带人工核查说明。
    - revised：规则候选 = 人工定位/修正原文摘录（逐字校验，missing 必带 page_no）；
      字段/条款候选 = 修正 value。revised_payload 落库为合并后的完整 payload。
    - rejected：必带结构化原因枚举。
    留痕：reviewer / review_note / revised_payload / decided_at；终态不可重复决策
    （例外：旧版遗留「已通过的 missing 候选」允许重新决策，见 04-修改日志 2026-09-14）。
    """
    if decision not in TERMINAL_STATUSES:
        raise ParseServiceError(f"不支持决策: {decision}")
    row = session.scalar(
        select(ParseCandidate).where(
            ParseCandidate.candidate_id == candidate_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.version == version,
        )
    )
    if row is None:
        raise CandidateNotFound(f"候选不存在: {candidate_id}@{material_id}:v{version}")
    payload = row.payload or {}
    is_missing = _is_missing(row.kind, payload)
    # 可重开的两类（其余终态不可重复决策，F005 §8）：
    # - 旧版遗留「已通过的 missing 占位」（v1.5 起通过即无效，须重新三选一）；
    # - rejected（无法确认）——它是「暂未核实」的搁置而非终局判断：复核人后来找到原文/
    #   扫描件辨清后应能补录（2026-09-15：逐字校验误报曾迫使用户点「无法确认」锁死候选）。
    #   原决策留在 audit_events（parse.candidate.review），重开不抹审计。
    reopenable = (row.status == STATUS_APPROVED and is_missing) or row.status == STATUS_REJECTED
    if row.status in TERMINAL_STATUSES and not reopenable:
        raise CandidateAlreadyDecided(f"候选已决策（{row.status}），终态不可重复决策")

    note = (review_note or "").strip()
    if decision == STATUS_APPROVED:
        if is_missing:
            raise ParseServiceError(
                "该候选未在原文定位到（锚点未命中），不能「通过」：请选择「本文件无此条款」、"
                "「人工定位补录」或「无法确认」（F021 §2.1 v1.5）"
            )
        if revised_payload:
            raise ParseServiceError("approved 不得携带 revised_payload（确认无误无需修正值）")
    elif decision == STATUS_NOT_APPLICABLE:
        if not is_missing:
            raise ParseServiceError(
                "「本文件无此条款」仅适用于未定位到原文的候选；已定位候选请用通过 / 编辑后确认 / 无法确认"
            )
        if not note:
            raise ParseServiceError(
                "「本文件无此条款」必须填写人工核查说明（如：全文检索“审计报告”，资格审查无此要求）"
            )
    elif decision == STATUS_REJECTED:
        # F021 §2.1 v1.3：标记无法确认必须携带结构化原因枚举（扫描不清/条款冲突/未识别/需业务解释），
        # 这不是上传企业材料；原因写入 review_note，允许“原因：补充说明”格式。
        if not note or not any(
            note == r or note.startswith(f"{r}：") or note.startswith(f"{r}:")
            for r in REJECT_REASONS
        ):
            raise ParseServiceError(
                f"rejected 决策必须携带结构化原因（{' / '.join(REJECT_REASONS)}），写入 review_note"
            )
    elif decision == STATUS_REVISED:
        if not revised_payload:
            raise ParseServiceError("revised 决策必须携带 revised_payload（人工修正值/依据）")
        if row.kind == CANDIDATE_KIND_RULE:
            assertion = str(revised_payload.get("assertion") or "").strip()
            if not assertion or assertion == MISSING:
                raise ParseServiceError("编辑后确认必须提供原文逐字摘录 assertion（不是修正说明）")
            page_no = revised_payload.get("page_no")
            if page_no is None:
                page_no = payload.get("page_no")
            if page_no is None:
                raise ParseServiceError("人工定位补录必须提供页码 page_no（摘录所在页）")
            try:
                page_no = int(page_no)
            except (TypeError, ValueError):
                raise ParseServiceError("page_no 必须是整数页码")
            checker = verbatim_checker or verify_verbatim
            check = checker(session, material_id=material_id, version=version,
                            quote=assertion, page_no=page_no)
            if not check.get("ok"):
                raise ParseServiceError(check.get("reason") or "摘录逐字校验未通过")
            # 校验器回报的实际所在页优先（填错一页/跨页句子时不把错误页码写进规则）
            if check.get("page_no") is not None:
                page_no = int(check["page_no"])
            revised_payload = _merge_rule_revision(
                payload, dict(revised_payload, page_no=page_no),
                is_missing=is_missing, check=check.get("check") or "verified",
            )
        else:
            value = str(revised_payload.get("value") or "").strip()
            if not value or value == MISSING:
                raise ParseServiceError("字段修正必须提供修正后的 value")
            merged = dict(payload)
            merged["value"] = value[:200]
            merged["missing_marker"] = False
            merged["confidence"] = "low" if is_missing else (payload.get("confidence") or "low")
            revised_payload = merged

    row.status = decision
    row.reviewer = reviewer
    row.review_note = review_note
    row.revised_payload = revised_payload
    row.decided_at = _now()
    row.updated_at = _now()
    session.commit()
    return _row_dict(row)


def legacy_approved_missing_ids(session: Session, *, project_id: str, material_id: str,
                                version: int | None = None) -> list[str]:
    """v1.4 及以前「通过了 missing 占位候选」的遗留行（确认写入时会被静默跳过 = 规则无声消失）。
    v1.5 起这些行必须重新决策（本文件无此条款 / 人工定位补录 / 无法确认），确认前阻断。"""
    q = select(ParseCandidate).where(
        ParseCandidate.project_id == project_id,
        ParseCandidate.material_id == material_id,
        ParseCandidate.status == STATUS_APPROVED,
    )
    if version is not None:
        q = q.where(ParseCandidate.version == version)
    return [r.candidate_id for r in session.scalars(q).all() if _is_missing(r.kind, r.payload or {})]


def material_parse_status(session: Session, *, project_id: str, material_id: str) -> str:
    """按候选复核进度推导 parse_status：
    - 无候选记录 → pending
    - 全部决策且无 pending → parsed（已确认通过）
    - 存在 missing_marker 候选且仍 pending 或 rejected → manual_review
    """
    rows = session.scalars(
        select(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
        )
    ).all()
    if not rows:
        return "pending"
    if any(r.status == STATUS_PENDING for r in rows):
        return "manual_review"  # 有待决候选：人工复核中
    if any(r.status == STATUS_APPROVED and _is_missing(r.kind, r.payload or {}) for r in rows):
        return "manual_review"  # 旧版遗留：已通过的 missing 占位必须重新决策
    # 全部已决：rejected 存在 → manual_review（需人工补录/修正）；否则 parsed（not_applicable 属正常终态）
    rejected = [r for r in rows if r.status == STATUS_REJECTED]
    if rejected:
        return "manual_review"
    return "parsed"


# ── 确认后写入（F021 §2.7：复核通过 → RuleSet/Requirement/FieldTrace） ──


def material_ruleset_id(material_id: str, version: int) -> str:
    """规则集草案 ID：RS-<MATERIAL>-v<version>（材料版本对齐，历史版本不可覆盖）。"""
    return f"RS-{material_id}-v{version}"


def confirm_rules_from_approved(
    session: Session,
    *,
    project_id: str,
    material_id: str,
    version: int,
    as_of: str,
    created_by: str,
) -> dict:
    """把全部 approved 规则候选写入 RuleSet/Requirement（快照式，历史不可覆盖）。

    仅 hard/scored 候选写入；rejected/revised 的处理：
    - rejected → 不写入（缺项由 review_note 记录，parse_status 保持 manual_review）
    - revised → 写入 revised_payload 内容（人工修正值，留痕可查）
    返回 {rule_set_id, created_requirements, rejected, pending}。
    """
    rows = session.scalars(
        select(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.version == version,
            ParseCandidate.kind == CANDIDATE_KIND_RULE,
        )
    ).all()
    if not rows:
        return {"rule_set_id": None, "created_requirements": 0, "rejected": 0, "pending": 0}

    rule_set_id = material_ruleset_id(material_id, version)
    existing = session.get(RuleSet, rule_set_id)
    if existing is not None:
        raise RuleSetExists(
            f"规则集 {rule_set_id} 已存在（历史版本不可覆盖；澄清应产生新 Material 版本）"
        )

    approved = [r for r in rows if r.status in (STATUS_APPROVED, STATUS_REVISED)]
    rejected = [r for r in rows if r.status == STATUS_REJECTED]
    pending = [r for r in rows if r.status == STATUS_PENDING]
    not_applicable = [r for r in rows if r.status == STATUS_NOT_APPLICABLE]
    if pending:
        raise ParseServiceError(f"仍有 {len(pending)} 个待决候选，不能生成规则集")
    # v1.5：已「通过」的 missing 占位不得静默跳过（= 规则无声消失）→ 阻断并点名，要求重新决策
    # （revised 行的原始 payload 仍是 missing，但 revised_payload 已人工定位，不算遗留）
    legacy = [r.candidate_id for r in approved
              if r.status == STATUS_APPROVED and _is_missing(r.kind, r.payload or {})]
    if legacy:
        raise ParseServiceError(
            f"{len(legacy)} 个候选未定位到原文却被「通过」（旧版遗留），需重新决策为"
            f"「本文件无此条款 / 人工定位补录 / 无法确认」后再确认：{', '.join(legacy[:6])}"
            + ("…" if len(legacy) > 6 else "")
        )

    snapshot_requirements = []
    session.add(RuleSet(
        rule_set_id=rule_set_id,
        project_id=project_id,
        version=f"v{version}",
        effective_from=None,
        created_by=created_by,
        snapshot={
            "source": f"material={material_id}:v{version}",
            "as_of": as_of,
            "confirmed_by": created_by,
            "requirement_count": len(approved),
            # 审计：谁确认了“本文件没有这一条”（F021 §2.1 v1.5）
            "not_applicable": [
                {
                    "candidate_id": r.candidate_id,
                    "anchor_key": ((r.payload or {}).get("rule") or {}).get("anchor_key"),
                    "category": (r.payload or {}).get("category"),
                    "reviewer": r.reviewer,
                    "note": r.review_note,
                    "decided_at": r.decided_at.isoformat() if r.decided_at else None,
                }
                for r in not_applicable
            ],
        },
        diff=None,
    ))
    created = 0
    for row in approved:
        payload = row.revised_payload or row.payload
        req_id = payload.get("requirement_id", row.candidate_id)
        req_type = payload.get("req_type", "hard_requirement")
        # 防御：合并后的 revised_payload 已保证非缺失；此处仅拦截异常数据
        if payload.get("missing_marker") or not payload.get("assertion") or payload.get("assertion") == MISSING:
            continue
        # 页码随规则固化（Requirement 无独立页码列；confirmed 视图与解释链回跳原文用）
        rule_dict = dict(payload.get("rule") or {})
        if payload.get("page_no") is not None and "page_no" not in rule_dict:
            rule_dict["page_no"] = payload.get("page_no")
        session.add(Requirement(
            requirement_id=f"{req_id}",
            rule_set_id=rule_set_id,
            req_type=req_type,
            category=payload.get("category"),
            lot_id=None,
            clause_ref=payload.get("clause_ref") or "",
            assertion=payload.get("assertion") or "",
            rule=rule_dict,
            evidence_required=payload.get("evidence_required") or [],
            as_of=as_of,
            missing_action="blocked_missing_data",
            failure_effect="not_qualified" if req_type == "hard_requirement" else None,
            priority=None,
            logic_group=req_type,
            operator=None,
            consortium_role="none",
            max_score=payload.get("max_score"),
            weight=payload.get("weight"),
            score_nature=payload.get("score_nature"),
            score_formula=payload.get("score_formula"),
            required_by_stage=payload.get("required_by_stage"),
        ))
        created += 1
        snapshot_requirements.append({
            "requirement_id": req_id, "req_type": req_type,
            "clause_ref": payload.get("clause_ref"),
        })
    session.commit()
    return {"rule_set_id": rule_set_id, "created_requirements": created,
            "rejected": len(rejected), "pending": len(pending),
            "not_applicable": len(not_applicable)}


def suggested_as_of(session: Session, *, project_id: str, material_id: str, version: int) -> dict | None:
    """确认写入的 as_of 建议（F021 §2.3 取值链第 2/3 级）：
    ① 该材料已 approved/revised 的主卡字段 deadline_bid（投标文件递交截止）日期部分；
    ② 项目既有规则集的 as_of 锚点（澄清版本重确认沿用）。都没有 → None（不得默认当前时间）。"""
    import re

    row = session.scalar(
        select(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.version == version,
            ParseCandidate.kind == CANDIDATE_KIND_FIELD,
            ParseCandidate.candidate_id == f"{material_id}:deadline_bid",
            ParseCandidate.status.in_((STATUS_APPROVED, STATUS_REVISED)),
        )
    )
    if row is not None:
        payload = row.revised_payload or row.payload or {}
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(payload.get("value") or ""))
        if m:
            return {"as_of": m.group(0), "source": "main_card:deadline_bid",
                    "page_no": payload.get("page_no"), "value": payload.get("value")}
    anchor = session.scalar(
        select(Requirement.as_of)
        .join(RuleSet, RuleSet.rule_set_id == Requirement.rule_set_id)
        .where(RuleSet.project_id == project_id)
        .order_by(RuleSet.created_at.desc())
        .limit(1)
    )
    if anchor:
        return {"as_of": anchor, "source": "rule_set_anchor", "page_no": None, "value": anchor}
    return None


def confirm_main_card_fields(
    session: Session,
    *,
    material_id: str,
    version: int,
    project_id: str,
    actor: str,
    content_hash: str | None = None,
) -> int:
    """把 approved 主卡字段候选写入 FieldTrace（F005 §4.3 字段级溯源）。返回写入数。

    只写非缺失字段；__待补__/rejected 不写（缺失在解析复核 UI 呈现）。
    """
    rows = session.scalars(
        select(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.version == version,
            ParseCandidate.kind == CANDIDATE_KIND_FIELD,
            ParseCandidate.status.in_((STATUS_APPROVED, STATUS_REVISED)),
        )
    ).all()
    written = 0
    for row in rows:
        payload = row.revised_payload or row.payload
        value = payload.get("value") or ""
        if not value or value == "__待补__":
            continue
        session.add(FieldTrace(
            trace_id=f"FT-{uuid.uuid4().hex[:16]}",
            object_id=f"{material_id}:v{version}",
            field_key=payload.get("field_key") or row.candidate_id,
            clause=payload.get("clause") or payload.get("clause_ref") or "",
            source_link=payload.get("source_link"),
            assertion=payload.get("assertion") or value,
            confidence=payload.get("confidence"),
            source_hash=content_hash,
        ))
        written += 1
    if written:
        session.commit()
    return written


def mark_material_parsed(session: Session, *, material_id: str, version: int,
                         actor: str = "parse.review") -> None:
    """复核全部通过 → material.parse_status = parsed（触发后续索引/匹配编排）。"""
    session.execute(
        update(Material)
        .where(Material.material_id == material_id, Material.version == version)
        .values(parse_status="parsed", updated_at=_now())
    )
    session.commit()


def mark_material_manual_review(session: Session, *, material_id: str, version: int,
                                note: str = "存在待决/驳回候选") -> None:
    session.execute(
        update(Material)
        .where(Material.material_id == material_id, Material.version == version)
        .values(parse_status="manual_review", updated_at=_now())
    )
    session.commit()


# ════════════════════════════════════════════════════════════════════
# F021 §2.1 v1.3 / 09-优化方案 §3.3：解析结果聚合（六组业务语言分组）
# 视图切换：写规则集前=parse_candidates 视图；确认后=Requirement(+FieldTrace) 视图。
# 分组判定确定性映射（不可判定 → 归“其他”并如实标注，禁止推断）。
# ════════════════════════════════════════════════════════════════════

# 六组固定分组（顺序即前端展示顺序；任何未命中项归“其他”）
REVIEW_GROUP_ORDER: list[str] = [
    "项目基本信息",
    "资格与资质",
    "项目经理与人员",
    "业绩、财务与信用",
    "场地、设备和其他资源",
    "投标动作与风险条款",
    "其他",
]
GROUP_BASIC = REVIEW_GROUP_ORDER[0]          # 项目基本信息
GROUP_QUALIFICATION = REVIEW_GROUP_ORDER[1]  # 资格与资质
GROUP_PERSONNEL = REVIEW_GROUP_ORDER[2]      # 项目经理与人员
GROUP_EVIDENCE = REVIEW_GROUP_ORDER[3]       # 业绩、财务与信用
GROUP_RESOURCES = REVIEW_GROUP_ORDER[4]      # 场地、设备和其他资源
GROUP_ACTION = REVIEW_GROUP_ORDER[5]         # 投标动作与风险条款
GROUP_OTHER = REVIEW_GROUP_ORDER[6]

# 规则锚点 → 业务分组（extractor.ANCHORS key 全集；新增锚点必须在此登记，否则归“其他”）
ANCHOR_GROUP: dict[str, str] = {
    # 资格与资质：企业必须具备的资质/许可
    "qualification_grade": GROUP_QUALIFICATION,
    "safety_license": GROUP_QUALIFICATION,
    # 项目经理与人员：人力资源约束
    "pm_registered_builder": GROUP_PERSONNEL,
    "pm_b_cert": GROUP_PERSONNEL,
    "pm_no_active": GROUP_PERSONNEL,
    "pm_social_security": GROUP_PERSONNEL,
    "safety_officer": GROUP_PERSONNEL,
    "tech_team": GROUP_PERSONNEL,
    # 业绩、财务与信用：证据类要求（含评分/硬性属性）
    "financial_audit": GROUP_EVIDENCE,
    "credit_no_loser": GROUP_EVIDENCE,
    "scoring_tech": GROUP_EVIDENCE,
    "scoring_similar_performance": GROUP_EVIDENCE,
    "scoring_business_credit": GROUP_EVIDENCE,
    # 场地、设备和其他资源：资源约束（是否允许联合体等）
    "consortium": GROUP_RESOURCES,
    # 投标动作与风险条款：保证金/有效期/限价/报名/递交/开标
    "bid_bond": GROUP_ACTION,
    "bid_validity": GROUP_ACTION,
    "ceiling_price": GROUP_ACTION,
    "action_file_acquisition": GROUP_ACTION,
    "action_deadline_bid": GROUP_ACTION,
    "action_bid_bond_due": GROUP_ACTION,
    "action_open": GROUP_ACTION,
    # 2026-09-14 锚点扩充批次
    "business_license": GROUP_QUALIFICATION,
    "prequalification_method": GROUP_QUALIFICATION,
    "tech_lead": GROUP_PERSONNEL,
    "pm_similar_performance": GROUP_PERSONNEL,
    "scoring_pm": GROUP_PERSONNEL,
    "similar_performance_hard": GROUP_EVIDENCE,
    "no_major_violation": GROUP_EVIDENCE,
    "credit_blacklist": GROUP_EVIDENCE,
    "tax_social_proof": GROUP_EVIDENCE,
    "scoring_enterprise_honor": GROUP_EVIDENCE,
    "scoring_equipment": GROUP_RESOURCES,
    "scoring_construction_plan": GROUP_RESOURCES,
    "evaluation_method": GROUP_ACTION,
    "scoring_price": GROUP_ACTION,
    "action_q_and_a": GROUP_ACTION,
    "action_site_visit": GROUP_ACTION,
    # 2026-09-14 v1.5：EPC 设计/施工负责人
    "design_lead": GROUP_PERSONNEL,
    "construction_lead": GROUP_PERSONNEL,
}

# 类别 → 业务分组（P4-2 发现候选：anchor_key 为空，按类别归组；未命中归“其他”）
CATEGORY_GROUP: dict[str, str] = {
    "资质": GROUP_QUALIFICATION,
    "人员": GROUP_PERSONNEL,
    "业绩": GROUP_EVIDENCE,
    "财务": GROUP_EVIDENCE,
    "信用": GROUP_EVIDENCE,
    "评分": GROUP_ACTION,
    "动作": GROUP_ACTION,
}

# 规则锚点 → 可读标题（展示层确定性生成，审计可查；未登记锚点用“类别 + 待复核”）
ANCHOR_TITLE: dict[str, str] = {
    "qualification_grade": "施工总承包资质等级要求",
    "safety_license": "安全生产许可证要求",
    "pm_registered_builder": "项目经理注册建造师资格",
    "pm_b_cert": "项目经理安全生产考核合格证（B 证）",
    "pm_no_active": "项目经理在施限制",
    "pm_social_security": "项目经理社保要求",
    "safety_officer": "专职安全生产管理人员配备",
    "tech_team": "专业技术团队配备",
    "financial_audit": "财务审计报告要求",
    "credit_no_loser": "信用记录要求（失信限制）",
    "consortium": "联合体投标限制",
    "bid_validity": "投标有效期要求",
    "bid_bond": "投标保证金要求",
    "ceiling_price": "最高投标限价要求",
    "scoring_tech": "技术标评审方式",
    "scoring_similar_performance": "类似业绩评分要求",
    "scoring_business_credit": "商务标/信用评分要求",
    "action_file_acquisition": "招标文件获取（报名）",
    "action_deadline_bid": "投标文件递交截止",
    "action_bid_bond_due": "投标保证金递交截止",
    "action_open": "开标时间",
    # 2026-09-14 锚点扩充批次
    "business_license": "营业执照 / 独立法人资格",
    "similar_performance_hard": "类似业绩硬性要求（资格审查）",
    "no_major_violation": "无重大违法记录要求",
    "credit_blacklist": "严重违法失信名单限制（税收 / 政采 / 信用中国）",
    "tax_social_proof": "纳税与社会保障资金缴纳证明",
    "prequalification_method": "资格审查方式（后审 / 预审）",
    "tech_lead": "技术负责人职称要求",
    "pm_similar_performance": "项目经理类似业绩要求",
    "evaluation_method": "评标办法",
    "scoring_price": "价格分 / 评标基准价",
    "scoring_pm": "项目经理评分项",
    "scoring_construction_plan": "施工组织设计评分项",
    "scoring_enterprise_honor": "企业荣誉 / 信用加分项",
    "scoring_equipment": "拟投入设备评分项",
    "action_q_and_a": "答疑 / 质疑截止",
    "action_site_visit": "现场踏勘安排",
    # 2026-09-14 v1.5：EPC 设计/施工负责人
    "design_lead": "设计负责人执业资格（EPC）",
    "construction_lead": "施工负责人执业资格（EPC）",
}

# 合同/商务/技术条款字段（kind=term_field，extractor.TERM_ANCHORS）→ 可读标题
CANDIDATE_KIND_TERM = "term_field"
TERM_TITLE: dict[str, str] = {
    "duration": "工期",
    "quality_standard": "质量标准",
    "contract_type": "合同类型 / 计价方式",
    "payment_terms": "付款方式",
    "advance_payment": "预付款",
    "performance_bond": "履约保证金 / 担保",
    "retention_money": "质量保证金",
    "warranty": "保修期 / 缺陷责任期",
    "provisional_sum": "暂列金额",
    "downward_rate": "下浮率",
    "safety_fee": "安全文明施工费",
    "tech_standard": "技术标准和要求",
    "subcontract": "分包约定",
}

# 主卡字段 → 可读标题（项目基本信息组）
MAIN_CARD_TITLE: dict[str, str] = {
    "project_name": "项目名称",
    "tender_no": "项目/招标编号",
    "tenderee": "招标人",
    "agency": "招标代理机构",
    "region": "建设地点",
    "ceiling_price": "最高投标限价",
    "budget_amount": "项目总投资",
    "bid_bond_amount": "投标保证金金额",
    "deadline_signup": "报名截止时间",
    "deadline_bid": "投标文件递交截止时间",
    "open_date": "开标时间",
    "project_type": "项目类型",
}

# rejected 结构化原因枚举（F021 §2.1 v1.3：标记无法确认必须携带原因，非上传材料）
REJECT_REASONS: tuple[str, ...] = ("扫描不清", "条款冲突", "未识别", "需业务解释")


def _anchor_key_of(payload: dict) -> str | None:
    rule = payload.get("rule") or {}
    return rule.get("anchor_key") if isinstance(rule, dict) else None


def _review_title(payload: dict, *, kind: str) -> str:
    """可读标题：锚点登记表 / 主卡字段表 / 条款字段表 → 兜底“类别 + 待复核”（不推断）。"""
    if kind == CANDIDATE_KIND_FIELD:
        fk = payload.get("field_key") or ""
        return MAIN_CARD_TITLE.get(fk) or f"{fk}（待复核）"
    if kind == CANDIDATE_KIND_TERM:
        fk = payload.get("field_key") or ""
        return TERM_TITLE.get(fk) or f"{fk}（条款，待复核）"
    anchor = _anchor_key_of(payload)
    if anchor and anchor in ANCHOR_TITLE:
        return ANCHOR_TITLE[anchor]
    category = payload.get("category") or "未分类"
    rule = payload.get("rule") if isinstance(payload.get("rule"), dict) else {}
    if (rule or {}).get("discovered"):
        return f"{category}要求（大模型发现，待复核）"
    return f"{category}要求（待复核）"


def _review_group(payload: dict, *, kind: str) -> str:
    """确定性分组：main_card_field → 项目基本信息；term_field → 投标动作与风险条款；
    action → 动作组；否则按锚点表；兜底“其他”。"""
    if kind == CANDIDATE_KIND_FIELD:
        return GROUP_BASIC
    if kind == CANDIDATE_KIND_TERM:
        return GROUP_ACTION
    if payload.get("req_type") == "action_requirement":
        return GROUP_ACTION
    anchor = _anchor_key_of(payload)
    if anchor and anchor in ANCHOR_GROUP:
        return ANCHOR_GROUP[anchor]
    return CATEGORY_GROUP.get(payload.get("category") or "", GROUP_OTHER)


def _issue_text(payload: dict, row: ParseCandidate | None = None) -> str | None:
    """缺失/歧义原因（复核人可读，F021 §2.1 v1.5）：
    - missing 占位 → 说明“系统未在原文找到该条款的常见措辞”，并指引三种处置；
    - 旧版遗留“已通过的 missing” → 必须重新决策；
    - rejected → 复核原因；无则 None。"""
    if row is not None and row.status == STATUS_APPROVED and payload.get("missing_marker"):
        return "已「通过」但未定位到原文（旧版遗留）：请重新决策——本文件无此条款 / 人工定位补录 / 无法确认"
    if row is not None and row.status == STATUS_NOT_APPLICABLE:
        return None
    if payload.get("missing_marker"):
        return ("系统没有在原文中找到这条要求的常见措辞。请用「检索关键词」全文搜索：找到 → 「人工定位补录」"
                "填页码与原文摘录；确认本文件没有这条 → 「本文件无此条款」；无法判断 → 「无法确认」。")
    rule = payload.get("rule") if isinstance(payload.get("rule"), dict) else {}
    if (rule or {}).get("located_by") == "llm" and (row is None or row.status == STATUS_PENDING):
        return payload.get("note") or "模型定位的原文摘录（低置信）：请核对原文后再通过"
    if row is not None and row.status == STATUS_REJECTED:
        return row.review_note or "标记无法确认（原因见复核记录）"
    return None


def _locate_hints(payload: dict) -> list[str]:
    from runtime.parsing.extractor import LOCATE_HINTS

    anchor = _anchor_key_of(payload)
    if anchor and anchor in LOCATE_HINTS:
        return list(LOCATE_HINTS[anchor])
    category = payload.get("category")
    return [category] if category else []


def _source_link(material_id: str, version: int) -> str:
    """原文文件接口（F021 §2.1 v1.5）；前端以 Bearer 拉取 blob 后附 #page=<page_no> 打开。"""
    return f"/api/v1/materials/{material_id}/file?version={version}"


def _clean_clause(clause: str | None) -> str | None:
    """历史候选的 clause_ref 在条款号回退时被写成「提示（提示）」（extractor 旧行为），展示时折叠。"""
    if not clause:
        return None
    import re

    m = re.fullmatch(r"(.+?)（\1）", clause)
    return m.group(1) if m else clause


def _requirement_type(req_type: str | None) -> str | None:
    if req_type == "hard_requirement":
        return "hard"
    if req_type == "scored_requirement":
        return "scored"
    if req_type == "action_requirement":
        return "action"
    return None


def grouped_requirements(
    session: Session, *, project_id: str, material_id: str | None = None, version: int | None = None,
) -> dict[str, Any]:
    """F021 §2.1 解析结果聚合：返回 {source, project_id, material_id, version, groups}。

    source 判定：
    - 规则集已写入（RuleSet 存在且材料 parsed/confirmed）→ "confirmed"：读 Requirement + FieldTrace；
    - 否则 → "candidates"：读 parse_candidates 全量（rule_candidate + main_card_field）。
    前端六组顺序 REVIEW_GROUP_ORDER 展示；audit 折叠放 material_id/content_hash/内部编码。
    """
    from runtime.db.models import Project

    # 定位该项目招标材料（显式传参优先，否则取项目最新招标文件）
    if material_id is None:
        project = session.get(Project, project_id)
        material_id = project.tender_document_ref if project else None
    if material_id is None:
        material = session.scalar(
            select(Material)
            .where(Material.project_id == project_id,
                   Material.material_type == "tender_document")
            .order_by(Material.version.desc())
            .limit(1)
        )
        if material is not None:
            material_id, version = material.material_id, material.version
    if material_id is None:
        return {"source": None, "project_id": project_id, "material_id": None,
                "version": None, "groups": [], "progress": {"total": 0, "pending": 0, "decided": 0}}
    material = session.scalar(
        select(Material)
        .where(Material.material_id == material_id)
        .order_by(Material.version.desc())
        .limit(1)
    )
    if material is None:
        return {"source": None, "project_id": project_id, "material_id": material_id,
                "version": None, "groups": [], "progress": {"total": 0, "pending": 0, "decided": 0}}
    version = version or material.version
    rule_set_id = material_ruleset_id(material_id, version)
    confirmed = session.get(RuleSet, rule_set_id) is not None

    groups: dict[str, list[dict]] = {g: [] for g in REVIEW_GROUP_ORDER}
    content_hash = material.content_hash
    audit = {"material_id": material_id, "content_hash": (content_hash or "")[:16]}
    source_link = _source_link(material_id, version)
    as_of_suggestion: dict | None = None

    if confirmed:
        # ── confirmed 视图：Requirement（规则） + FieldTrace（主卡字段溯源） ──
        reqs = session.scalars(
            select(Requirement).where(Requirement.rule_set_id == rule_set_id)
            .order_by(Requirement.requirement_id)
        ).all()
        for req in reqs:
            payload = {
                "requirement_id": req.requirement_id,
                "req_type": req.req_type,
                "category": req.category,
                "clause_ref": req.clause_ref,
                "assertion": req.assertion,
                "rule": req.rule,
            }
            item = {
                "id": req.requirement_id,
                "title": _review_title(payload, kind=CANDIDATE_KIND_RULE),
                "requirement_type": _requirement_type(req.req_type),
                "value": None,  # Requirement 无结构化 value 列；要求值见 assertion（不推断）
                "assertion": req.assertion,
                "clause_ref": _clean_clause(req.clause_ref),
                "page_no": (req.rule or {}).get("page_no") if isinstance(req.rule, dict) else None,
                "confidence": None,
                "review_status": "confirmed",
                "issue": None,
                "locate_hints": [],
                "source_link": source_link,
                "audit": dict(audit, requirement_id=req.requirement_id),
            }
            groups[_review_group(payload, kind=CANDIDATE_KIND_RULE)].append(item)
        traces = session.scalars(
            select(FieldTrace)
            .where(FieldTrace.object_id == f"{material_id}:v{version}")
            .order_by(FieldTrace.field_key)
        ).all()
        for tr in traces:
            groups[GROUP_BASIC].append({
                "id": f"{tr.object_id}:{tr.field_key}",
                "title": MAIN_CARD_TITLE.get(tr.field_key) or TERM_TITLE.get(tr.field_key) or tr.field_key,
                "requirement_type": None,
                "value": tr.assertion,  # 主卡字段的“要求值”即已确认字段内容（FieldTrace 溯源）
                "assertion": tr.assertion,
                "clause_ref": tr.clause,
                "page_no": None,
                "confidence": tr.confidence,
                "review_status": "confirmed",
                "issue": None,
                "locate_hints": [],
                "source_link": tr.source_link or source_link,
                "audit": dict(audit, field_key=tr.field_key),
            })
        source = "confirmed"
    else:
        # ── candidates 视图：待复核/已决候选全量（rule + field + term） ──
        rows = session.scalars(
            select(ParseCandidate)
            .where(ParseCandidate.project_id == project_id,
                   ParseCandidate.material_id == material_id,
                   ParseCandidate.version == version)
            .order_by(ParseCandidate.created_at, ParseCandidate.kind)
        ).all()
        for row in rows:
            payload = row.payload or {}
            # 已决 revised 行展示合并后的修正 payload（人工定位的页码/摘录），未决行展示解析原值
            shown = (row.revised_payload or payload) if row.status == STATUS_REVISED else payload
            missing = _is_missing(row.kind, shown)
            is_rule = row.kind == CANDIDATE_KIND_RULE
            item = {
                "id": row.candidate_id,
                "title": _review_title(payload, kind=row.kind),
                "requirement_type": _requirement_type(payload.get("req_type")),
                "value": shown.get("value"),
                "assertion": "" if missing else (shown.get("assertion") or ""),
                # 未定位到原文的候选不回传锚点基线文件的条款提示（在本文件里是错误位置）
                "clause_ref": None if (missing and is_rule) else _clean_clause(shown.get("clause_ref") or shown.get("clause")),
                "page_no": None if missing else shown.get("page_no"),
                "confidence": shown.get("confidence"),
                "review_status": row.status,
                "missing": missing,
                "needs_redecision": row.status == STATUS_APPROVED and missing,
                "issue": _issue_text(shown, row),
                "locate_hints": _locate_hints(payload) if (missing and is_rule) else [],
                "review_note": row.review_note,
                "reviewer": row.reviewer,
                "source_link": source_link,
                "audit": dict(audit, kind=row.kind,
                              requirement_id=payload.get("requirement_id"),
                              field_key=payload.get("field_key")),
            }
            groups[_review_group(payload, kind=row.kind)].append(item)
        source = "candidates"
        as_of_suggestion = suggested_as_of(
            session, project_id=project_id, material_id=material_id, version=version
        )

    return {
        "source": source,
        "project_id": project_id,
        "material_id": material_id,
        "version": version,
        "groups": [
            {"group": g, "group_label": g, "items": groups[g]}
            for g in REVIEW_GROUP_ORDER
        ],
        "progress": pending_summary(session, project_id=project_id, material_id=material_id),
        "as_of_suggestion": as_of_suggestion,
    }
