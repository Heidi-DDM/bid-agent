# R018/F018 §4.2：单 worker 任务循环
# 轮询 analysis_jobs，领取任务 -> 心跳 -> 执行 -> 完成/失败重试。
# 进程异常时任务保持 running 超时后可恢复为 retryable（F018 §4.4），
# 不直接把项目推进到下一业务状态。
#
# 用法：
#   python -m runtime.worker            # 默认配置
#   WORKER_POLL_INTERVAL_SECONDS=2 python -m runtime.worker
from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.config
import os
import signal
import sys
import threading
import time

from runtime.core import jobs as job_logic
from runtime.core.compliance import ComplianceError
from runtime.core.config import job_running_timeout_seconds, logging_config, worker_poll_interval_seconds
from runtime.db.models import AnalysisJob
from runtime.db.worker_service import claim_job, finish_job, heartbeat_job

logger = logging.getLogger("runtime.worker")

RUNNER_ID = os.environ.get("RUNNER_ID", f"worker-{os.getpid()}")


def process_one(session, runner_id: str, stale_seconds: int) -> bool:
    """领取并执行一个任务。返回是否处理了任务。"""
    job = claim_job(session, runner_id, stale_seconds=stale_seconds)
    if job is None:
        return False
    job_id = job.job_id
    kind = job.kind
    logger.info("领取任务 job_id=%s kind=%s attempts=%s", job_id, kind, job.attempts)
    try:
        # 心跳保持（单次任务执行期间周期性刷新；演示执行器即刻返回）
        heartbeat_job(session, job_id)
        # F021+ 在此接入具体执行器（解析/匹配/准入），当前为可验证的占位执行
        _execute(session, kind, job.input_ref, job.project_id, job_id)
        finish_job(session, job_id, outcome="completed")
        logger.info("完成任务 job_id=%s kind=%s", job_id, kind)
        # F020 §2.1：解析成功只触发一次首次匹配（parse.completed 幂等编排）
        if kind == "parse.tender_document":
            _on_parse_completed(session, job.project_id)
    except ComplianceError as exc:
        # 限频/合规拒绝：终态失败不自动重试（窗口内重试必再被拒，徒耗 attempt），
        # 错误信息携带可重试时刻；用户按倒计时后重新提交导入任务（attempts 归零）。
        logger.warning("任务合规拒绝 job_id=%s kind=%s error=%s", job_id, kind, exc)
        session.rollback()
        from runtime.db.worker_service import finish_job as _finish_job

        suffix = ""
        if exc.retry_after_seconds is not None:
            suffix = f"（可重试时刻：约 {exc.retry_after_seconds} 秒后）"
        try:
            _finish_job(session, job_id, outcome="failed",
                        error_code="rate_limited" if exc.retry_after_seconds is not None else type(exc).__name__,
                        error_message=f"{exc}{suffix}")
        except Exception:
            session.rollback()
            from runtime.db.worker_service import fail_then_retryable as _retryable

            _retryable(session, job_id, error_code="rate_limited", error_message=str(exc)[:500])
    except Exception as exc:
        # session 可能已处于 PendingRollback（如 store_candidates flush 撞主键），
        # 必须先 rollback 才能继续读写；否则 fail_then_retryable 二次抛错、
        # job 停留 running 直至 heartbeat_timeout（2026-09-03 MAT-TEST-001 实测 3 次）。
        logger.exception("任务执行异常 job_id=%s kind=%s error=%s", job_id, kind, type(exc).__name__)
        session.rollback()
        from runtime.db.worker_service import fail_then_retryable

        fail_then_retryable(session, job_id, error_code=type(exc).__name__, error_message=str(exc)[:500])
    return True


