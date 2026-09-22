# F027：企业资料画像聚合（纯事实统计）
# 按 10 个常见投标评分维度 + 5 个企业内部准入项聚合 qualifications/performances/
# managers/personnel/evidence_files 的计数、字段覆盖与证据回链情况。
# 红线（F027 §1/§4）：只统计事实，不输出资格结论、评分预测或投标建议；
# 待补口径 = 导入哨兵 "__待补__" 或 NULL；空库返回全 0，不编造数字。
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.db.models import (
    EvidenceFile,
    Manager,
    Performance,
    Personnel,
    Qualification,
)

SENTINEL = "__待补__"
PENDING = "pending_verification"

# 四分类口径（F027 §2；对齐 F008 §2 四类结论的输入侧）
CATEGORY_DEFS = [
    {
        "key": "hard",
        "label": "硬性资格/响应要求",
        "note": "满足或不满足（二值）；缺证据即阻断（blocked_missing_data），不得推断满足。"
                "对应匹配引擎 hard_requirement。",
    },
    {
        "key": "objective",
        "label": "客观可计算评分项",
        "note": "报价、业绩数量、人员证书、工期等可按公式复算的评分项；输入不齐时 not_calculable，"
                "不填 0 冒充。对应 scored_requirement（score_nature=objective）。",
    },
    {
        "key": "subjective",
        "label": "主观评审项",
        "note": "技术方案、服务方案、组织措施等；系统不打分，进入人工内部质量评审，"
                "未完成评审则 manual_review。对应 scored_requirement（score_nature=subjective）。",
    },
    {
        "key": "internal",
        "label": "企业内部准入项",
        "note": "资料完整性、资源可用性、项目经理在施状态等企业侧内部条件；不属评标项，"
                "但影响内部满分准入（F008 §6.1）。",
    },
]

_KIND_LABELS = {
    "qualifications": "资质",
    "performances": "业绩",
    "managers": "项目经理",
    "personnel": "人员",
    "evidence_files": "证据文件",
}


def _is_missing(value: Any) -> bool:
    return value is None or value == SENTINEL or value == ""


def _material_ref_count(refs: list | None) -> int:
    """单项证明文件引用数：排除 ledger:（汇总台账行回链）与 cert_no:（编号留痕）。

    台账行回链只指回导入批次的汇总台账文件（如整本 xlsx），不等于该条记录的
    单项证明文件（扫描件/合同/验收单）；画像须区分两级口径（F027 §4）。
    """
    return sum(
        1 for r in (refs or []) if not str(r).startswith(("ledger:", "cert_no:"))
    )


def _bucket_of(category: str | None) -> str:
    """资质按台账类别归桶（F027 §3）；无法归类的进 construction_qual。"""
    c = category or ""
    if "安全生产" in c:
        return "safety_license"
    if "信用" in c:
        return "credit_rating"
    if "CA" in c.upper():
        return "ca_cert"
    if "质量" in c or "ISO" in c.upper():
        return "quality_system"
    return "construction_qual"


def _status_count(rows: Iterable[Any]) -> dict[str, int]:
    return dict(Counter(r.status for r in rows))


def _within_90d(valid_until: date | None, today: date) -> bool:
    return valid_until is not None and today < valid_until <= today + timedelta(days=90)


# ── 各表统计（整表载入后内存计数；台账量级千行级，且避免 PG 专属 JSON 聚合） ──


