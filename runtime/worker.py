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
from runtime.core.matching import GateBlockedError
from runtime.db.models import AnalysisJob
from runtime.db.worker_service import claim_job, finish_job, heartbeat_job, record_heartbeat

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
        # 注意：parse.tender_document 完成后【不】在此调度索引/匹配（F021 §2 第 7 步：
        # 编排发生在人工复核确认 confirm → parse_status=parsed 之后）。
        # 2026-09-15 修复：此前这里调 _on_parse_completed 会把 match.run 提前排进队列，
        # 候选未确认时必然「项目无规则集」失败，且失败任务占住幂等键，confirm 之后也补不回来。
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
    except GateBlockedError as exc:
        # ADR-004 服务端门禁（项目过期/身份冲突）：确定性阻断，重试必再被拦，直接终态失败留痕；
        # 纠正数据（延期公告更新截止/修正项目关联）后由人工重新发起任务。
        logger.warning("任务被门禁拦截 job_id=%s kind=%s gate=%s error=%s", job_id, kind, exc.code, exc)
        session.rollback()
        try:
            finish_job(session, job_id, outcome="failed", error_code=f"gate_{exc.code}",
                       error_message=str(exc)[:500])
        except Exception:
            session.rollback()
            from runtime.db.worker_service import fail_then_retryable as _retryable

            _retryable(session, job_id, error_code=f"gate_{exc.code}", error_message=str(exc)[:500])
    except Exception as exc:
        # session 可能已处于 PendingRollback（如 store_candidates flush 撞主键），
        # 必须先 rollback 才能继续读写；否则 fail_then_retryable 二次抛错、
        # job 停留 running 直至 heartbeat_timeout（2026-09-03 MAT-TEST-001 实测 3 次）。
        logger.exception("任务执行异常 job_id=%s kind=%s error=%s", job_id, kind, type(exc).__name__)
        session.rollback()
        from runtime.db.worker_service import fail_then_retryable

        fail_then_retryable(session, job_id, error_code=type(exc).__name__, error_message=str(exc)[:500])
    return True