def _on_parse_completed(session, project_id: str | None) -> None:
    """解析完成编排（兼容入口，R021-5 后统一走 api_service.schedule_post_parse）。

    实际调度发生在人工复核确认（confirm API → parse_status=parsed）之后：
    schedule_post_parse 按幂等键创建 knowledge_index（index:…）与 match.run
    （kind+input_ref+project_id）；结果页 GET 查询不创建 run（F020 §2.1/§7）。
    """
    if not project_id:
        return
    from runtime.db import api_service

    scheduled = api_service.schedule_post_parse(session, project_id=project_id)
    if scheduled["index_jobs"] or scheduled["match_job"]:
        logger.info(
            "解析完成编排 project_id=%s index_jobs=%s match_job=%s",
            project_id, scheduled["index_jobs"], scheduled["match_job"],
        )


def _execute_parse_tender_document(session, input_ref: str | None,
                                   project_id: str | None = None) -> None:
    """parse.tender_document 执行器（R021-5 / F021 §2 第 3-6 步）：

    不可变原文（material_versions.object_uri）→ route_document（PDF 逐页/DOCX 解包/
    扫描件 OCR）→ extract_rule_candidates + extract_main_card → store_candidates 幂等落库
    （kind 分流 rule_candidate / main_card_field）→ material.parse_status=manual_review。

    人工确认（confirm API）后才写 RuleSet/Requirement 并触发索引+匹配（F021 §2 第 7 步），
    本执行器不自动写规则、不伪造成功。input_ref = material_id（取最新版本）。
    """
    if not input_ref:
        raise ValueError("parse.tender_document 任务缺少 input_ref（material_id）")
    from pathlib import Path

    from sqlalchemy import select

    from runtime.core.config import object_store_root
    from runtime.db import api_service, parse_service
    from runtime.db.models import Material, MaterialVersion
    from runtime.parsing.extractor import extract_main_card, extract_rule_candidates, extract_term_candidates
    from runtime.parsing.router import route_document

    material = session.scalar(
        select(Material)
        .where(Material.material_id == input_ref)
        .order_by(Material.version.desc())
        .limit(1)
    )
    if material is None:
        raise ValueError(f"材料不存在: {input_ref}")
    mv = session.get(MaterialVersion, (material.material_id, material.version))
    if mv is None:
        raise RuntimeError(f"material_versions 缺不可变记录 {material.material_id}:v{material.version}")
    path = Path(object_store_root()) / mv.object_uri
    if not path.is_file():
        raise RuntimeError(f"原文缺失: {path}（索引/解析不得伪造产物）")

    route = route_document(str(path))
    if route.kind in ("unsupported", "error") or not route.pages:
        raise RuntimeError(f"文档路由失败 kind={route.kind} error={route.error}")

    project = material.project_id or project_id or ""
    rule_cands = extract_rule_candidates(
        route.pages, project_id=project, material_id=material.material_id,
        content_hash=material.content_hash,
    )
    field_cands = extract_main_card(route.pages)
    term_cands = extract_term_candidates(route.pages)
    # P4（docs/10 §5）：规则未命中的必查 hard 候选 → 云端大模型仅定位 verbatim 摘录 → 规则复核
    # 通过才替换 missing（confidence=low 待人工确认）；LLM 关闭/失败一律保持 missing，不推断
    try:
        from runtime.parsing.llm_fallback import fallback_rule_candidates
        rule_cands = fallback_rule_candidates(route.pages, rule_cands, permission_scope="public_read")
    except Exception as exc:  # 兜底层任何异常不得阻断确定性主链
        logger.warning("LLM 兜底（规则候选）跳过：%s", exc)
    c_r, s_r = parse_service.store_candidates(
        session, project_id=project, material_id=material.material_id,
        version=material.version, kind="rule_candidate",
        candidates=[c.to_dict() for c in rule_cands],
    )
    c_f, s_f = parse_service.store_candidates(
        session, project_id=project, material_id=material.material_id,
        version=material.version, kind="main_card_field",
        candidates=[c.to_dict() for c in field_cands],
    )
    c_t, s_t = parse_service.store_candidates(
        session, project_id=project, material_id=material.material_id,
        version=material.version, kind=parse_service.CANDIDATE_KIND_TERM,
        candidates=[c.to_dict() for c in term_cands],
    )
    if c_r == 0 and c_f == 0 and c_t == 0:
        raise RuntimeError(
            f"材料 {material.material_id}:v{material.version} 未产出任何候选"
            "（全部与既有候选重复且未落库？禁止静默通过）"
        )
    parse_service.mark_material_manual_review(
        session, material_id=material.material_id, version=material.version,
        note=f"解析候选已生成 rule={c_r} main_card={c_f} term={c_t}，等待人工复核确认（F021 §2.7）",
    )
    api_service.audit(
        session, actor="system", action="parse.candidates_stored",
        basis=f"material={material.material_id}:v{material.version} kind={route.kind}",
        outcome=f"rule_created={c_r} rule_skipped={s_r} field_created={c_f} field_skipped={s_f} "
                f"term_created={c_t} term_skipped={s_t}",
        object_ref=material.project_id or project_id,
    )
    session.commit()
    logger.info(
        "招标文件解析完成 material=%s:%s kind=%s rules=%s(+%s) fields=%s(+%s) terms=%s(+%s) → manual_review",
        material.material_id, material.version, route.kind,
        c_r, s_r, c_f, s_f, c_t, s_t,
    )