def _qualification_stats(session: Session, today: date) -> dict:
    rows = session.scalars(select(Qualification)).all()
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = buckets.setdefault(
            _bucket_of(r.category),
            {"total": 0, "by_status": Counter(), "with_evidence": 0,
             "valid_untils": [], "labels": [], "levels": Counter()},
        )
        b["total"] += 1
        b["by_status"][r.status] += 1
        if r.evidence_refs:
            b["with_evidence"] += 1
        if r.valid_until:
            b["valid_untils"].append(r.valid_until.isoformat())
        b["labels"].append(r.category)
        if r.level:
            b["levels"][r.level] += 1
    for b in buckets.values():
        b["by_status"] = dict(b["by_status"])
        b["levels"] = dict(b["levels"])
        b["valid_untils"] = sorted(b["valid_untils"])
    return {
        "total": len(rows),
        "by_status": _status_count(rows),
        "with_evidence": sum(1 for r in rows if r.evidence_refs),
        "with_material_evidence": sum(1 for r in rows if _material_ref_count(r.evidence_refs)),
        "buckets": buckets,
        "expiring_90d": sum(
            1 for r in rows if r.status != "expired" and _within_90d(r.valid_until, today)
        ),
    }


def _performance_stats(session: Session, today: date) -> dict:
    rows = session.scalars(select(Performance)).all()
    five_years_ago = today - timedelta(days=5 * 365)
    types = Counter(r.project_type for r in rows)
    amount_total = sum(float(r.contract_amount) for r in rows if r.contract_amount is not None)
    return {
        "total": len(rows),
        "by_status": _status_count(rows),
        "with_evidence": sum(1 for r in rows if r.evidence_refs),
        "with_material_evidence": sum(1 for r in rows if _material_ref_count(r.evidence_refs)),
        "with_amount": sum(1 for r in rows if r.contract_amount is not None),
        "with_completed": sum(1 for r in rows if r.completed_at is not None),
        "with_full_start_end": sum(
            1 for r in rows if r.awarded_at is not None and r.completed_at is not None
        ),
        "completed_within_5y": sum(
            1 for r in rows if r.completed_at is not None and r.completed_at >= five_years_ago
        ),
        "amount_total": amount_total,
        "types_top": [
            {"type": t, "count": c} for t, c in types.most_common(8)
        ],
    }


def _manager_stats(session: Session, today: date) -> dict:
    rows = session.scalars(select(Manager)).all()
    return {
        "total": len(rows),
        "by_status": _status_count(rows),
        "with_evidence": sum(1 for r in rows if r.evidence_refs),
        "with_material_evidence": sum(1 for r in rows if _material_ref_count(r.evidence_refs)),
        "by_availability": dict(Counter(r.availability for r in rows)),
        "by_cert_level": dict(Counter(r.cert_level for r in rows)),
        "by_edu_safety_status": dict(Counter(r.edu_safety_status for r in rows)),
        "with_b_cert": sum(1 for r in rows if not _is_missing(r.b_cert_no)),
        "reg_no_missing": sum(1 for r in rows if _is_missing(r.reg_cert_no)),
        "cert_until_missing": sum(1 for r in rows if r.cert_valid_until is None),
        "on_active_project": sum(1 for r in rows if r.active_projects),
        # 数据质量哨兵（2026-09-22 项目经理M实测矛盾后新增）：状态=可用却登记在施项目
        "onsite_project_conflicts": sum(
            1 for r in rows if r.availability == "available" and (r.active_projects or [])
        ),
        "expected_available": sum(1 for r in rows if r.expected_available_at is not None),
        "cert_expiring_90d": sum(
            1 for r in rows if r.status != "expired" and _within_90d(r.cert_valid_until, today)
        ),
    }


def _personnel_stats(session: Session, today: date) -> dict:
    rows = session.scalars(select(Personnel)).all()
    by_category: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = by_category.setdefault(
            r.category, {"total": 0, "by_status": Counter(), "by_cert_level": Counter()}
        )
        c["total"] += 1
        c["by_status"][r.status] += 1
        if r.cert_level:
            c["by_cert_level"][r.cert_level] += 1
    for c in by_category.values():
        c["by_status"] = dict(c["by_status"])
        c["by_cert_level"] = dict(c["by_cert_level"])
    on_site = Counter(("未标注" if r.on_site is None else r.on_site) for r in rows)
    return {
        "total": len(rows),
        "by_status": _status_count(rows),
        "with_evidence": sum(1 for r in rows if r.evidence_refs),
        "with_material_evidence": sum(1 for r in rows if _material_ref_count(r.evidence_refs)),
        "by_category": by_category,
        "cert_no_missing": sum(1 for r in rows if _is_missing(r.cert_no)),
        "valid_until_missing": sum(1 for r in rows if r.valid_until is None),
        "by_on_site": dict(on_site),
        "onsite_project_conflicts": sum(
            1 for r in rows if r.on_site == "否" and r.on_site_project
        ),
        "cert_expiring_90d": sum(
            1 for r in rows if r.status != "expired" and _within_90d(r.valid_until, today)
        ),
    }


