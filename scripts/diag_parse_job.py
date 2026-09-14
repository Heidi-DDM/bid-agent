# -*- coding: utf-8 -*-
"""只读诊断：复现 worker 对 parse.tender_document(MAT-TEST-001) 的执行路径，不写库。

用法（在项目根目录）：
    .venv/bin/python scripts/diag_parse_job.py

输出分段：
    0) 环境快照（env vs .env 解析值，暴露 OBJECT_STORE_ROOT 占位符问题）
    1) analysis_jobs 相关任务行（status/attempts/error_code/heartbeat）
    2) materials / material_versions / parse_candidates / audit_events 现状
    3) 复现 worker 执行：原文检查 -> route_document -> extract_rule_candidates
       -> extract_main_card，逐段计时（每段 120s 护栏，hang 会直接指出位置）
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

logging.disable(logging.CRITICAL)  # 避免第三方库噪音刷屏

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from runtime.core.config import database_url, object_store_root  # noqa: E402
from runtime.db.models import (  # noqa: E402
    AnalysisJob,
    AuditEvent,
    Material,
    MaterialVersion,
    ParseCandidate,
)

_MATERIAL_ID = "MAT-TEST-001"

_ALARM_HANDLER_STATE = {"hit": None}


def _alarm_handler(signum, frame):  # noqa: ARG001
    raise TimeoutError("段执行超时（120s 护栏触发）")


def timed_section(label: str, fn, *, guard_seconds: int = 120):
    """执行并计时；超时抛出并记录卡点位置。"""
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(guard_seconds)
    t0 = time.monotonic()
    try:
        result = fn()
        print(f"   {label}: 完成，耗时 {(time.monotonic() - t0):.1f}s")
        return result
    finally:
        signal.alarm(0)


def main() -> None:
    print("=" * 74)
    print("0) 环境快照")
    raw_root = os.environ.get("OBJECT_STORE_ROOT")
    print(f"   shell/进程 env OBJECT_STORE_ROOT = {raw_root!r}")
    resolved = object_store_root()
    print(f"   config.object_store_root()       = {resolved!r}")
    if resolved == "/absolute/path/to/bid_agent_objects" or not Path(resolved).is_dir():
        print("   !! 警告：解析出的 OBJECT_STORE_ROOT 是 .env 模板占位符或不存在。")
        print("      若 worker/API 未用环境变量覆盖，会出现『原文缺失』——但注意这会在")
        print("      claim 后立刻抛错(RuntimeError)，不会表现为 heartbeat_timeout。")
    print(f"   DATABASE_URL = {database_url()}")

    eng = create_engine(database_url())
    with Session(eng) as s:
        print("=" * 74)
        print("1) analysis_jobs（parse.tender_document / MAT-TEST-001）")
        jobs = s.execute(
            select(AnalysisJob)
            .where(
                (AnalysisJob.kind == "parse.tender_document")
                | (AnalysisJob.input_ref == _MATERIAL_ID)
            )
            .order_by(AnalysisJob.created_at)
        ).scalars().all()
        if not jobs:
            print("   （无相关任务行）")
        for j in jobs:
            msg = (j.error_message or "")[:160].replace("\n", " ")
            print(f"   job_id={j.job_id} kind={j.kind} status={j.status} "
                  f"attempts={j.attempts}/{j.max_attempts} input_ref={j.input_ref} "
                  f"project_id={j.project_id}")
            print(f"     error_code={j.error_code} runner_id={j.runner_id} "
                  f"heartbeat_at={j.heartbeat_at} updated_at={j.updated_at}")
            if msg:
                print(f"     error_message={msg}")

        print("=" * 74)
        print("2) 材料与解析现状")
        material = s.scalar(
            select(Material)
            .where(Material.material_id == _MATERIAL_ID)
            .order_by(Material.version.desc())
            .limit(1)
        )
        if material is None:
            print(f"   !! materials 中不存在 {_MATERIAL_ID} —— worker 会在 claim 后立即抛错")
            s.close()
            return
        mv = s.get(MaterialVersion, (material.material_id, material.version))
        print(f"   material        : id={material.material_id} version={material.version} "
              f"type={material.material_type} parse_status={material.parse_status} "
              f"status={material.status}")
        if material.project_id:
            print(f"     project_id={material.project_id} "
                  f"content_hash={material.content_hash[:16]}…")
        if mv is None:
            print("   !! material_versions 缺不可变记录 —— worker 会在 claim 后立即抛错")
            s.close()
            return
        obj_path = Path(object_store_root()) / mv.object_uri
        print(f"   material_version: object_uri={mv.object_uri}")
        print(f"     解析出的对象文件 = {obj_path}")
        print(f"     存在={obj_path.exists()} 大小="
              f"{obj_path.stat().st_size if obj_path.exists() else 'N/A'}")
        if not obj_path.is_file():
            print("   !! 原文缺失：worker 会在 claim 后立即抛 RuntimeError(原文缺失)")
            s.close()
            return

        counts = s.execute(
            select(ParseCandidate.kind, ParseCandidate.status, func.count())
            .where(ParseCandidate.material_id == _MATERIAL_ID)
            .group_by(ParseCandidate.kind, ParseCandidate.status)
        ).all()
        if counts:
            for kind, status, n in counts:
                print(f"   parse_candidates: kind={kind} status={status} n={n}")
        else:
            print("   parse_candidates: （无候选行 —— worker 从未走到 store_candidates）")

        print("   audit_events（最近 12 条）:")
        events = s.execute(
            select(AuditEvent)
            .where(AuditEvent.object_ref == (material.project_id or _MATERIAL_ID))
            .order_by(AuditEvent.created_at.desc())
            .limit(12)
        ).scalars().all()
        if not events:
            events = s.execute(
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(12)
            ).scalars().all()
        for ev in events:
            print(f"     {ev.created_at} {ev.actor} {ev.action} | "
                  f"{ev.basis or ''} | {ev.outcome or ''}")

        print("=" * 74)
        print("3) 复现 worker 执行路径（只读：route + extract，不写库）")
        from runtime.parsing.extractor import extract_main_card, extract_rule_candidates
        from runtime.parsing.router import route_document

        def _do_route():
            return route_document(str(obj_path))

        route = timed_section("route_document", _do_route)
        print(f"   route 结果: kind={route.kind} pages={len(route.pages)} "
              f"confidence={route.confidence} error={route.error}")
        if route.kind in ("unsupported", "error") or not route.pages:
            print("   !! 路由失败 —— 与 worker 的 RuntimeError 分支一致，但不应表现为 "
                  "heartbeat_timeout")
            return

        def _do_rule_extract():
            return extract_rule_candidates(
                route.pages,
                project_id=material.project_id or "",
                material_id=material.material_id,
                content_hash=material.content_hash,
            )

        rule_cands = timed_section("extract_rule_candidates", _do_rule_extract)

        def _do_field_extract():
            return extract_main_card(route.pages)

        field_cands = timed_section("extract_main_card", _do_field_extract)

        print(f"   产出: rule_candidates={len(rule_cands)} "
              f"main_card_candidates={len(field_cands)}")
        print()
        print("=> 若以上全部秒级完成：解析链路本身无性能问题，问题在 worker 进程侧")
        print("   （DB 写路径锁等待 / 进程环境差异），下一步需在前台盯 worker 日志。")
        print("=> 若某段 120s 超时：复现了 hang，卡点即超时段。")
    eng.dispose()


if __name__ == "__main__":
    try:
        main()
    except TimeoutError:
        print("!! 诊断中断：某段执行超过 120s 护栏（复现了 worker 卡住的位置）")
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001
        print(f"!! 诊断脚本异常 {type(exc).__name__}: {exc}")
        sys.exit(1)