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
import logging
import logging.config
import os
import signal
import sys
import threading
import time

from runtime.core import jobs as job_logic
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
        _execute(session, kind, job.input_ref, job.project_id)
        finish_job(session, job_id, outcome="completed")
        logger.info("完成任务 job_id=%s kind=%s", job_id, kind)
        # F020 §2.1：解析成功只触发一次首次匹配（parse.completed 幂等编排）
        if kind == "parse.tender_document":
            _on_parse_completed(session, job.project_id)
    except Exception as exc:
        logger.exception("任务执行异常 job_id=%s kind=%s error=%s", job_id, kind, type(exc).__name__)
        from runtime.db.worker_service import fail_then_retryable

        fail_then_retryable(session, job_id, error_code=type(exc).__name__, error_message=str(exc)[:500])
    return True


def _on_parse_completed(session, project_id: str | None) -> None:
    """解析完成编排：先创建 knowledge_index 任务（方案 §3.5 第 1 步），
    再在满足前置时创建一次 match.run 任务。

    幂等：knowledge_index 幂等键（index:... SHA-256）与 match.run 幂等键
    （kind+input_ref+project_id）保证同一输入不重复建任务；结果页 GET 查询不创建 run（F020 §2.1/§7）。
    """
    if not project_id:
        return
    from runtime.core import orchestration
    from runtime.db import api_service, worker_service
    from runtime.db.models import Material

    from sqlalchemy import select

    materials = session.scalars(
        select(Material).where(Material.project_id == project_id)
    ).all()
    material_dicts = [
        {
            "material_id": m.material_id,
            "material_type": m.material_type,
            "parse_status": m.parse_status,
            "version": m.version,
        }
        for m in materials
    ]
    # 1) 知识索引：解析完成 → 为每个已解析材料创建索引任务（幂等；向量不可用时不伪造成功）
    from runtime.rag import service as rag_service

    index_jobs: list[str] = []
    for material_id, version in orchestration.materials_needing_index(material_dicts):
        job, created = rag_service.create_index_job(
            session, material_id=material_id, version=version, project_id=project_id
        )
        index_jobs.append(job.job_id)
        api_service.audit(session, actor="system", action="knowledge.index_triggered",
                          basis=f"material={material_id}:v{version}",
                          outcome="created" if created else "idempotent_reuse",
                          object_ref=project_id)
    if index_jobs:
        session.commit()
        logger.info("解析完成创建索引任务 project_id=%s jobs=%s", project_id, index_jobs)

    # 2) 首次匹配（既有编排，F020 §2.1）
    run = api_service.latest_match_run(session, project_id)
    existing_runs = 1 if run is not None else 0
    if not orchestration.should_trigger_first_match(material_dicts, existing_runs):
        logger.info("不触发首次匹配 project_id=%s（前置不满足）", project_id)
        return
    job, created = worker_service.create_job(
        session, kind="match.run", input_ref=project_id, project_id=project_id
    )
    api_service.audit(session, actor="system", action="match.auto_trigger",
                      basis=f"parse.completed project_id={project_id}",
                      outcome="created" if created else "idempotent_reuse",
                      object_ref=project_id)
    session.commit()
    logger.info("解析完成自动触发首次匹配 job_id=%s created=%s", job.job_id, created)


def _execute_knowledge_index(session, input_ref: str | None) -> None:
    """knowledge_index 执行器：材料版本 → chunk + embedding（方案 §8.1）。

    解析产物（parsed_pages）由 F021/F022 解析器写入后传入；当前解析器未接入时
    抛 IndexingError → 任务 retryable/manual_review，不伪造成功（F025 §8.1）。
    """
    if not input_ref or ":" not in input_ref:
        raise ValueError("knowledge_index 任务 input_ref 必须为 material_id:version")
    material_id, version = input_ref.rsplit(":", 1)
    from runtime.rag.indexer import IndexingError, index_material

    try:
        result = index_material(session, material_id, int(version))
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


def _execute(session, kind: str, input_ref: str | None, project_id: str | None = None) -> None:
    """任务执行器：knowledge_index 执行真实索引；match.* 执行 RAG→核验→规则引擎链路；
    其余保持占位（R021-R024 接入）。"""
    if kind == "knowledge_index":
        _execute_knowledge_index(session, input_ref)
        return
    if kind == "match.run":
        _execute_match_run(session, project_id, mode="gate")
        return
    if kind == "match.recalculate":
        _execute_match_run(session, project_id, mode="gate")
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
    session.commit()
    logger.info(
        "匹配完成 project_id=%s run=%s coverage=%s retrieval=%s",
        project_id, run.run_id, result["coverage"], run.retrieval_run_id,
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
            fn = retrieve_fn or hybrid_search
            response = fn(session, request, vis, role=rbac.BUSINESS_HEAD)
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