def _execute_ocr_route(session, input_ref: str | None) -> None:
    """ocr.route 执行器（R022-② / F022 §2 第 4-6 步）：

    企业资料材料（不可变原文）→ Document Router 分流（文本 PDF/DOCX 直接
    抽取；扫描件 tesseract OCR 逐页置信度）→ EvidenceFile 落库
    （page_no/ocr_confidence/source_hash），低置信（<0.6）或损坏/不支持 →
    review_status=pending_review 进人工复核队列；文本直读 → 无需复核。

    不静默入库（F022 §5）：路由失败/无产物抛错 → 任务 retryable/failed，
    不伪造成功。input_ref = material_id（取最新版本）。"""
    if not input_ref:
        raise ValueError("ocr.route 任务缺少 input_ref（material_id）")
    from pathlib import Path

    from sqlalchemy import select

    from runtime.core.config import object_store_root
    from runtime.db import api_service
    from runtime.db.models import EvidenceFile, Material, MaterialVersion
    from runtime.parsing.router import route_document

    material = session.scalar(
        select(Material)
        .where(Material.material_id == input_ref)
        .order_by(Material.version.desc())
        .limit(1)
    )
    if material is None:
        raise ValueError(f"材料不存在: {input_ref}")
    mv = session.get(MaterialVersion, (material.material_id, material.version))
    if mv is None:
        raise RuntimeError(f"material_versions 缺不可变记录 {material.material_id}:v{material.version}")
    path = Path(object_store_root()) / mv.object_uri
    if not path.is_file():
        raise RuntimeError(f"原文缺失: {path}（OCR 不得伪造产物）")

    route = route_document(str(path))
    if route.kind in ("unsupported", "error") or not route.pages:
        # 损坏/不支持 → 不静默跳过：任务失败可重试，人工可在 review-queue 看到
        raise RuntimeError(f"OCR 文档路由失败 kind={route.kind} error={route.error} note={route.note}")

    needs_review = route.needs_review or (route.confidence is not None and route.confidence < 0.6)
    page = next((p for p in route.pages), None)
    existing = session.scalar(
        select(EvidenceFile).where(
            EvidenceFile.material_id == material.material_id,
            EvidenceFile.source_hash == material.content_hash,
        )
    )
    if existing is not None:
        logger.info("OCR 结果已存在 evidence=%s material=%s（幂等跳过）",
                    existing.evidence_id, material.material_id)
        session.commit()
        return
    ev = EvidenceFile(
        evidence_id=f"E-{_dt.datetime.now(_dt.timezone.utc).strftime('%s')}-ocr",
        material_id=material.material_id,
        file_type=material.material_type or "证书扫描件",
        object_uri=mv.object_uri,
        source_hash=material.content_hash,
        page_no=page.page_no if page else None,
        ocr_confidence=route.confidence,
        classification=material.classification or "internal",
        uploaded_by="system:ocr",
        review_status="pending_review" if needs_review else None,
        review_note=(f"kind={route.kind} 低置信度需人工复核" if needs_review else None),
    )
    session.add(ev)
    api_service.audit(
        session, actor="system", action="ocr.route.completed",
        basis=f"material={material.material_id}:v{material.version} kind={route.kind}",
        outcome=f"evidence={ev.evidence_id} confidence={route.confidence} "
                f"review={'pending' if needs_review else 'none'}",
        object_ref=material.project_id or material.material_id,
    )
    session.commit()
    logger.info("OCR 路由完成 material=%s kind=%s confidence=%s review=%s",
                material.material_id, route.kind, route.confidence,
                "pending_review" if needs_review else "none")