def _evidence_stats(session: Session) -> dict:
    rows = session.scalars(select(EvidenceFile)).all()
    return {
        "total": len(rows),
        "by_status": dict(
            Counter("未复核" if r.review_status is None else r.review_status for r in rows)
        ),
        "by_file_type": dict(Counter(r.file_type for r in rows)),
        "low_confidence": sum(
            1 for r in rows if r.ocr_confidence is not None and r.ocr_confidence < 0.9
        ),
        # 证据文件本身即证据：回链数=总数（口径与四类业务记录区分）
        "with_evidence": len(rows),
    }


def collect_stats(session: Session, today: date | None = None) -> dict:
    today = today or date.today()
    return {
        "today": today.isoformat(),
        "qualifications": _qualification_stats(session, today),
        "performances": _performance_stats(session, today),
        "managers": _manager_stats(session, today),
        "personnel": _personnel_stats(session, today),
        "evidence_files": _evidence_stats(session),
    }


# ── 维度装配（F027 §3：10 评分维度 + 5 内部项） ──────────────────


def _source(kind: str, label: str, subset: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "total": subset.get("total", 0),
        "by_status": subset.get("by_status", {}),
        "with_evidence": subset.get("with_evidence"),
    }


def _coverage(*subsets: dict[str, Any]) -> str:
    total = sum(s.get("total", 0) for s in subsets)
    active = sum(s.get("by_status", {}).get("active", 0) for s in subsets)
    if active:
        return "active"
    return "pending" if total else "missing"


def _level_summary(levels: dict[str, int]) -> str:
    parts = [
        f"{'等级待补' if k == SENTINEL else k} {v}" for k, v in
        sorted(levels.items(), key=lambda kv: -kv[1])
    ]
    return "、".join(parts) if parts else "无等级记录"


def _wan(amount: float) -> str:
    return f"{amount / 10000:,.0f}"