def _execute_parse_tender_document(session, input_ref: str | None,
                                   project_id: str | None = None,
                                   job_id: str | None = None) -> None:
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
        content_hash=material.content_hash, version=material.version,
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
    # P4-2（2026-09-15）：锚点未覆盖、原文明确写了的条款 → 大模型定位逐字摘录 → 低置信
    # generic 候选（必须人工复核；与既有候选/锚点重叠的已在发现层丢弃）
    try:
        from runtime.parsing.llm_fallback import discover_rule_candidates
        # 重叠去重的"既有候选"必须包含条款（term）与主卡（field）候选：此前只传规则候选，
        # 大模型把「质量保证金 3%」「履约保证金」这类已由 TERM_ANCHORS 抽到的条款再发现
        # 一遍 → 复核页同一条款出现两行（邢台实测 2026-09-24"重复"问题的一部分）
        rule_cands.extend(discover_rule_candidates(
            route.pages, rule_cands + term_cands + field_cands, project_id=project,
            material_id=material.material_id, content_hash=material.content_hash,
            permission_scope="public_read", version=material.version))
    except Exception as exc:
        logger.warning("LLM 条款发现跳过：%s", exc)
    # 重新解析：上一轮仍是 pending 的大模型发现草稿先清掉——发现候选 ID 是摘录内容哈希，
    # LLM 每轮摘录略有差异 → 不清理会跨轮累积出重复行（已决策行不动，审计留痕）
    pruned = parse_service.prune_pending_llm_drafts(
        session, material_id=material.material_id, version=material.version)
    if pruned:
        logger.info("重新解析：清理上一轮 pending 的 LLM 草稿 %s 条（material=%s）",
                    pruned, material.material_id)
    c_r, s_r = parse_service.store_candidates(
        session, project_id=project, material_id=material.material_id,
        version=material.version, kind="rule_candidate",
        candidates=[c.to_dict() for c in rule_cands],
        refresh_pending=True,
    )
    c_f, s_f = parse_service.store_candidates(
        session, project_id=project, material_id=material.material_id,
        version=material.version, kind="main_card_field",
        candidates=[c.to_dict() for c in field_cands],
        refresh_pending=True,
    )
    c_t, s_t = parse_service.store_candidates(
        session, project_id=project, material_id=material.material_id,
        version=material.version, kind=parse_service.CANDIDATE_KIND_TERM,
        candidates=[c.to_dict() for c in term_cands],
        refresh_pending=True,
    )
    # 重新解析既有材料：抽取器升级后新锚点会增量落库，但若全部候选均已存在（无新增），
    # 属幂等重放而非空解析——正常完成并如实留痕，让既有候选继续人工复核（2026-09-15：
    # 此前一律 RuntimeError，用户对 manual_review 材料点「重新解析」必然收到任务失败）。
    existing_candidates = None
    if c_r == 0 and c_f == 0 and c_t == 0:
        from runtime.db.models import ParseCandidate as _PC

        existing_candidates = len(session.scalars(
            select(_PC.candidate_id).where(
                _PC.material_id == material.material_id, _PC.version == material.version)
        ).all())
        if existing_candidates > 0:
            logger.info(
                "重新解析无新增候选（幂等重放）material=%s:%s 既有候选=%s，保留待复核",
                material.material_id, material.version, existing_candidates,
            )
        else:
            raise RuntimeError(
                f"材料 {material.material_id}:v{material.version} 未产出任何候选"
                "（解析产物为空，禁止静默通过）"
            )
    note = (f"重新解析：无新增候选（既有 {existing_candidates} 条保留待复核）"
            if existing_candidates is not None
            else f"解析候选已生成 rule={c_r} main_card={c_f} term={c_t}，等待人工复核确认（F021 §2.7）")
    parse_service.mark_material_manual_review(
        session, material_id=material.material_id, version=material.version, note=note,
    )
    outcome = (f"rule_created={c_r} rule_skipped={s_r} field_created={c_f} field_skipped={s_f} "
               f"term_created={c_t} term_skipped={s_t}")
    if existing_candidates is not None:
        outcome += f" idempotent_replay=true existing_candidates={existing_candidates}"
    api_service.audit(
        session, actor="system", action="parse.candidates_stored",
        basis=f"material={material.material_id}:v{material.version} kind={route.kind}",
        outcome=outcome,
        object_ref=material.project_id or project_id,
    )
    # 覆盖率报告（2026-09-24 四步之四）：解析完立即产出「靠什么定位/还缺什么」，
    # 写入任务 result_summary（任务历史可查）+ 一行人话日志
    try:
        coverage = parse_service.parse_coverage(
            session, project_id=project, material_id=material.material_id,
            version=material.version)
        if coverage is not None and job_id:
            job = session.get(AnalysisJob, job_id)
            if job is not None:
                job.result_summary = [{"kind": "parse_coverage", **coverage}]
        if coverage is not None:
            loc = coverage["located"]
            logger.info(
                "解析覆盖率 material=%s:%s 规则候选 %s 条（锚点 %s / LLM兜底 %s / LLM发现 %s）"
                "待人工定位缺失 %s · 主卡字段 %s/%s · 商务条款 %s 条",
                material.material_id, material.version, coverage["rule_total"],
                loc["anchor"], loc["llm_fallback"], loc["llm_discovery"],
                coverage["missing_pending"], coverage["main_card_located"],
                coverage["main_card_total"], coverage["term_total"],
            )
    except Exception as exc:  # 报告失败不阻断解析主链
        logger.warning("覆盖率报告生成失败（不影响解析结果）：%s", type(exc).__name__)
    # ADR-007（2026-09-25）：解析自动确认与例外驱动复核——用户上传后直达风险判定。
    # ① 高置信锚点候选系统自动通过（留痕可改判）；② 例外式规则集（pending 例外随快照
    # 留档，不写 Requirement）；③ 自动触发索引+匹配（schedule_post_parse 幂等）。
    # 任何异常不阻断解析主链（退化为旧的人工确认路径）。
    try:
        auto = parse_service.auto_confirm_anchor_candidates(
            session, project_id=project, material_id=material.material_id,
            version=material.version)
        if auto["approved"]:
            api_service.audit(
                session, actor="system", action="parse.auto_confirmed",
                basis=f"material={material.material_id}:v{material.version}",
                outcome=f"approved={auto['approved']} remaining_pending={auto['remaining_pending']}",
                object_ref=project,
            )
        suggestion = parse_service.suggested_as_of(
            session, project_id=project, material_id=material.material_id,
            version=material.version)
        if suggestion is not None:
            try:
                confirmed = parse_service.confirm_rules_from_approved(
                    session, project_id=project, material_id=material.material_id,
                    version=material.version, as_of=suggestion["as_of"],
                    created_by=parse_service.AUTO_REVIEWER,
                    allow_pending_exceptions=True,
                )
            except parse_service.RuleSetExists:
                confirmed = None  # 重解析幂等：规则集已存在，例外走 reconfirm 快照链
            if confirmed and confirmed["rule_set_id"]:
                parse_service.confirm_main_card_fields(
                    session, material_id=material.material_id, version=material.version,
                    project_id=project, actor=parse_service.AUTO_REVIEWER,
                    content_hash=material.content_hash,
                )
                rejected = confirmed["rejected"]
                exceptions = confirmed["pending"]
                if rejected or exceptions:
                    parse_service.mark_material_manual_review(
                        session, material_id=material.material_id, version=material.version,
                        note=f"ADR-007 例外驱动：待确认例外 {exceptions} 条、驳回 {rejected} 条"
                             "（风险页逐条处置，清零后自动重算）",
                    )
                else:
                    parse_service.mark_material_parsed(
                        session, material_id=material.material_id, version=material.version,
                        actor=parse_service.AUTO_REVIEWER)
                api_service.schedule_post_parse(session, project_id=project)
                logger.info(
                    "ADR-007 自动确认 material=%s:%s 自动通过=%s 规则集=%s（要求 %s 条，"
                    "待确认例外 %s 条）→ 已触发索引/匹配",
                    material.material_id, material.version, auto["approved"],
                    confirmed["rule_set_id"], confirmed["created_requirements"], exceptions,
                )
        else:
            logger.info(
                "ADR-007：材料 %s:%s 无 as_of 来源（deadline_bid 未定位）→ 退人工确认路径",
                material.material_id, material.version)
    except Exception as exc:  # 自动路径失败不阻断：候选已落库，人工确认流程照常可用
        session.rollback()
        logger.warning("ADR-007 自动确认/规则集生成失败（退人工确认路径）：%s", type(exc).__name__)
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
        _execute_parse_tender_document(session, input_ref, project_id, job_id)
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
        Material,
        MatchItem,
        MatchRun,
        Performance,
        Personnel,
        Project,
        ProjectIdentity,
        Qualification,
        Requirement,
        RuleSet,
    )

    # 0) ADR-004 服务端门禁（gate 运行驱动准入状态；诊断运行不受限）：
    #    项目身份未校验则先自动校验一次（不覆盖既有/人工确认结果）→ 过期 / 身份冲突 → 终态失败留痕
    project = session.get(Project, project_id)
    if mode == "gate" and project is not None:
        from runtime.db import identity_service, lifecycle_service

        identity_service.ensure_project_identity(session, project_id=project_id, actor="system:match")
        blocked = lifecycle_service.gate_reason(session, project, action="match", actor="system:match")
        if blocked is not None:
            code, message = blocked
            api_service.audit(
                session, actor="system:match", action="match.gate_blocked",
                basis=f"project_id={project_id} gate=match", outcome=code, object_ref=project_id,
            )
            session.commit()
            raise matching.GateBlockedError(code, message)

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

    # 2) 结构化 active 快照（截至 as_of，F023 §2.3）+ 台账口径上下文（docs/12 §3.2 状态拆分）
    quals = session.scalars(select(Qualification)).all()
    perfs = session.scalars(select(Performance)).all()
    mgrs = session.scalars(select(Manager)).all()
    ppls = session.scalars(select(Personnel)).all()
    evidence = matching.build_enterprise_evidence(
        qualifications=quals, performances=perfs, managers=mgrs, personnel=ppls, as_of=as_of,
    )
    # 台账口径（docs/12 8.1-1）：区分「台账 0 条记录=真缺资料」与「有记录但未核验/过期」
    ledger_counts, ineligible = matching.build_ledger_context(
        qualifications=quals, performances=perfs, managers=mgrs, personnel=ppls, as_of=as_of,
    )

    # 2b) 解析例外快照（ADR-007 / docs/12 §2.5/§3.4：独立解析质量对象 + 快照版本）
    from runtime.db import parse_service as _parse_service

    pending_rows = _parse_service.pending_exception_rows(session, project_id=project_id)
    pe_snapshot_version = _parse_exception_snapshot_version(pending_rows)
    _sync_parse_exceptions(session, project_id=project_id, pending_rows=pending_rows,
                           snapshot_version=pe_snapshot_version)
    pending_exc_count = len(pending_rows)

    # 2c) run_context 输入版本固化（docs/12 §3.5：所有投影接口原样回显同一上下文）
    historical_sample = bool((getattr(project, "test_context", None) or {}).get("historical_sample"))
    run_context = _build_run_context(
        project_id=project_id, rule_set=rule_set, as_of=as_of,
        materials=session.scalars(select(Material).where(Material.project_id == project_id)).all(),
        identity=session.get(ProjectIdentity, project_id),
        evidence_snapshot_hash=matching.snapshot_hash(evidence),
        managers=mgrs, ledger_ineligible=ineligible,
        parse_exception_snapshot_version=pe_snapshot_version,
        engine_result=None, historical_sample=historical_sample,
    )
    run_input_fingerprint = _run_input_fingerprint(run_context, lot_id=None)

    # 2d) 运行级幂等（docs/12 §4.3-5）：相同输入复用既有结果，任一输入不同创建新运行。
    #     仅当既有运行仍为 current 且其准入结果未失效时复用（stale 结果必须重算恢复 current）。
    if mode == "gate":
        reused = session.scalar(
            select(MatchRun).where(
                MatchRun.project_id == project_id,
                MatchRun.run_input_fingerprint == run_input_fingerprint,
                MatchRun.status == "completed",
                MatchRun.is_current.is_(True),
            ).limit(1)
        )
        if reused is not None:
            from runtime.db.models import AdmissionResult as _AR

            reused_admission = session.scalar(
                select(_AR).where(_AR.run_id == reused.run_id, _AR.result_freshness == "current").limit(1)
            )
            if reused_admission is not None:
                api_service.audit(
                    session, actor="system", action="match.reused",
                    basis=f"fingerprint={run_input_fingerprint} reused_run={reused.run_id}",
                    outcome="identical_inputs", object_ref=project_id,
                )
                session.commit()
                logger.info("匹配输入未变化 project_id=%s 复用运行 run=%s（不新建结果）",
                            project_id, reused.run_id)
                return

    # 3) RAG 候选发现 + 结构化核验（索引未就绪 → 可解释降级）
    candidates = _collect_rag_candidates(
        session, project_id, requirements, evidence, as_of, retrieve_fn=retrieve_fn
    )
    run_context["retrieval_run_id"] = candidates["main_retrieval_run_id"]
    run_context["candidate_plan_snapshot_version"] = _candidate_plan_snapshot_version(None)

    # 4) 规则判定：只有核验通过/明确的证据进入引擎，向量分数不参与（方案 §3.5 第 4 步）
    req_dicts = [matching.requirement_dict(r) for r in requirements]
    result = matching.run_match(
        requirements=req_dicts, evidence=evidence, as_of=as_of,
        mode=mode, lot_id=None, evaluate_fn=evaluate_fn,
        ledger_counts=ledger_counts, ineligible=ineligible,
    )
    # 4b) ADR-004 §2.5 证据链最低要求：hard/scored 满足项无企业证据回链 → manual_review，
    #     不计入正式准入（RAG 索引未就绪导致全部无回链时一律降级，不以"匹配跑完"冒充匹配正确）
    result = matching.downgrade_satisfied_without_evidence(
        result, candidates["evidence_refs_by_req"], req_dicts,
    )
    run_context["candidate_plan_snapshot_version"] = _candidate_plan_snapshot_version(result)

    # 5) 快照落库（MatchRun + MatchItem，方案 §3.5 第 5 步 / F024 §2 可回放）
    #    ADR-008（docs/12 §3.5）：新运行产生后同项目旧运行不再是 current（读取端默认
    #    仅返回 current，避免矩阵/队列/待确认页取到不同版本的数据）。
    from sqlalchemy import update as _sa_update

    session.execute(
        _sa_update(MatchRun).where(MatchRun.project_id == project_id, MatchRun.is_current.is_(True))
        .values(is_current=False)
    )
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
        run_context=run_context,
        run_input_fingerprint=run_input_fingerprint,
        is_current=True,
    )
    session.add(run)
    session.flush()
    # JSON 列不追踪原地变更：flag_modified 显式标记脏，确保 run_id 写入持久化快照
    run_context["run_id"] = run.run_id
    run.run_context = dict(run_context)
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    _flag_modified(run, "run_context")
    refs_by_req = candidates["evidence_refs_by_req"]
    max_score_by_req = {r["requirement_id"]: r.get("max_score") for r in req_dicts}
    req_type_domain = {"hard_requirement": "qualification", "scored_requirement": "scoring",
                       "action_requirement": "operation"}
    for entry in result["matrix"]:
        refs = refs_by_req.get(entry["requirement_id"], {})
        # 证据回链合并（2026-09-23 v1.6）：RAG 检索引用 ∪ 引擎满足记录自带 evidence_refs
        evidence_refs = sorted(set(refs.get("evidence_refs") or []) | set(entry.get("evidence_refs") or []))
        if evidence_refs and not refs.get("evidence_refs"):
            refs = dict(refs, evidence_refs=evidence_refs)
            refs_by_req[entry["requirement_id"]] = refs
    for entry in result["matrix"]:
        refs = refs_by_req.get(entry["requirement_id"], {})
        # gate_executed（F023 §2 第 4 步）：门禁短路后由诊断全量补齐的条目为 False，
        # 结果页据此标「门禁短路后·诊断结果」，不得展示为门禁通过（F008 覆盖门禁）
        match_reason = {
            "text": entry["match_reason"],
            "retrieval_run_id": refs.get("retrieval_run_id"),
            "gate_executed": entry.get("gate_executed", True),
            # ADR-004 §2.5：满足项因无证据回链被降级（结果页据此提示「需补证据回链」）
            "evidence_downgraded": bool(entry.get("evidence_downgraded", False)),
        }
        plan_id = entry.get("candidate_plan_ref")
        session.add(
            MatchItem(
                item_id=f"MI-{_uuid.uuid4().hex[:12]}",
                run_id=run.run_id,
                requirement_id=entry["requirement_id"],
                match_result=entry["match_result"],
                domain=entry.get("domain") or req_type_domain.get(entry.get("req_type")),
                reason_code=entry.get("reason_code"),
                evidence_state=entry.get("evidence_state"),
                observed_value=entry.get("observed_value"),
                required_value=entry.get("required_value"),
                candidate_plan_id=(f"{run.run_id}:{plan_id}" if plan_id else None),
                person_id=entry.get("person_id"),
                next_action=entry.get("next_action"),
                score_classification=entry.get("score_classification"),
                task_kind=entry.get("task_kind"),
                score=entry.get("score"),
                max_score=(entry.get("max_score") if entry.get("max_score") is not None
                           else max_score_by_req.get(entry["requirement_id"])),
                match_reason=match_reason,
                evidence_refs=refs.get("evidence_refs"),
                evaluated_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )

    # 5b) 候选班子落库（docs/12 §3.3：人员结论回链同一方案同一人；快照不可覆盖）
    _persist_candidate_plans(session, run=run, engine_result=result)

    # 5c) 动作任务落库（docs/12 §2.4/§3.4：动作项只进执行计划，不进缺证队列）
    _sync_operation_tasks(session, project_id=project_id, run=run,
                          requirements=requirements, engine_result=result)

    api_service.audit(
        session, actor="system", action="match.completed",
        basis=f"rule_set={rule_set.rule_set_id} as_of={as_of} "
              f"evidence_snapshot={run.evidence_snapshot_hash[:12]}",
        outcome=f"complete={result['coverage'].get('complete')} "
                f"executed={result['coverage'].get('executed')} "
                f"gate_executed={result['coverage'].get('gate_executed')} "
                f"short_circuited={len(result['coverage'].get('short_circuited') or [])} "
                f"evidence_downgraded={len(result['coverage'].get('evidence_downgraded') or [])} "
                f"parse_exceptions_pending={pending_exc_count}",
        object_ref=project_id,
    )

    # 6) 准入结果生成（F023 §2 第 6 步 / R024 / docs/12 §4.4）：gate 运行 → AdmissionResult
    #    不可变快照 + 旧结果置 stale + 项目准入状态迁移。
    #    ADR-007 §2.5 + docs/12 §4.3-1：解析例外阻断原因独立为 parse_exceptions_pending；
    #    历史样本测试上下文阻断审批（blocking_reasons 点名），均不计为企业缺证。
    admission = None
    if mode == "gate":
        from runtime.db.admission_service import generate_admission_result

        admission = generate_admission_result(
            session,
            run=run,
            engine_result=result,
            requirements=req_dicts,
            evidence=evidence,
            parse_exception_count=pending_exc_count,
            historical_sample=historical_sample,
        )

    # First commit the immutable MatchRun/MatchItem/AdmissionResult snapshot. Task
    # orchestration is a downstream projection: its failure must neither erase a
    # factual matching result nor be reported as a successful full workflow.
    session.commit()
    task_sync: dict | None = None
    if admission is not None:
        try:
            from runtime.db import workflow_service

            task_sync = workflow_service.sync_tasks_from_admission(
                session, project_id=project_id, match_run_id=run.run_id,
                admission=admission, commit=False, requirements=req_dicts,
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            # Persist an explicit incident before re-raising. The worker will mark
            # the job failed/retryable instead of falsely saying the end-to-end
            # matching-and-remediation workflow completed.
            api_service.audit(
                session, actor="system:match", action="remediation_task.sync_failed",
                outcome=type(exc).__name__, basis=f"run_id={run.run_id}", object_ref=project_id,
            )
            session.commit()
            raise
    logger.info(
        "匹配完成 project_id=%s run=%s coverage=%s retrieval=%s admission=%s task_sync=%s",
        project_id, run.run_id, result["coverage"], run.retrieval_run_id,
        f"{admission.result_id}:{admission.internal_admission_result.get('status')}"
        if admission else None, task_sync,
    )


# ---------------------------------------------------------------------------
# ADR-008 / docs/12 §3.3-§3.5 辅助：解析例外快照、run_context、候选班子与动作任务
# ---------------------------------------------------------------------------

def _hash8(payload) -> str:
    import hashlib as _hashlib

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return _hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _parse_exception_snapshot_version(pending_rows) -> str:
    """解析例外快照版本：PE-<count>-<hash8(候选id 列表)>（docs/12 §3.5）。

    任何一条例外被处置（候选不再 pending）→ 版本变化 → 匹配输入指纹变化 → 新运行。
    """
    ids = sorted(getattr(r, "candidate_id", "") or "" for r in pending_rows)
    return f"PE-{len(ids)}-{_hash8(ids)}"


def _parse_exception_type_and_rank(row) -> tuple[str, int]:
    """单条待确认候选 → (exception_type, risk_rank)（docs/12 §2.5 风险分层）。"""
    from runtime.core.domain_dict import (
        PARSE_EXC_CONFLICT, PARSE_EXC_HARD_ANCHOR_MISS, PARSE_EXC_LLM_PENDING,
        PARSE_EXC_PROBE_MISS, PARSE_EXCEPTION_RISK_RANK,
    )

    payload = getattr(row, "payload", None) or {}
    issue = str(payload.get("issue") or "")
    if "冲突" in issue:
        return PARSE_EXC_CONFLICT, PARSE_EXCEPTION_RISK_RANK[PARSE_EXC_CONFLICT]
    rule = payload.get("rule") or {}
    missing = bool(payload.get("missing_marker"))
    if missing and payload.get("req_type") == "hard_requirement":
        return PARSE_EXC_HARD_ANCHOR_MISS, PARSE_EXCEPTION_RISK_RANK[PARSE_EXC_HARD_ANCHOR_MISS]
    if rule.get("located_by") == "llm":
        return PARSE_EXC_LLM_PENDING, PARSE_EXCEPTION_RISK_RANK[PARSE_EXC_LLM_PENDING]
    return PARSE_EXC_PROBE_MISS, PARSE_EXCEPTION_RISK_RANK[PARSE_EXC_PROBE_MISS]


def _sync_parse_exceptions(session, *, project_id: str, pending_rows, snapshot_version: str) -> None:
    from sqlalchemy import select as _sa_select
    select = _sa_select
    """把当前待确认候选物化为 parse_exceptions 快照行（docs/12 §3.4）。

    同一快照版本幂等（唯一键 snapshot_version + candidate_id）；处置历史追加在
    decision_history。该表只影响解析质量与满分阻断，不得宣告企业不满足。
    """
    import uuid as _uuid

    from runtime.db.models import ParseException

    for row in pending_rows:
        exists = session.scalar(
            select(ParseException).where(
                ParseException.snapshot_version == snapshot_version,
                ParseException.candidate_id == row.candidate_id,
            ).limit(1)
        )
        if exists is not None:
            continue
        payload = getattr(row, "payload", None) or {}
        exc_type, rank = _parse_exception_type_and_rank(row)
        title = str(payload.get("assertion") or payload.get("value") or payload.get("anchor_key")
                    or row.candidate_id)[:256]
        if payload.get("missing_marker"):
            title = f"未检出：{title}（不代表文件无此要求）"
        elif (payload.get("rule") or {}).get("located_by") == "llm":
            title = f"大模型定位待确认：{title}"
        session.add(ParseException(
            parse_exception_id=f"PX-{_uuid.uuid4().hex[:12]}",
            project_id=project_id,
            material_id=row.material_id,
            material_version=row.version,
            candidate_id=row.candidate_id,
            snapshot_version=snapshot_version,
            exception_type=exc_type,
            risk_rank=rank,
            req_type=payload.get("req_type"),
            status="open",
            title=title,
            payload=payload,
        ))
    session.flush()


def _build_run_context(*, project_id: str, rule_set, as_of: str, materials, identity,
                       evidence_snapshot_hash: str, managers, ledger_ineligible: dict,
                       parse_exception_snapshot_version: str, engine_result, historical_sample: bool) -> dict:
    """docs/12 §3.5：MatchRun 必须固化并由所有投影接口原样返回的运行上下文。"""
    from runtime.core.domain_dict import DICT_VERSION

    identity_status = getattr(identity, "identity_status", None) if identity is not None else None
    identity_fields = []
    if identity is not None:
        for f in ("project_name_announcement", "project_name_tender", "tenderee", "tender_no",
                  "lot", "budget", "deadline"):
            identity_fields.append(str(getattr(identity, f, None) or ""))
    doc_set = sorted(
        (f"{m.material_id}:v{m.version}:{(m.content_hash or '')[:12]}"
         for m in (materials or []) if getattr(m, "project_id", None) == project_id)
    )
    personnel_availability = sorted(
        (f"{getattr(m, 'manager_id', '')}:{len(getattr(m, 'active_projects', None) or [])}"
         f":{getattr(m, 'availability', None) or ''}"
         for m in (managers or []))
    )
    from scripts.matching.engine import MATCHER_VERSION

    return {
        "project_id": project_id,
        "project_identity_version": f"{identity_status or 'unverified'}@{_hash8(identity_fields)}",
        "document_set_version": _hash8(doc_set),
        "ruleset_version": getattr(rule_set, "version", None),
        "parse_exception_snapshot_version": parse_exception_snapshot_version,
        "enterprise_snapshot_version": evidence_snapshot_hash,
        "personnel_availability_snapshot_version": _hash8([personnel_availability, _hash8(ledger_ineligible)]),
        "candidate_plan_snapshot_version": None,  # 引擎执行后回填
        "retrieval_run_id": None,  # 检索执行后回填
        "as_of": as_of,
        "matcher_version": f"{MATCHER_VERSION}@{DICT_VERSION}",
        "dict_version": DICT_VERSION,
        "historical_sample": historical_sample,
    }


def _run_input_fingerprint(run_context: dict, *, lot_id) -> str:
    """docs/12 §4.3-5 幂等键：项目、标段、文件集合、规则、企业/人员快照、解析例外快照、
    授权评分输入及引擎版本（检索运行与 run_id 不入键——它们是同一输入的派生）。"""
    keys = ("project_id", "project_identity_version", "document_set_version", "ruleset_version",
            "parse_exception_snapshot_version", "enterprise_snapshot_version",
            "personnel_availability_snapshot_version", "candidate_plan_snapshot_version",
            "as_of", "matcher_version", "historical_sample")
    payload = {k: run_context.get(k) for k in keys}
    payload["lot_id"] = lot_id
    return _hash8(payload)


def _candidate_plan_snapshot_version(engine_result) -> str | None:
    plans = None
    if isinstance(engine_result, dict):
        plans = engine_result.get("candidate_plans")
    elif isinstance(engine_result, list):
        plans = engine_result
    if not plans:
        return "none"
    return _hash8([[p.get("plan_ref"), p.get("status"), p.get("primary")] for p in plans])


def _persist_candidate_plans(session, *, run, engine_result: dict) -> None:
    from sqlalchemy import select as _sa_select
    select = _sa_select
    """候选班子三表落库（docs/12 §3.3）。方案 id = <run_id>:<plan_ref>，跨 run 不复用。"""
    import uuid as _uuid

    from runtime.db.models import CandidatePlan, CandidatePlanMember, CandidatePlanRequirementLink

    for plan in (engine_result.get("candidate_plans") or []):
        plan_id = f"{run.run_id}:{plan.get('plan_ref')}"
        session.add(CandidatePlan(
            candidate_plan_id=plan_id,
            run_id=run.run_id,
            project_id=run.project_id,
            lot_id=run.lot_id,
            role_code=plan.get("role_code") or "project_manager",
            status="proposed",
            is_primary=bool(plan.get("primary")),
            selection_basis=plan.get("selection_basis"),
            gap_reason_codes=plan.get("gap_reason_codes") or [],
        ))
        for member in plan.get("members") or []:
            session.add(CandidatePlanMember(
                candidate_plan_id=plan_id,
                role_code=member.get("role_code") or plan.get("role_code") or "project_manager",
                person_id=member.get("person_id") or "",
                person_kind=member.get("person_kind") or "manager",
                is_primary=bool(member.get("is_primary", True)),
                availability_as_of=member.get("availability_as_of") or run.as_of,
                evidence_refs=member.get("evidence_refs") or [],
            ))
        for rid, res in (plan.get("results") or {}).items():
            session.add(CandidatePlanRequirementLink(
                candidate_plan_id=plan_id,
                requirement_id=rid,
                person_id=(plan.get("members") or [{}])[0].get("person_id"),
                result=res.get("result"),
                reason_code=res.get("reason_code"),
            ))
    session.flush()


def _sync_operation_tasks(session, *, project_id: str, run, requirements, engine_result: dict) -> None:
    from sqlalchemy import select as _sa_select
    select = _sa_select
    """动作要求 → operation_tasks（docs/12 §2.4/§3.4）。

    任务 id 确定性（project + requirement），跨运行 upsert：状态以引擎判定回写，
    保留已登记回执；动作只影响执行就绪与 approval_ready 阶段门禁。
    """
    import hashlib as _hashlib

    from runtime.db.models import OperationTask

    engine_by_req = {e.get("requirement_id"): e for e in engine_result.get("matrix") or []}
    # 2026-09-29 用户裁定：保证金等「投标时才发生」的条款转入执行计划（bond/submission_ready），
    # 判定阶段不作为缺证阻断（docs/12 §2.4 task_kind=bond）。
    def _execution_rows():
        for req in requirements or []:
            entry = engine_by_req.get(req.requirement_id) or {}
            rule = req.rule if isinstance(req.rule, dict) else {}
            if req.req_type == "action_requirement":
                yield req, entry, (entry.get("task_kind") or rule.get("task_kind") or "other"), \
                    (req.required_by_stage or "approval_ready")
            elif req.req_type == "hard_requirement" and entry.get("task_kind") == "bond":
                amount = rule.get("amount")
                forms = "/".join(rule.get("forms") or [])
                title = "投标保证金" + (f" {amount} 元" if amount is not None else "")
                title += f"（{forms}）" if forms else ""
                title += "——递交投标文件前办理"
                yield req, entry, "bond", "submission_ready"
    for req, entry, task_kind, stage in _execution_rows():
        if task_kind == "bond" and entry.get("task_kind") == "bond":
            rule = req.rule if isinstance(req.rule, dict) else {}
            amount = rule.get("amount")
            forms = "/".join(rule.get("forms") or [])
            title = "投标保证金" + (f" {amount} 元" if amount is not None else "")
            title += f"（{forms}）" if forms else ""
            title += "——递交投标文件前办理"
        else:
            title = (req.assertion or req.requirement_id)[:256]
        # 2026-09-29：按内容去重——重确认快照链（-r2 等）产生新 requirement_id 但同 assertion，
        # 执行任务以 (project, normalized_assertion) 为确定性键，同内容只留一条
        dedup_key = (req.assertion or req.requirement_id).strip()[:200]
        task_id = "OT-" + _hashlib.sha256(f"{project_id}|{dedup_key}".encode()).hexdigest()[:16]
        existing = session.get(OperationTask, task_id)
        status = entry.get("match_result") or req.action_status or "not_started"
        if status == "not_applicable":
            status = "not_started"  # 执行任务以动作状态表达；not_applicable 是判定阶段口径
        fields = dict(
            run_id=run.run_id if run is not None else None,
            lot_id=req.lot_id,
            task_kind=task_kind,
            required_by_stage=stage,
            title=title[:256],
            clause_ref=req.clause_ref,
            deadline_at=req.deadline_at,
            status=status,
        )
        if existing is None:
            session.add(OperationTask(operation_task_id=task_id, project_id=project_id,
                                      action_requirement_id=req.requirement_id,
                                      owner_role="bid_specialist", **fields))
        else:
            for k, v in fields.items():
                setattr(existing, k, v)
            existing.version = (existing.version or 1) + 1
    session.flush()


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
    # ADR-004 §2.6（P0-01）：进程级心跳由独立守护线程写入（与任务执行解耦——长任务期间主循环
    # 不回到轮询点，若在主循环写心跳会被 /readyz 误判 stale）；心跳失败只记日志不中断任务。
    hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(SessionLocal, stop_event, poll_interval),
        name="worker-heartbeat", daemon=True,
    )
    hb_thread.start()
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


def _heartbeat_loop(session_factory, stop_event: threading.Event, poll_interval: float) -> None:
    """守护线程：每 heartbeat 间隔 upsert worker_heartbeats（阈值 WORKER_HEARTBEAT_STALE_SECONDS 的 1/3 内）。"""
    import socket

    from runtime.core.config import worker_heartbeat_stale_seconds

    hostname = socket.gethostname()[:128]
    interval = max(1.0, min(float(poll_interval), worker_heartbeat_stale_seconds() / 3.0))
    while not stop_event.is_set():
        session = session_factory()
        try:
            record_heartbeat(session, RUNNER_ID, poll_interval_seconds=poll_interval,
                             pid=os.getpid(), hostname=hostname)
        except Exception as exc:
            try:
                session.rollback()
            except Exception:
                pass
            logger.warning("worker 心跳写入失败（/readyz 将报告 worker 不可用）: %s: %s",
                           type(exc).__name__, exc)
        finally:
            session.close()
        stop_event.wait(interval)


def main() -> None:
    logging.config.dictConfig(logging_config())
    run_forever()


if __name__ == "__main__":
    main()