def _pages_from_material_version(session, material_id: str, version: int) -> list:
    """从不可变原文（material_versions.object_uri）重路由出 ParsedPage 列表。

    knowledge_index 的解析产物来源：parse 候选只存结构化结果，不存整页文本；
    页文本一律以不可变原文为准重路由（内容哈希在 index_material 内复核），
    避免引入新的页文本持久化表（v1 从简）。
    """
    from pathlib import Path

    from runtime.core.config import object_store_root
    from runtime.db.models import MaterialVersion
    from runtime.parsing.router import route_document

    mv = session.get(MaterialVersion, (material_id, version))
    if mv is None:
        raise RuntimeError(f"material_versions 缺不可变记录 {material_id}:v{version}")
    path = Path(object_store_root()) / mv.object_uri
    if not path.is_file():
        raise RuntimeError(f"原文缺失: {path}（无法重建解析产物）")
    route = route_document(str(path))
    if route.kind in ("unsupported", "error") or not route.pages:
        raise RuntimeError(f"文档路由失败 kind={route.kind} error={route.error}")
    return route.pages


def _execute_knowledge_index(session, input_ref: str | None) -> None:
    """knowledge_index 执行器：材料版本 → 解析产物 → chunk + embedding（方案 §8.1）。

    解析产物由不可变原文重路由重建（_pages_from_material_version）；缺失/路由失败
    抛 IndexingError/RuntimeError → 任务 retryable/manual_review，不伪造成功（F025 §8.1）。
    """
    if not input_ref or ":" not in input_ref:
        raise ValueError("knowledge_index 任务 input_ref 必须为 material_id:version")
    material_id, version = input_ref.rsplit(":", 1)
    pages = _pages_from_material_version(session, material_id, int(version))
    from runtime.rag.indexer import IndexingError, index_material

    try:
        result = index_material(session, material_id, int(version), parsed_pages=pages)
    except IndexingError as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    from runtime.db import api_service

    api_service.audit(
        session, actor="system", action="knowledge.index.completed",
        basis=f"material={material_id}:v{version} idx={result.index_version}",
        outcome=f"created={result.created} skipped={result.skipped}",
        object_ref=material_id,
    )
    session.commit()


def _execute_announcement_search(session, input_ref: str | None, job_id: str | None) -> None:
    """announcement.search 执行器（R004/F020 §2.2.1）：真实公告搜索。

    input_ref = JSON {keyword, region, sources}（POST 时生成，v1.7 不再携带
    category/scale——种类/规模由候选列表内筛选接口承担）；逐源执行
    robots 预检 → 限频 → 抓列表页 → 解析 → 关键词过滤 → 候选落库；
    逐源结果摘要（含限频/robots 拒绝的如实说明）写入 job.result_summary，
    供 GET 轮询展示（红线拒绝不静默、不伪造成功）。
    """
    if not input_ref:
        raise ValueError("announcement.search 任务缺少 input_ref（搜索参数 JSON）")
    from runtime.collecting import service as collecting

    payload = json.loads(input_ref)
    job_id = job_id or ""
    summary = collecting.search_sources(
        session,
        keyword=str(payload.get("keyword") or ""),
        region=payload.get("region"),
        sources=payload.get("sources"),
        search_job_id=job_id,
    )
    collecting.save_result_summary(session, job_id, summary)
    found = sum(int(s.get("count") or 0) for s in summary if s.get("status") == "ok")
    logger.info("公告搜索完成 job_id=%s keyword=%r 候选=%s summary=%s",
                job_id, payload.get("keyword"), found,
                [{"source_id": s["source_id"], "status": s["status"], "count": s.get("count")}
                 for s in summary])


