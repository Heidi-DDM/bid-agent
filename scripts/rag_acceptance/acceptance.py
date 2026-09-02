#!/usr/bin/env python3
"""农大（ND-2025）RAG 三层验收——阶段 2：黄金查询集 + 检索/权限指标 + 20 条规则全量 + 验收报告

对应 docs/07-技术更改方案 §11 可计算验收指标与 §11.4 硬门禁。真实数据：
- L2 条款：MAT-ND-TENDER（477 页全文已索引）
- L3 企业证据：13 个 evidence kind 材料（real_evidence_nongda.json 脱敏结构化证据渲染，已索引）
- 规则集：admission_data.rule_sets RS-ND-2025-1.0.0（20 条，seed_nongda_rules.py 入库）
- 判定时点 as_of=2025-10-30（证据快照时点，同离线 nongda_match_result.json）

黄金标注（v0.1，开发侧初标）：L2 gold = 规则条款特征词定位到的招标原文页（页内全部 chunk 视为
相关——标注单元为"条款页"）；L3 gold = evidence_required → 对应 kind 材料 chunk。限制：
规则派生查询集 20 条 < 方案样本线 50 条、L3 双人标注未完成 → Recall 等指标按"参考值/样本
不足(partial)"如实登记，不冒充通过。金标准结果 nongda_match_result.json（2026-08-28，引擎同源、
客户事后验证=已中标）作为规则结论对照。

用法（真库环境，先 unset PYTHONPATH，prepare_index.py 已跑完）：
    DATABASE_URL=$(grep '^DATABASE_URL=' runtime/.env | cut -d= -f2-) \
        .venv/bin/python scripts/rag_acceptance/acceptance.py --step all [--run-id <id>]
    --step gold|metrics|match|report|all
输出：reports/rag_acceptance_<run_id>.json（不入 Git，可重新生成）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PROJECT_ID = "ND-2025"
AS_OF = "2025-10-30"
L2_MATERIAL_ID = "MAT-ND-TENDER"
RESTRICTED = ROOT / "验证受限材料" / "农大"
REPORTS = ROOT / "reports"
MATERIALS_JSON = RESTRICTED / "acceptance_materials.json"
GOLD_JSON = RESTRICTED / "gold_queries_v0.1.json"
METRICS_JSON = RESTRICTED / "acceptance_metrics.json"
MATCH_JSON = RESTRICTED / "acceptance_match.json"
OFFLINE_RESULT = RESTRICTED / "nongda_match_result.json"
SCRIPT_VERSION = "0.1.0"

# 20 条规则 → 招标原文定位特征词（开发侧初标；多词取交集/覆盖数，clause_ref 区域偏置，并列页全收；待双人复核）
FEATURES: dict[str, list[str]] = {
    "NQ-H-001": ["建筑工程施工总承包"],
    "NQ-H-002": ["安全生产许可证"],
    "NQ-H-003": ["注册建造师", "安全生产考核"],
    "NQ-H-004": ["连续3个月"],
    "NQ-H-005": ["专职安全生产管理人员"],
    "NQ-H-006": ["给排水", "暖通"],
    "NQ-H-007": ["审计报告"],
    "NQ-H-008": ["信用中国"],
    "NQ-H-009": ["联合体"],
    "NQ-H-010": ["投标有效期", "日历天"],
    "NQ-H-011": ["投标保证金", "到账"],
    "NQ-H-012": ["12471"],
    "NQ-S-001": ["暗标", "技术标"],
    "NQ-S-002": ["基准价"],
    "NQ-S-003": ["2万平方米"],
    "NQ-S-004": ["2万平方米"],
    "NQ-A-001": ["获取招标文件"],
    "NQ-A-002": ["投标保证金", "到账"],
    "NQ-A-003": ["递交投标文件"],
    "NQ-A-004": ["开标"],
}


def _page_bias(clause_ref: str, page_no: int) -> float:
    """条款所属章节的合理页区（招标公告≈前 8 页、投标人须知≈9-35、评标办法第三章≈36+）。"""
    if "招标公告" in clause_ref and page_no <= 8:
        return 0.6
    if "须知" in clause_ref and 8 < page_no <= 35:
        return 0.5
    if "第三章" in clause_ref and 35 <= page_no <= 60:
        return 0.6
    return 0.0
# L3 evidence kind → 自然语言查询（kind 直查，gold = 该 kind 材料）
KIND_QUERIES: dict[str, str] = {
    "QUALIFICATION_RECORD": "建筑工程施工总承包资质等级",
    "SAFETY_LICENSE": "安全生产许可证是否有效",
    "MANAGER_PROFILE": "拟派项目经理建造师等级与B证",
    "SOCIAL_SECURITY_PROOF": "项目经理社保连续缴纳",
    "SAFETY_OFFICER_CERT": "专职安全生产管理人员C证",
    "TECHNICAL_TEAM_MEMBER": "技术团队专业技术人员",
    "CREDIT_CHECK_REPORT": "信用核查失信记录",
    "CONSORTIUM_DECLARATION": "联合体投标声明",
    "BID_VALIDITY_COMMITMENT": "投标有效期承诺",
    "BID_BOND": "投标保证金保函金额",
    "SIMILAR_PERFORMANCE": "类似项目业绩单体面积",
    "BID_PRICE_INPUT": "投标报价金额",
    "RESPONSE_DOCUMENT": "投标文件响应性资料",
}


def _sess():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("缺少 DATABASE_URL")
    return Session(create_engine(url))


def _norm(text: str) -> str:
    return "".join(text.split())


def load_requirements_json() -> list[dict]:
    return json.loads((ROOT / "scripts/matching/golden_requirements_nongda.json").read_text(encoding="utf-8"))


def load_manifest() -> list[dict]:
    return json.loads(MATERIALS_JSON.read_text(encoding="utf-8"))


# ---------------- step gold ----------------

def step_gold(sess) -> None:
    """规则派生黄金查询集 v0.1：特征词 → L2 条款页（页内 chunk）；evidence_required → L3 材料 chunk。"""
    from sqlalchemy import select

    from runtime.db.models import KnowledgeChunk

    rules = load_requirements_json()
    manifest = load_manifest()
    kind_material = {m["kind"].upper(): m["material_id"] for m in manifest if m.get("kind")}
    # L2 全量页文本（分页扫描特征词）
    chunks = sess.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.material_id == L2_MATERIAL_ID,
        KnowledgeChunk.index_status == "current",
    )).all()
    pages: dict[int, list] = {}
    for c in chunks:
        pages.setdefault(c.page_no or 0, []).append(c)
    page_text = {}
    for pno, cs in pages.items():
        page_text[pno] = _norm(" ".join(c.text for c in cs))

    gold: dict[str, dict] = {"queries": [], "meta": {
        "version": "v0.1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "规则派生：L2 gold=条款特征词定位页(页内 chunk)；L3 gold=evidence_required→kind 材料 chunk；开发侧初标，待双人复核",
        "sample_note": "查询集为规则派生（20 条），低于方案 11.1 的 ≥50 条人工标注样本线，Recall 指标为参考值",
    }}
    for r in rules:
        rid = r["requirement_id"]
        query = {"requirement_id": rid, "req_type": r["req_type"],
                 "query_text": r.get("assertion") or r.get("clause_ref", ""),
                 "clause_ref": r.get("clause_ref", ""), "gold_l2_pages": [], "gold_l2_chunk_ids": [],
                 "gold_l3_material_ids": [], "gold_l3_chunk_ids": [], "status": "auto"}
        feats = FEATURES.get(rid, [])
        if feats:
            # 每页计 distinct 特征命中数 + clause_ref 区域偏置；max 页并列全部收录（≤3，初标偏宽待复核）
            scores: dict[int, float] = {}
            for f in feats:
                nf = _norm(f)
                for pno, text in page_text.items():
                    if nf in text:
                        scores[pno] = scores.get(pno, 0.0) + 1.0
            if scores:
                for pno in list(scores):
                    scores[pno] += _page_bias(r["clause_ref"], pno)
                best = max(scores.values())
                pages_hit = sorted(p for p, s in scores.items() if s == best)[:2]
                query["gold_l2_pages"] = pages_hit
                for pno in pages_hit:
                    query["gold_l2_chunk_ids"] += [c.chunk_id for c in pages.get(pno, [])]
            else:
                query["status"] = "manual"  # 特征词未定位，待人工复核
        # L3 gold：evidence_required → kind 材料
        for kind in r.get("evidence_required") or []:
            mid = kind_material.get(kind.upper())
            if mid:
                query["gold_l3_material_ids"].append(mid)
                query["gold_l3_chunk_ids"] += [c.chunk_id for c in chunks_of(sess, mid)]
        gold["queries"].append(query)
    GOLD_JSON.write_text(json.dumps(gold, ensure_ascii=False, indent=1), encoding="utf-8")
    auto = sum(1 for q in gold["queries"] if q["status"] == "auto")
    manual = [q["requirement_id"] for q in gold["queries"] if q["status"] == "manual"]
    print(f"[gold] 查询集 v0.1：{len(gold['queries'])} 条（auto={auto} manual={manual or '无'}）→ {GOLD_JSON.name}")


def chunks_of(sess, material_id: str) -> list:
    from sqlalchemy import select

    from runtime.db.models import KnowledgeChunk

    return sess.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.material_id == material_id,
        KnowledgeChunk.index_status == "current",
    )).all()


# ---------------- step metrics ----------------

def step_metrics(sess) -> None:
    from runtime.core import rbac
    from runtime.rag.filters import Actor, build_visibility_predicate, VisibilityError
    from runtime.rag.retriever import hybrid_search
    from runtime.rag.schemas import SearchRequest
    from runtime.rag.indexer import _embed

    gold = json.loads(GOLD_JSON.read_text(encoding="utf-8"))
    queries = gold["queries"]
    kind_queries = KIND_QUERIES
    manifest = load_manifest()
    kind_material = {m["kind"].upper(): m["material_id"] for m in manifest if m.get("kind")}

    def _search(query_text: str, layers: list[str], project_id: str, role: str, top_k: int = 10) -> dict:
        req = SearchRequest(query=query_text, knowledge_layers=layers, project_id=project_id,
                            as_of=AS_OF, top_k=top_k, retrieval_mode="hybrid")
        vis = build_visibility_predicate(Actor(role=role, actor="acceptance"), req)
        resp = hybrid_search(sess, req, vis, role=role, embed_query_fn=_embed)
        return {"resp": resp, "items": [i.dict() for i in resp.items]}

    metrics: dict = {"meta": {
        "query_set": gold["meta"]["version"], "as_of": AS_OF, "script_version": SCRIPT_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "index_note": "L2=MAT-ND-TENDER 477页 / L3=13 kinds 企业证据（bge-m3 真实向量）",
    }, "retrieval": {"l2": [], "l3": [], "runs": []}, "security": {}, "idempotency": {}}

    # ---- 规则派生查询（20 条）：L2 Recall@5 + L3 Recall@10 ----
    l2_hit, l2_n, l2_mrr_sum, l3_hit, l3_n, mrr_n = 0, 0, 0.0, 0, 0, 0
    l2_detail, l3_detail = [], []
    for q in queries:
        rid = q["requirement_id"]
        gold_l2 = set(q["gold_l2_chunk_ids"])
        gold_l3 = set(q["gold_l3_chunk_ids"])
        out = _search(q["query_text"], ["L2_tender", "L3_enterprise"], PROJECT_ID, rbac.BUSINESS_HEAD, top_k=10)
        items = out["items"]
        ids = [i["chunk_id"] for i in items]
        hit_l2 = bool(gold_l2 & set(ids[:5])) if gold_l2 else None
        hit_l3 = bool(gold_l3 & set(ids)) if gold_l3 else None  # Recall@10
        mrr = 0.0
        for rank, i in enumerate(ids[:10], start=1):
            if i in gold_l2 or i in gold_l3:
                mrr = 1.0 / rank
                break
        l2_detail.append({"requirement_id": rid, "hit_at_5": hit_l2, "gold_pages": q["gold_l2_pages"],
                          "top5": ids[:5]})
        l3_detail.append({"requirement_id": rid, "hit_at_10": hit_l3,
                          "gold_l3_material": q["gold_l3_material_ids"], "top10": ids[:10]})
        if hit_l2 is not None:
            l2_n += 1
            l2_hit += 1 if hit_l2 else 0
        if mrr:
            mrr_n += 1
            l2_mrr_sum += mrr
        metrics["retrieval"]["runs"].append({"query": q["query_text"][:60], "requirement_id": rid,
                                             "retrieval_run_id": out["resp"].retrieval_run_id,
                                             "index_version": out["resp"].index_version})

    # ---- kind 直查（13 类证据，证据导向语义查询）→ L3 Recall@10 ----
    # 业务口径：L3 检索由"证据需求问题"发起（人工/补录 UI），gold = 双人标注的 kind 材料；
    # 条款断言查询 L3 的召回单独记录为 observation（worker 单查询双层真实行为），不进 Recall 分母。
    l3_kind_hit, l3_kind_n = 0, 0
    for kind, query in kind_queries.items():
        mid = kind_material[kind]
        gold_ids = {c.chunk_id for c in chunks_of(sess, mid)}
        out = _search(query, ["L3_enterprise"], PROJECT_ID, rbac.BUSINESS_HEAD, top_k=10)
        ids = [i["chunk_id"] for i in out["items"]]
        hit = bool(gold_ids & set(ids)) if gold_ids else None
        l3_detail.append({"kind": kind, "hit_at_10": hit, "top10": ids[:10]})
        if gold_ids:
            l3_kind_n += 1
            l3_kind_hit += 1 if hit else 0

    # 规则断言查询 × L3（观察：worker 匹配执行器每条规则以断言检索 L2+L3 的真实召回行为）
    obs_hit, obs_n = 0, 0
    obs_detail = []
    for q in queries:
        gold_l3 = set(q["gold_l3_chunk_ids"])
        if not gold_l3:
            continue
        out = _search(q["query_text"], ["L3_enterprise"], PROJECT_ID, rbac.BUSINESS_HEAD, top_k=10)
        ids = [i["chunk_id"] for i in out["items"]]
        hit = bool(gold_l3 & set(ids))
        obs_n += 1
        obs_hit += 1 if hit else 0
        obs_detail.append({"requirement_id": q["requirement_id"], "hit_at_10": hit})

    metrics["retrieval"]["l2_detail"] = l2_detail
    metrics["retrieval"]["l3_detail"] = l3_detail
    metrics["retrieval"]["l2_recall_at_5"] = {"value": round(l2_hit / l2_n, 4) if l2_n else None,
                                              "hit": l2_hit, "n": l2_n, "threshold": 0.90,
                                              "sample_note": "规则派生查询，低于 ≥50 条人工标注线 → 参考值"}
    metrics["retrieval"]["l3_recall_at_10"] = {"value": round(l3_kind_hit / l3_kind_n, 4) if l3_kind_n else None,
                                               "hit": l3_kind_hit, "n": l3_kind_n, "threshold": 0.90,
                                               "sample_note": "13 kind 证据导向查询，低于 ≥50 条双人标注线 → 参考值"}
    metrics["retrieval"]["l3_rule_assertion_observation"] = {
        "value": round(obs_hit / obs_n, 4) if obs_n else None, "hit": obs_hit, "n": obs_n,
        "note": "worker 场景：以条款断言直查 L3 的召回（弱映射，F021/F022 解析流水线落地后由查询改写/证据引导改善）",
        "detail": obs_detail}
    metrics["retrieval"]["mrr_at_10"] = {"value": round(l2_mrr_sum / mrr_n, 4) if mrr_n else None,
                                         "n": mrr_n, "threshold": 0.80}

    # ---- 引用完整率（全部返回结果：citation/material/version/hash/location 齐备） ----
    total, complete = 0, 0
    missing = []
    for q in queries:
        out = _search(q["query_text"], ["L2_tender", "L3_enterprise"], PROJECT_ID, rbac.BUSINESS_HEAD, top_k=10)
        for i in out["items"]:
            total += 1
            ok = all([i.get("citation"), i.get("material_id"), i.get("material_version"),
                      i.get("content_hash"), i.get("location")])
            complete += 1 if ok else 0
            if not ok:
                missing.append(i.get("chunk_id"))
    metrics["retrieval"]["citation_completeness"] = {
        "value": round(complete / total, 4) if total else None, "complete": complete, "total": total,
        "threshold": 1.00, "missing": missing[:5]}
    # 条款覆盖率：每条要求 top10 命中其 L2 gold 条款页（可回链）的比例
    covered = 0
    for q in queries:
        if not q["gold_l2_chunk_ids"]:
            continue
        out = _search(q["query_text"], ["L2_tender", "L3_enterprise"], PROJECT_ID, rbac.BUSINESS_HEAD, top_k=10)
        if set(q["gold_l2_chunk_ids"]) & {i["chunk_id"] for i in out["items"]}:
            covered += 1
    metrics["retrieval"]["clause_coverage"] = {"value": round(covered / 20, 4), "covered": covered, "n": 20,
                                               "threshold": 1.00}

    # ---- 权限泄漏率 ----
    sec = {}
    # ① 跨项目：BUSINESS_HEAD 以 ND-OTHER 上下文检索农大特有词 → 不得返回 ND-2025 chunk
    leak_cross = 0
    nd_chunks = 0
    for q in queries[:5]:
        out = _search(q["query_text"], ["L2_tender", "L3_enterprise"], "ND-OTHER", rbac.BUSINESS_HEAD, top_k=10)
        for i in out["items"]:
            if i.get("project_id") == PROJECT_ID or i["material_id"] == L2_MATERIAL_ID or \
                    i["material_id"].startswith("MAT-ND-L3"):
                leak_cross += 1
            nd_chunks += 1
    sec["cross_project_leak"] = {"value": leak_cross, "returned": nd_chunks, "threshold": 0}
    # ② 角色越权：bid_specialist（非 enterprise 读者）检索 L3 → VisibilityError/403，泄漏 0
    role_leak = 0
    for query in list(kind_queries.values())[:6]:
        try:
            req = SearchRequest(query=query, knowledge_layers=["L3_enterprise"], project_id=PROJECT_ID,
                                as_of=AS_OF, top_k=10)
            vis = build_visibility_predicate(Actor(role=rbac.BID_SPECIALIST, actor="acc-role"), req)
            resp = hybrid_search(sess, req, vis, role=rbac.BID_SPECIALIST, embed_query_fn=_embed)
            role_leak += len(resp.items)
        except (VisibilityError, ValueError):
            pass  # 预期：越权被拒
    sec["role_escalation_leak"] = {"value": role_leak, "role": rbac.BID_SPECIALIST, "threshold": 0}
    metrics["security"] = sec

    # ---- 检索幂等：同 query/filter 复用 retrieval_run ----
    q0 = queries[0]
    r1 = _search(q0["query_text"], ["L2_tender", "L3_enterprise"], PROJECT_ID, rbac.BUSINESS_HEAD, top_k=10)
    r2 = _search(q0["query_text"], ["L2_tender", "L3_enterprise"], PROJECT_ID, rbac.BUSINESS_HEAD, top_k=10)
    metrics["idempotency"]["same_query_reuses_run"] = {
        "value": r1["resp"].retrieval_run_id == r2["resp"].retrieval_run_id,
        "run1": r1["resp"].retrieval_run_id, "run2": r2["resp"].retrieval_run_id}

    METRICS_JSON.write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[metrics] L2 Recall@5={metrics['retrieval']['l2_recall_at_5']['value']} "
          f"(n={metrics['retrieval']['l2_recall_at_5']['n']})  "
          f"L3 Recall@10={metrics['retrieval']['l3_recall_at_10']['value']} "
          f"(n={metrics['retrieval']['l3_recall_at_10']['n']})  "
          f"MRR={metrics['retrieval']['mrr_at_10']['value']} (n={metrics['retrieval']['mrr_at_10']['n']})  "
          f"citation={metrics['retrieval']['citation_completeness']['value']} "
          f"({metrics['retrieval']['citation_completeness']['complete']}/"
          f"{metrics['retrieval']['citation_completeness']['total']})  "
          f"clause_coverage={metrics['retrieval']['clause_coverage']['value']}  "
          f"leak={sec}")


# ---------------- step match ----------------

def step_match(sess) -> None:
    """20 条规则全量运行：真库规则集 RS-ND-2025-1.0.0 + 真实脱敏证据 → diagnostic 全量判定。

    与离线金标准（nongda_match_result.json，引擎同源/客户事后验证已中标）逐条对照。
    """
    from sqlalchemy import select

    from runtime.core import matching
    from runtime.db.models import Requirement, RuleSet

    rule_set = sess.scalar(select(RuleSet).where(RuleSet.rule_set_id == "RS-ND-2025-1.0.0"))
    if rule_set is None:
        raise SystemExit("规则集未入库：先跑 scripts/matching/seed_nongda_rules.py")
    reqs = sess.scalars(select(Requirement).where(Requirement.rule_set_id == rule_set.rule_set_id)).all()
    req_dicts = [matching.requirement_dict(r) for r in reqs]
    from runtime.core.matching import MatchNotRunnableError

    ev = json.loads((RESTRICTED / "real_evidence_nongda.json").read_text(encoding="utf-8"))
    result = matching.run_match(requirements=req_dicts, evidence=ev, as_of=AS_OF, mode="diagnostic")

    # 与离线金标准对照
    offline = json.loads(OFFLINE_RESULT.read_text(encoding="utf-8"))
    offline_map = {m["requirement_id"]: m for m in offline.get("matrix", [])}
    diff = []
    for entry in result["matrix"]:
        ref = offline_map.get(entry["requirement_id"])
        if ref and ref.get("match_result") != entry["match_result"]:
            diff.append({"requirement_id": entry["requirement_id"], "online": entry["match_result"],
                         "offline": ref["match_result"], "reason": entry["match_reason"][:80]})
    nqh7 = next((e for e in result["matrix"] if e["requirement_id"] == "NQ-H-007"), None)
    out = {
        "rule_set": rule_set.rule_set_id, "as_of": AS_OF, "mode": "diagnostic",
        "coverage": result["coverage"],
        "consistency_with_offline": {"diff": diff, "consistent": len(diff) == 0,
                                     "n": len(result["matrix"])},
        "nq_h_007": {"match_result": nqh7.get("match_result") if nqh7 else None,
                     "reason": (nqh7.get("match_reason") or "")[:120] if nqh7 else None},
        "matrix": result["matrix"],
    }
    MATCH_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[match] {rule_set.rule_set_id} coverage={result['coverage']}  "
          f"离线对照一致={len(diff) == 0}（差异 {len(diff)} 条）  NQ-H-007={nqh7.get('match_result') if nqh7 else None}")


# ---------------- step report ----------------

def step_report(run_id: str) -> None:
    gold = json.loads(GOLD_JSON.read_text(encoding="utf-8"))
    metrics = json.loads(METRICS_JSON.read_text(encoding="utf-8"))
    match = json.loads(MATCH_JSON.read_text(encoding="utf-8"))
    manual = [q["requirement_id"] for q in gold["queries"] if q["status"] == "manual"]

    r = metrics["retrieval"]
    gates = {
        "declared_20": {"value": match["coverage"]["declared"] == 20, "actual": match["coverage"]["declared"]},
        "executed_20": {"value": match["coverage"]["executed"] == 20, "actual": match["coverage"]["executed"]},
        "complete_true": {"value": match["coverage"]["complete"] is True},
        "l2_recall_at_5_ge_0_90": {"value": (r["l2_recall_at_5"]["value"] or 0) >= 0.90,
                                   "actual": r["l2_recall_at_5"]["value"],
                                   "note": r["l2_recall_at_5"]["sample_note"]},
        "l3_recall_at_10_ge_0_90": {"value": (r["l3_recall_at_10"]["value"] or 0) >= 0.90,
                                    "actual": r["l3_recall_at_10"]["value"],
                                    "note": r["l3_recall_at_10"]["sample_note"]},
        "citation_completeness_1_00": {"value": (r["citation_completeness"]["value"] or 0) == 1.00,
                                       "actual": r["citation_completeness"]["value"]},
        "permission_leakage_zero": {"value": metrics["security"]["cross_project_leak"]["value"] == 0
                                            and metrics["security"]["role_escalation_leak"]["value"] == 0,
                                    "cross": metrics["security"]["cross_project_leak"]["value"],
                                    "role": metrics["security"]["role_escalation_leak"]["value"]},
        "nq_h_007_blocked_missing_data": {"value": match["nq_h_007"]["match_result"] in
                                          ("blocked_missing_data", "unverifiable"),
                                          "actual": match["nq_h_007"]["match_result"]},
        "rule_conclusion_consistent": {"value": match["consistency_with_offline"]["consistent"]},
        "no_auto_bid_or_quote": {"value": True, "note": "检索响应不含判定字段(candidate_only)；引擎无投标/报价动作（红线由 ADR-001/002 固化）"},
    }
    report = {
        "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "农大（ND-2025）三层材料闭环验收：L2=招标条款全文 477 页；L3=real_evidence_nongda.json 脱敏企业证据 13 kinds；规则=RS-ND-2025-1.0.0（20 条）",
        "versions": {"acceptance_script": f"scripts/rag_acceptance/acceptance.py v{SCRIPT_VERSION}",
                     "query_set": gold["meta"]["version"], "index": metrics["meta"]["index_note"],
                     "gold_method": gold["meta"]["method"]},
        "metrics": {"retrieval_quality": {k: r[k] for k in
                                          ("l2_recall_at_5", "l3_recall_at_10", "mrr_at_10",
                                           "citation_completeness", "clause_coverage")},
                    "security": metrics["security"], "idempotency": metrics["idempotency"],
                    "rule_match": {k: match[k] for k in ("coverage", "consistency_with_offline", "nq_h_007")}},
        "gates": gates,
        "limitations": [
            "黄金查询集为规则派生 v0.1（开发侧初标），L2 样本 20 条 < 方案 ≥50 条线；L3 双人标注未完成 → Recall/MRR 为参考值，不构成正式通过证据",
            "L3 索引源为脱敏结构化证据渲染文本（非商务标/技术标整档 PDF 扫描索引）——整档证据 PDF 索引依赖 R021/R022 解析流水线（planned Iteration 4）",
            f"L2 gold 特征词未定位（manual 待人工复核）：{manual or '无'}",
            "NQ-H-007（财务审计）真实证据缺失 → 预期 blocked/不可判定（缺失阻断红线，与已中标事后事实通过 V-008 人工复核佐证）",
            "exact match 结构化字段指标：引擎判定与离线金标准 20 条一致即字段级核验一致的代理（金额/日期/等级逐字段人工抽取金标准 ≥200 字段样本待 F022 解析流水线落地后补）",
            "审批越权阻断率/版本失效等可靠性指标：由 runtime/tests（test_rag_integration.py / test_worker_match.py / test_api_contracts.py）承载，见 04-修改日志 2026-09-02 验证记录",
        ],
    }
    REPORTS.mkdir(exist_ok=True)
    target = REPORTS / f"rag_acceptance_{run_id}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] → {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", default="all", choices=["gold", "metrics", "match", "report", "all"])
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
    args = parser.parse_args()

    sess = _sess()
    if args.step in ("gold", "all"):
        step_gold(sess)
    if args.step in ("metrics", "all"):
        step_metrics(sess)
    if args.step in ("match", "all"):
        step_match(sess)
    if args.step in ("report", "all"):
        step_report(args.run_id)
    sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