def _dimension_payloads(st: dict[str, Any]) -> list[dict[str, Any]]:
    q = st["qualifications"]
    pf = st["performances"]
    mg = st["managers"]
    pe = st["personnel"]
    dims: list[dict[str, Any]] = []

    # 1 投标报价 —— 不属企业资料库
    dims.append({
        "key": "bid_price", "label": "投标报价", "categories": ["objective"],
        "coverage": "not_applicable", "sources": [], "facts": [], "gaps": [],
        "remark": "报价不来自企业资料库：匹配时必须由授权人工录入（score_inputs.quoted_price，"
                  "F008 §4.2），系统不生成、不推荐、不假定报价；未录入时报价分 not_calculable，内部准入阻断。",
    })

    # 2 施工组织设计或技术方案 —— 主观评审
    dims.append({
        "key": "technical_plan", "label": "施工组织设计或技术方案", "categories": ["subjective"],
        "coverage": "not_applicable", "sources": [], "facts": [], "gaps": [],
        "remark": "标书编制产物，企业资料库不承载。系统对此类主观项不打分：匹配输出「待内部质量评审」，"
                  "未完成内部评审时评分结果为 manual_review（F008 §4.2），不以内部评审冒充评标分数。",
    })

    # 3 工期与进度保障
    dims.append({
        "key": "schedule", "label": "工期与进度保障", "categories": ["objective", "subjective"],
        "coverage": _coverage(pf),
        "sources": [_source("performances", "已完工业绩（开竣工日期）", pf)],
        "facts": [
            f"业绩台账 {pf['total']} 条，其中 {pf['with_full_start_end']} 条同时有开工与竣工日期"
            "（可支撑工期履约事实核查）",
            f"{pf['with_completed']} 条有竣工日期、{pf['with_amount']} 条有合同金额"
            + (f"（有值记录合计 {_wan(pf['amount_total'])} 万元，台账口径）" if pf["with_amount"] else ""),
        ],
        "gaps": [
            f"{pf['total'] - pf['with_full_start_end']} 条业绩缺少完整开竣工日期，涉及工期口径核查时将不可判定",
        ] if pf["total"] > pf["with_full_start_end"] else [],
        "remark": "进度计划本身属标书主观评审项；企业侧可核查的是历史工期履约事实（以台账+核验证据为准）。",
    })

    # 4 质量保证措施
    q_bucket = q["buckets"].get("quality_system", {})
    dims.append({
        "key": "quality", "label": "质量保证措施", "categories": ["subjective"],
        "coverage": _coverage(q_bucket),
        "sources": [_source("qualifications", "质量体系/ISO 认证", q_bucket)],
        "facts": [
            f"资质库质量/ISO 体系类记录 {q_bucket.get('total', 0)} 条",
        ],
        "gaps": [
            "资质库无质量体系/ISO 认证记录：如招标将质量体系认证作为评分项或硬性要求，需先补录并核验",
        ] if not q_bucket.get("total") else [],
        "remark": "质量保证措施方案属标书主观评审项；体系认证证书（如有）应入资质库并挂证据。",
    })

    # 5 安全生产与文明施工
    sl = q["buckets"].get("safety_license", {})
    sl_facts = [f"安全生产许可证台账 {sl.get('total', 0)} 项"]
    if sl.get("valid_untils"):
        sl_facts.append(f"最近有效期至 {sl['valid_untils'][-1]}（台账）")
    sl_facts.append(f"项目经理 {mg['with_b_cert']}/{mg['total']} 有安全 B 证编号")
    safety_gaps: list[str] = []
    if sl.get("by_status", {}).get(PENDING):
        safety_gaps.append(
            f"安许证记录 {sl['by_status'][PENDING]} 条待核验——未核验不参与匹配（F022 §2.6）"
        )
    if mg["total"] > mg["with_b_cert"]:
        safety_gaps.append(f"{mg['total'] - mg['with_b_cert']} 名项目经理安全 B 证编号待补")
    dims.append({
        "key": "safety", "label": "安全生产与文明施工", "categories": ["hard", "subjective"],
        "coverage": _coverage(sl),
        "sources": [
            _source("qualifications", "安全生产许可证", sl),
            _source("managers", "项目经理（安全 B 证）", mg),
        ],
        "facts": sl_facts,
        "gaps": safety_gaps,
        "remark": "安许证属独立证据种类 safety_license（不分等级，F008 §9.1）；文明施工措施属标书主观评审项。"
                  "专职安全员 C 证如需按人数核查，需在人员库补充岗位证书子类标注。",
    })

    # 6 项目经理及技术团队
    rb = pe["by_category"].get("registered_builder", {})
    tt = pe["by_category"].get("technical_title", {})
    avail = mg["by_availability"]
    avail_parts = [f"{('状态待补' if k == SENTINEL else k)} {v}" for k, v in avail.items()]
    dims.append({
        "key": "managers_team", "label": "项目经理及技术团队",
        "categories": ["hard", "objective"],
        "coverage": _coverage(mg, rb, tt),
        "sources": [
            _source("managers", "项目经理名录", mg),
            _source("personnel", "注册建造师（人员库）", rb),
            _source("personnel", "技术职称人员（人员库）", tt),
        ],
        "facts": [
            f"项目经理名录 {mg['total']} 人（{_level_summary(mg['by_cert_level'])}）",
            f"项目经理在施状态（台账）：{'、'.join(avail_parts) if avail_parts else '无记录'}",
            f"人员库：注册建造师 {rb.get('total', 0)} 人、技术职称 {tt.get('total', 0)} 人",
        ],
        "gaps": [
            f"{mg['total'] - mg['with_b_cert']} 名项目经理安全 B 证编号待补",
        ] if mg["total"] > mg["with_b_cert"] else [],
        "remark": "项目经理注册专业/等级/B 证/在建状态为 F007 硬条件字段，逐项匹配时缺任一即不可判定；"
                  "技术团队人数与证书按客观评分公式复算。",
    })

    # 7 类似工程业绩
    pf_facts = [
        f"已完工业绩台账 {pf['total']} 条（近五年竣工 {pf['completed_within_5y']} 条，"
        f"按台账竣工日期、截至 {st['today']}）",
        f"业绩证明构成：挂单项证明文件（中标通知书/合同/验收）{pf['with_material_evidence']} 条、"
        f"仅汇总台账行回链 {pf['with_evidence'] - pf['with_material_evidence']} 条、"
        f"无任何回链 {pf['total'] - pf['with_evidence']} 条",
    ]
    if pf["types_top"]:
        top = pf["types_top"][0]
        pf_facts.append(f"项目类型分布最多为「{top['type']}」（{top['count']} 条）")
    if pf["with_amount"]:
        pf_facts.append(f"有合同金额记录 {pf['with_amount']} 条，合计 {_wan(pf['amount_total'])} 万元（台账口径）")
    dims.append({
        "key": "similar_performance", "label": "类似工程业绩",
        "categories": ["hard", "objective"],
        "coverage": _coverage(pf),
        "sources": [_source("performances", "已完工业绩", pf)],
        "facts": pf_facts,
        "gaps": [
            f"{pf['total'] - pf['with_material_evidence']} 条业绩无单项证明文件（仅有汇总台账行回链或无回链）"
            "——类似业绩硬性核查以单项证据为准，可核验性不足",
        ] if pf["total"] > pf["with_material_evidence"] else [],
        "remark": "类似业绩判定按招标条款的类型/规模/年限起算口径逐项执行（F008 §4.1），"
                  "本视图只反映台账与证据覆盖情况。",
    })

    # 8 机械设备和资源配置
    dims.append({
        "key": "equipment", "label": "机械设备和资源配置",
        "categories": ["objective", "subjective"],
        "coverage": "missing", "sources": [],
        "facts": [],
        "gaps": [
            "企业资料库未建设机械设备台账（F006 未导入设备清单）；F008 已定义设备清单证据种类 "
            "equipment_list——如招标要求拟投入设备或按设备评分，需先补录并核验",
        ],
        "remark": "拟投入设备清单属标书内容；自有设备台账可作为企业侧事实来源，当前无数据。",
    })

    # 9 绿色施工、环保和 BIM
    dims.append({
        "key": "green_bim", "label": "绿色施工、环保和 BIM",
        "categories": ["objective", "subjective"],
        "coverage": "missing", "sources": [],
        "facts": [],
        "gaps": [
            "资质库无绿色施工/环保/BIM 类认证或证书记录——如招标按绿色施工、环保措施或 BIM 应用评分，需先补录",
        ],
        "remark": "绿色施工与环保措施方案属标书主观评审项；相关认证/证书（如有）应入资质库并挂证据。",
    })

    # 10 企业信用、奖项及履约记录
    cr = q["buckets"].get("credit_rating", {})
    cr_facts = [
        f"企业信用记录 {cr.get('total', 0)} 条"
        + (f"（{'、'.join(cr['labels'][:3])}）" if cr.get("labels") else ""),
        f"已完工业绩 {pf['total']} 条可作为履约记录的事实来源（以台账+核验证据为准）",
    ]
    dims.append({
        "key": "credit_awards", "label": "企业信用、奖项及履约记录",
        "categories": ["hard", "objective"],
        "coverage": _coverage(cr, pf),
        "sources": [
            _source("qualifications", "信用评级记录", cr),
            _source("performances", "履约记录（已完工业绩）", pf),
        ],
        "facts": cr_facts,
        "gaps": [
            "无独立奖项/荣誉台账（F006 未建表，honor_certificate 证据种类暂无数据）——如招标按奖项计分需补录",
            "信用核查记录（credit_check，如失信/处罚查询）无台账——招标要求信用承诺或核查记录时需人工办理并留痕",
        ],
        "remark": "信用核查类硬性要求（无失信记录等）以招标条款逐项核验；本视图只反映信用类资料的台账与证据覆盖。",
    })

    # ── 企业内部准入项（第 4 类） ──
    kinds = [
        ("qualifications", "资质", q), ("performances", "业绩", pf),
        ("managers", "项目经理", mg), ("personnel", "人员", pe),
    ]
    ev_gaps = [
        f"{label}：单项证明文件 {s['with_material_evidence']}/{s['total']} 条，"
        f"{s['total'] - s['with_material_evidence']} 条仅有台账行回链或无回链"
        "——逐项核查如需单项证明文件，需补录并核验"
        for _, label, s in kinds if s["total"] > s["with_material_evidence"]
    ]
    dims.append({
        "key": "evidence_integrity", "label": "证据回链完整性", "categories": ["internal"],
        "coverage": _coverage(q, pf, mg, pe),
        "sources": [_source(k, lbl, s) for k, lbl, s in kinds],
        "facts": [
            f"证据文件库 {st['evidence_files']['total']} 份"
            f"（低 OCR 置信度 {st['evidence_files']['low_confidence']} 份，需人工复核）"
        ],
        "gaps": ev_gaps,
        "remark": "无证据记录不可核验转 active（F022 §2.6）；台账记录本身不构成可核验证据。",
    })

    pending_detail = "、".join(
        f"{label} {s['by_status'].get(PENDING, 0)}" for _, label, s in kinds
        if s["by_status"].get(PENDING, 0)
    )
    pending_total = sum(s["by_status"].get(PENDING, 0) for _, _, s in kinds)
    dims.append({
        "key": "verification_status", "label": "核验状态", "categories": ["internal"],
        "coverage": _coverage(q, pf, mg, pe),
        "sources": [_source(k, lbl, s) for k, lbl, s in kinds],
        "facts": [f"待核验（pending_verification）共 {pending_total} 条" + (f"：{pending_detail}" if pending_detail else "")],
        "gaps": [
            f"全部 {pending_total} 条待核验记录在匹配中不可作为「满足」依据（F022 §2.6），需数据责任人核验后生效",
        ] if pending_total else [],
        "remark": "核验入口见「企业资料库（后台）」核验队列；核验通过即写 verified_at 并可参与 as_of 时点判定。",
    })

    expired_parts = [f"{label} {s['by_status'].get('expired', 0)}" for _, label, s in kinds
                     if s["by_status"].get("expired", 0)]
    expiring_parts = [
        f"资质 {q['expiring_90d']}", f"项目经理证书 {mg['cert_expiring_90d']}",
        f"人员证书 {pe['cert_expiring_90d']}",
    ]
    dims.append({
        "key": "validity_management", "label": "有效期管理", "categories": ["internal"],
        "coverage": _coverage(q, pf, mg, pe),
        "sources": [_source(k, lbl, s) for k, lbl, s in kinds],
        "facts": [
            f"已过期（expired）记录：" + ("、".join(expired_parts) if expired_parts else "0 条"),
            f"90 天内临期（未过期记录，按 {st['today']} 统计）：" + "、".join(expiring_parts),
        ],
        "gaps": [
            "临期记录需安排续证或更新：过期记录不可用于匹配（F006 §6.2）",
        ] if any((q["expiring_90d"], mg["cert_expiring_90d"], pe["cert_expiring_90d"])) else [],
        "remark": "临期与过期均按记录 valid_until 与统计日事实判定，不预告具体项目影响。",
    })

    avail_known = sum(v for k, v in avail.items() if k in ("available", "occupied", "planning"))
    on_site = pe["by_on_site"]
    dims.append({
        "key": "resource_availability", "label": "资源可用性与在施状态", "categories": ["internal"],
        "coverage": _coverage(mg, pe),
        "sources": [
            _source("managers", "项目经理（在施状态）", mg),
            _source("personnel", "人员（在施标注）", pe),
        ],
        "facts": [
            f"项目经理：可用 {avail.get('available', 0)} / 在施 {avail.get('occupied', 0)} / "
            f"规划中 {avail.get('planning', 0)} / 其他口径 {mg['total'] - avail_known}",
            f"明确登记在施项目的项目经理 {mg['on_active_project']} 人；已登记预计可投入时间 {mg['expected_available']} 人",
            f"人员库在施标注（台账）：{('、'.join(f'{k} {v}' for k, v in on_site.items())) or '无记录'}",
        ],
        "gaps": (
            [f"{mg['total'] - avail_known} 名项目经理在施状态为待补/未知——可用性判断将不可判定"]
            if mg["total"] > avail_known else []
        ) + (
            [f"在施状态与在施项目矛盾 {mg['onsite_project_conflicts']} 名经理 / "
             f"{pe['onsite_project_conflicts']} 条人员记录（状态=否却登记项目，以主表状态为准，项目名待公司确认）"]
            if mg["onsite_project_conflicts"] or pe["onsite_project_conflicts"] else []
        ),
        "remark": "无合格可用项目经理时内部准入不通过（F008 §6.1 exists(qualified_available_project_manager)）；"
                  "系统不生成不存在的经理（F007 §6.3）。",
    })

    dims.append({
        "key": "field_completeness", "label": "关键字段完整性", "categories": ["internal"],
        "coverage": _coverage(mg, pe),
        "sources": [
            _source("managers", "项目经理（证书字段）", mg),
            _source("personnel", "人员（证书字段）", pe),
        ],
        "facts": [
            f"项目经理：注册编号待补 {mg['reg_no_missing']} 条、B 证编号待补 {mg['total'] - mg['with_b_cert']} 条、"
            f"证书有效期未登记 {mg['cert_until_missing']} 条",
            f"人员：证书编号待补 {pe['cert_no_missing']}/{pe['total']} 条、证书有效期未登记 {pe['valid_until_missing']} 条",
        ],
        "gaps": [
            "待补字段在逐项匹配中按「不可判定/待补」处理，不推断满足（项目红线：禁止编造）",
        ],
        "remark": "字段待补源于台账「后续补充」等占位值（导入时归一化为 NULL/待补哨兵，F006 §6.7）。",
    })

    return dims