def _execute_announcement_import(session, input_ref: str | None) -> None:
    """announcement.import_detail 执行器：候选公告详情原文抓取入库。

    input_ref = candidate_id；抓详情（robots+限频）→ 原文固化（material）→
    Project 建档 → 候选 import_status=imported；失败落账 failed 后上抛
    （任务 retryable/failed，详情页字段缺失不推断）。
    """
    if not input_ref:
        raise ValueError("announcement.import_detail 任务缺少 input_ref（candidate_id）")
    from runtime.collecting import service as collecting
    from runtime.core.config import object_store_root
    from runtime.db.models import AnnouncementCandidate

    candidate = session.get(AnnouncementCandidate, input_ref)
    if candidate is None:
        raise ValueError(f"候选公告不存在: {input_ref}")
    if candidate.import_status == "imported":
        logger.info("候选公告已入库 candidate=%s project=%s（幂等跳过）",
                    candidate.candidate_id, candidate.project_id)
        return
    try:
        result = collecting.import_candidate_detail(
            session,
            candidate=candidate,
            actor=candidate.requested_by or "system:announcement_import",
            store_root=object_store_root(),
        )
    except Exception as exc:
        collecting.mark_import_failed(session, candidate.candidate_id, exc)
        logger.info("公告详情入库失败 candidate=%s err=%s:%s",
                    candidate.candidate_id, type(exc).__name__, str(exc)[:200])
        raise
    from runtime.db.models import AnalysisJob

    job = session.get(AnalysisJob, candidate.import_job_id or "")
    if job is not None and job.result_summary is None:
        job.result_summary = [{"source_id": candidate.source_id, "status": "imported",
                               "count": 1, "note": f"project={result['project_id']}"}]
        session.commit()


def _execute(session, kind: str, input_ref: str | None, project_id: str | None = None,
             job_id: str | None = None) -> None:
    """任务执行器：knowledge_index 执行真实索引（解析产物重路由）；
    parse.tender_document 执行 原文路由→条款/主卡候选→manual_review（R021-5）；
    match.* 执行 RAG→核验→规则引擎链路；announcement.* 执行真实公告搜索/详情入库
    （R004/F020）；其余保持占位（R022-R024 接入）。"""
    if kind == "knowledge_index":
        _execute_knowledge_index(session, input_ref)
        return
    if kind == "parse.tender_document":
        _execute_parse_tender_document(session, input_ref, project_id)
        return
    if kind == "ocr.route":
        _execute_ocr_route(session, input_ref)
        return
    if kind == "match.run":
        _execute_match_run(session, project_id, mode="gate")
        return
    if kind == "match.recalculate":
        _execute_match_run(session, project_id, mode="gate")
        return
    if kind == "announcement.search":
        _execute_announcement_search(session, input_ref, job_id)
        return
    if kind == "announcement.import_detail":
        _execute_announcement_import(session, input_ref)
        return
    if not input_ref:
        raise ValueError("任务缺少 input_ref")
    # 演示：模拟耗时工作，便于验证心跳与超时恢复
    time.sleep(0.2)


def _execute_match_run(session, project_id: str | None, *, mode: str = "gate",
                       retrieve_fn=None, evaluate_fn=None) -> None:
    """match.run / match.recalculate 执行器（方案 §3.5：RAG→结构化核验→规则引擎→快照）。

    不可执行（缺规则集/无 as_of/引擎不可用）→ 抛异常 → 任务 retryable，不伪造成功；
    知识索引未就绪 → 降级为结构化 active 快照判定（方案 §5 可解释降级）。
    """
    if not project_id:
        raise ValueError("match 任务缺少 project_id")
    import uuid as _uuid

    from sqlalchemy import or_, select

    from runtime.core import matching
    from runtime.db import api_service
    from runtime.db.models import (
        Manager,
        MatchItem,
        MatchRun,
        Performance,
        Personnel,
        Qualification,
        Requirement,
        RuleSet,
    )

    # 1) 规则集与判定时点（as_of 不得默认当前时间，F008 §4.1）
    rule_set = session.scalar(
        select(RuleSet)
        .where(or_(RuleSet.project_id == project_id, RuleSet.project_id.is_(None)))
        .order_by(RuleSet.created_at.desc())
        .limit(1)
    )
    if rule_set is None:
        raise matching.MatchNotRunnableError(f"项目 {project_id} 无规则集，匹配不可执行")
    requirements = session.scalars(
        select(Requirement).where(Requirement.rule_set_id == rule_set.rule_set_id)
    ).all()
    if not requirements:
        raise matching.MatchNotRunnableError(f"规则集 {rule_set.rule_set_id} 无要求条目")
    as_of = next((r.as_of for r in requirements if r.as_of), None)
    if not as_of:
        raise matching.MatchNotRunnableError("规则集未提供 as_of（判定时点不得默认当前时间）")

    # 2) 结构化 active 快照（截至 as_of，F023 §2.3）
    evidence = matching.build_enterprise_evidence(
        qualifications=session.scalars(select(Qualification)).all(),
        performances=session.scalars(select(Performance)).all(),
        managers=session.scalars(select(Manager)).all(),
        personnel=session.scalars(select(Personnel)).all(),
        as_of=as_of,
    )

    # 3) RAG 候选发现 + 结构化核验（索引未就绪 → 可解释降级）
    candidates = _collect_rag_candidates(
        session, project_id, requirements, evidence, as_of, retrieve_fn=retrieve_fn
    )

    # 4) 规则判定：只有核验通过/明确的证据进入引擎，向量分数不参与（方案 §3.5 第 4 步）
    req_dicts = [matching.requirement_dict(r) for r in requirements]
    result = matching.run_match(
        requirements=req_dicts, evidence=evidence, as_of=as_of,
        mode=mode, lot_id=None, evaluate_fn=evaluate_fn,
    )

    # 5) 快照落库（MatchRun + MatchItem，方案 §3.5 第 5 步 / F024 §2 可回放）
    run = MatchRun(
        run_id=f"MR-{_uuid.uuid4().hex[:12]}",
        project_id=project_id,
        rule_set_id=rule_set.rule_set_id,
        as_of=as_of,
        mode=mode,
        coverage=result["coverage"],
        status="completed",
        retrieval_run_id=candidates["main_retrieval_run_id"],
        index_version=candidates["index_version"],
        candidate_chunk_ids=candidates["candidate_chunk_ids"],
        structured_verification=candidates["verification_summary"],
        evidence_snapshot_hash=matching.snapshot_hash(evidence),
    )
    session.add(run)
    session.flush()
    refs_by_req = candidates["evidence_refs_by_req"]
    for entry in result["matrix"]:
        refs = refs_by_req.get(entry["requirement_id"], {})
        session.add(
            MatchItem(
                item_id=f"MI-{_uuid.uuid4().hex[:12]}",
                run_id=run.run_id,
                requirement_id=entry["requirement_id"],
                match_result=entry["match_result"],
                score=entry.get("score"),
                max_score=entry.get("max_score"),
                match_reason={
                    "text": entry["match_reason"],
                    "retrieval_run_id": refs.get("retrieval_run_id"),
                },
                evidence_refs=refs.get("evidence_refs"),
                evaluated_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )
    api_service.audit(
        session, actor="system", action="match.completed",
        basis=f"rule_set={rule_set.rule_set_id} as_of={as_of} "
              f"evidence_snapshot={run.evidence_snapshot_hash[:12]}",
        outcome=f"complete={result['coverage'].get('complete')} "
                f"executed={result['coverage'].get('executed')}",
        object_ref=project_id,
    )

    # 6) 准入结果生成（F023 §2 第 6 步 / R024）：gate 运行 → AdmissionResult 不可变快照
    #    + 旧结果置 stale + 项目准入状态迁移（qualified_full_score/blocked_*）。
    #    mode=gate 才生成——诊断运行不驱动准入状态（F023 §2 第 4 步）。
    admission = None
    if mode == "gate":
        from runtime.db.admission_service import generate_admission_result

        admission = generate_admission_result(
            session,
            run=run,
            engine_result=result,
            requirements=req_dicts,
            evidence=evidence,
        )

    session.commit()
    logger.info(
        "匹配完成 project_id=%s run=%s coverage=%s retrieval=%s admission=%s",
        project_id, run.run_id, result["coverage"], run.retrieval_run_id,
        f"{admission.result_id}:{admission.internal_admission_result.get('status')}"
        if admission else None,
    )


def _collect_rag_candidates(session, project_id: str, requirements, evidence: dict,
                            as_of: str, *, retrieve_fn=None) -> dict:
    """每条 hard/scored 要求以条款文本检索 L2/L3 候选（方案 §3.5 第 2 步）。

    索引未就绪/检索失败 → 降级（方案 §5 可解释降级，不伪造成功）；
    L3 候选按 material_id 关联结构化记录做约束核验：blocked（无法判定）的
    记录从判定输入剔除（unverifiable 语义），failed（明确不满足）保留给引擎判 not_satisfied。
    """
    from runtime.core import matching
    from runtime.core import rbac
    from runtime.rag.filters import VisibilityError, build_visibility_predicate
    from runtime.rag.retriever import KnowledgeNotReadyError, RetrievalError, hybrid_search
    from runtime.rag.schemas import SearchRequest
    from runtime.db.models import KnowledgeChunk

    from sqlalchemy import select

    out: dict = {
        "main_retrieval_run_id": None,
        "index_version": None,
        "candidate_chunk_ids": [],
        "verification_summary": {"rag_degraded": None, "checked": [], "removed_material_ids": []},
        "evidence_refs_by_req": {},
    }
    degraded: str | None = None
    checked: list[dict] = []
    removed: set[str] = set()
    for req in requirements:
        req_dict = matching.requirement_dict(req)
        query = req_dict.get("assertion") or req_dict.get("clause_ref")
        if not query:
            continue
        try:
            request = SearchRequest(
                query=query,
                knowledge_layers=["L2_tender", "L3_enterprise"],
                project_id=project_id,
                as_of=as_of,
                top_k=10,
            )
            from runtime.rag.filters import Actor

            vis = build_visibility_predicate(Actor(role=rbac.BUSINESS_HEAD, actor="system"), request)
            if retrieve_fn is not None:
                # 测试/特定编排器显式注入时保持既有调用契约。
                response = retrieve_fn(session, request, vis, role=rbac.BUSINESS_HEAD)
            else:
                # 自动匹配必须注入真实查询 embedding；否则 hybrid_search 会按设计退化为
                # keyword-only，无法利用已建的 pgvector 索引。
                from runtime.rag.indexer import _embed as query_embed

                response = hybrid_search(
                    session,
                    request,
                    vis,
                    role=rbac.BUSINESS_HEAD,
                    embed_query_fn=query_embed,
                )
        except (KnowledgeNotReadyError, RetrievalError, VisibilityError, ValueError) as exc:
            if degraded is None:
                degraded = f"{type(exc).__name__}: {exc}"
            continue
        if response.retrieval_run_id:
            out["main_retrieval_run_id"] = out["main_retrieval_run_id"] or response.retrieval_run_id
        out["index_version"] = out["index_version"] or response.index_version
        per_req: dict = {
            "retrieval_run_id": response.retrieval_run_id,
            "evidence_refs": [],
            "passed": [], "failed": [], "blocked": [],
        }
        chunks = session.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.chunk_id.in_([i.chunk_id for i in response.items])
            )
        ).all()
        for chunk in chunks:
            out["candidate_chunk_ids"].append(chunk.chunk_id)
            if chunk.knowledge_layer != "L3_enterprise" or not chunk.material_id:
                continue  # L2 条款候选仅登记回链（citation），不进入证据核验
            related = [
                rec for recs in evidence.values() for rec in recs
                if rec.get("material_id") == chunk.material_id
            ]
            required = matching.candidate_required_from_rule(req_dict.get("rule") or {}, as_of)
            if not related:
                per_req["blocked"].append({
                    "material_id": chunk.material_id, "chunk_id": chunk.chunk_id,
                    "note": "召回但未在 active 结构化快照中（不可判定）",
                })
                removed.add(chunk.material_id)
                continue
            if not required:
                # 规则无结构化约束：候选命中仅登记回链，不做剔除
                per_req["passed"].append({
                    "material_id": chunk.material_id, "chunk_id": chunk.chunk_id,
                    "note": "无结构化约束",
                })
                citation = f"{chunk.material_id}:v{chunk.material_version}:p{chunk.page_no or 0}"
                per_req["evidence_refs"].append(citation)
                continue
            for kind, recs in evidence.items():
                for rec in recs:
                    if rec.get("material_id") != chunk.material_id:
                        continue
                    cand = matching.candidate_from_record(
                        kind, rec, content_hash=chunk.content_hash
                    )
                    vr = matching.gate_candidates([cand], required)
                    if vr.blocked:
                        per_req["blocked"].append({
                            "material_id": chunk.material_id, "chunk_id": chunk.chunk_id,
                            **matching.verification_summary(vr),
                        })
                        removed.add(chunk.material_id)
                    elif vr.failed:
                        per_req["failed"].append({
                            "material_id": chunk.material_id, "chunk_id": chunk.chunk_id,
                            **matching.verification_summary(vr),
                        })
                    else:
                        per_req["passed"].append({
                            "material_id": chunk.material_id, "chunk_id": chunk.chunk_id,
                            **matching.verification_summary(vr),
                        })
                        citation = f"{chunk.material_id}:v{chunk.material_version}:p{chunk.page_no or 0}"
                        per_req["evidence_refs"].append(citation)
        if not chunks:
            per_req["blocked"].append({"note": "无候选证据（召回为空）"})
        if per_req["evidence_refs"]:
            out["evidence_refs_by_req"][req.requirement_id] = {
                "retrieval_run_id": response.retrieval_run_id,
                "evidence_refs": sorted(set(per_req["evidence_refs"])),
            }
        if per_req["passed"] or per_req["failed"] or per_req["blocked"]:
            checked.append({"requirement_id": req.requirement_id, **per_req})
    if removed:
        # 核验 blocked（无法判定）的记录不得进入规则输入（F025 §6）
        for kind in list(evidence):
            evidence[kind] = [
                r for r in evidence[kind] if r.get("material_id") not in removed
            ]
            if not evidence[kind]:
                del evidence[kind]
    out["verification_summary"] = {
        "rag_degraded": degraded,
        "checked": checked,
        "removed_material_ids": sorted(removed),
    }
    out["candidate_chunk_ids"] = sorted(set(out["candidate_chunk_ids"]))
    return out


def run_forever() -> None:
    stale_seconds = int(job_running_timeout_seconds())
    poll_interval = worker_poll_interval_seconds()

    stop = False
    stop_event = threading.Event()

    def _on_signal(signum, frame):
        nonlocal stop
        logger.info("收到信号 %s，优雅退出", signum)
        stop = True
        # 唤醒轮询等待，避免 SIGTERM 后还要等完整 poll 间隔才退出。
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from runtime.core.config import database_url

    engine = create_engine(database_url())
    SessionLocal = sessionmaker(bind=engine)

    logger.info("worker 启动 runner_id=%s poll=%ss stale=%ss", RUNNER_ID, poll_interval, stale_seconds)
    while not stop:
        session = SessionLocal()
        try:
            processed = process_one(session, RUNNER_ID, stale_seconds=stale_seconds)
        except Exception:
            logger.exception("worker 循环异常")
            processed = False
        finally:
            session.close()
        if not processed:
            stop_event.wait(poll_interval)


def main() -> None:
    logging.config.dictConfig(logging_config())
    run_forever()


if __name__ == "__main__":
    main()