def _summary(st: dict[str, Any]) -> dict[str, Any]:
    order = ["qualifications", "performances", "managers", "personnel", "evidence_files"]
    kinds = []
    for kind in order:
        s = st[kind]
        kinds.append({
            "kind": kind,
            "label": _KIND_LABELS[kind],
            "total": s["total"],
            "by_status": s["by_status"],
            "with_evidence": s["with_evidence"],
        })
    core = [st[k] for k in order[:4]]
    return {
        "as_of": st["today"],
        "kinds": kinds,
        "records_total": sum(s["total"] for s in core),
        "active_total": sum(s["by_status"].get("active", 0) for s in core),
        "pending_total": sum(s["by_status"].get(PENDING, 0) for s in core),
        "expired_total": sum(s["by_status"].get("expired", 0) for s in core),
        "rejected_total": sum(s["by_status"].get("rejected", 0) for s in core),
    }


def build_overview(session: Session, today: date | None = None) -> dict[str, Any]:
    """F027 §5：企业资料画像（只读聚合，不写库不写审计）。"""
    st = collect_stats(session, today)
    return {
        "note": "本视图为资料解析情况的事实聚合（计数与覆盖统计），不构成资格审查结论、评分或投标建议；"
                "逐项匹配结论以匹配引擎输出为准（F008/F023），满分仍须经营负责人人工审批。",
        "summary": _summary(st),
        "category_definitions": CATEGORY_DEFS,
        "dimensions": _dimension_payloads(st),
    }
