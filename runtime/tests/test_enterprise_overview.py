# F027 企业资料画像聚合测试（sqlite 内存库）
# 覆盖（F027 §7 验收）：
# - 空库不报错、全 0 / missing / not_applicable，不编造数字
# - 四分类定义与 15 个维度（10 评分维度 + 5 内部项）按 §3 输出
# - 计数与库内事实一致（资质分桶 / 业绩日期与证据 / 项目经理 B 证 / 人员待补）
# - facts/gaps 均为事实陈述，不出现资格结论措辞
from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from runtime.db.models import (
    Base,
    EvidenceFile,
    Manager,
    Performance,
    Personnel,
    Qualification,
)
from runtime.db import enterprise_profile

# sqlite 不支持多 schema：剥离前缀（与 test_enterprise_import.py 同法）
_SCHEMA_PREFIX = re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed(session: Session) -> None:
    from datetime import date

    session.add_all([
        Qualification(
            qualification_id="Q-SL-1", category="安全生产许可证",
            valid_until=date(2028, 11, 4), evidence_refs=["MAT-E:v1"],
            data_owner="甲", status="active",
        ),
        Qualification(
            qualification_id="Q-CR-1", category="企业信用等级 AAA",
            data_owner="甲", status="pending_verification",
        ),
        Qualification(
            qualification_id="Q-CQ-1", category="建筑工程施工总承包", level="特级",
            data_owner="甲", status="pending_verification",
        ),
        Performance(
            performance_id="PF-1", project_name="某住宅项目", project_type="房屋建筑工程",
            contract_amount=98_000_000, awarded_at=date(2022, 3, 1),
            completed_at=date(2024, 6, 30), evidence_refs=["MAT-PF:v1"],
            data_owner="甲", status="pending_verification",
        ),
        Performance(
            performance_id="PF-2", project_name="某市政道路", project_type="市政道路工程",
            data_owner="甲", status="pending_verification",
        ),
        Performance(
            performance_id="PF-3", project_name="某公路工程", project_type="公路工程",
            evidence_refs=["ledger:xlsx:sheet0:row9"],  # 仅汇总台账行回链（F027 §4 两级口径）
            data_owner="甲", status="pending_verification",
        ),
        Manager(
            manager_id="PM-1", display_name="张**", organization="某建设集团",
            specialty="建筑工程", reg_cert_type="一级建造师", reg_cert_no="冀13xxxx",
            cert_level="一级", b_cert_no="B-001", availability="available",
            evidence_refs=[], data_owner="甲", status="pending_verification",
        ),
        Manager(
            manager_id="PM-2", display_name="李**", organization="某建设集团",
            specialty="市政", reg_cert_type="二级建造师", reg_cert_no="__待补__",
            cert_level="二级", availability="occupied", active_projects=["某在施项目"],
            evidence_refs=[], data_owner="甲", status="pending_verification",
        ),
        Personnel(
            personnel_id="P-1", name="王**", category="registered_builder",
            specialty="建筑工程", cert_level="一级建造师", cert_no="冀2xxxx",
            on_site="否", data_owner="甲", status="pending_verification",
        ),
        Personnel(
            personnel_id="P-2", name="赵**", category="technical_title",
            cert_level="正高级工程师", cert_no="ZC-1",
            data_owner="甲", status="pending_verification",
        ),
        Personnel(
            personnel_id="P-3", name="钱**", category="post_certificate",
            cert_no="__待补__", data_owner="甲", status="pending_verification",
        ),
        EvidenceFile(
            evidence_id="E-1", file_type="证书扫描件", object_uri="obj://e1",
            source_hash="h1", ocr_confidence=0.95, review_status="approved",
            uploaded_by="甲",
        ),
        EvidenceFile(
            evidence_id="E-2", file_type="合同", object_uri="obj://e2",
            source_hash="h2", ocr_confidence=0.5, uploaded_by="甲",
        ),
    ])
    session.commit()


def _overview(session):
    from datetime import date

    return enterprise_profile.build_overview(session, today=date(2026, 9, 22))


def _dim(payload: dict, key: str) -> dict:
    return next(d for d in payload["dimensions"] if d["key"] == key)


# ── 结构与空库（F027 §5/§7.2） ──────────────────────────────


class TestStructure:
    def test_categories_and_dimension_count(self, session):
        payload = _overview(session)
        keys = [c["key"] for c in payload["category_definitions"]]
        assert keys == ["hard", "objective", "subjective", "internal"]
        assert len(payload["dimensions"]) == 15
        # 10 个评分维度标签与 F027 §3 逐字一致
        labels = [d["label"] for d in payload["dimensions"][:10]]
        assert labels == [
            "投标报价", "施工组织设计或技术方案", "工期与进度保障", "质量保证措施",
            "安全生产与文明施工", "项目经理及技术团队", "类似工程业绩",
            "机械设备和资源配置", "绿色施工、环保和 BIM", "企业信用、奖项及履约记录",
        ]
        # 每个维度的分类 key 均在四分类定义内
        valid = set(keys)
        for d in payload["dimensions"]:
            assert d["categories"] and set(d["categories"]) <= valid, d["key"]

    def test_empty_db_all_zero_no_fabrication(self, session):
        payload = _overview(session)
        s = payload["summary"]
        assert s["records_total"] == 0 and s["active_total"] == 0 and s["pending_total"] == 0
        assert _dim(payload, "safety")["coverage"] == "missing"
        assert _dim(payload, "similar_performance")["coverage"] == "missing"
        # 非资料库承载维度如实标注，不给编造计数
        assert _dim(payload, "bid_price")["coverage"] == "not_applicable"
        assert _dim(payload, "technical_plan")["coverage"] == "not_applicable"
        assert _dim(payload, "equipment")["coverage"] == "missing"
        for d in payload["dimensions"]:
            for src in d["sources"]:
                assert src["total"] == 0, (d["key"], src)

    def test_note_disclaims_conclusions(self, session):
        note = _overview(session)["note"]
        assert "不构成资格审查结论" in note
        assert "不构成" in note and "投标建议" in note


# ── 计数与库内事实一致（F027 §7.3） ────────────────────────


class TestAggregation:
    @pytest.fixture()
    def payload(self, session):
        _seed(session)
        return _overview(session)

    def test_summary_counts(self, payload):
        s = payload["summary"]
        assert s["records_total"] == 11  # 资质3 + 业绩3 + 项目经理2 + 人员3
        assert s["active_total"] == 1
        assert s["pending_total"] == 10
        assert s["expired_total"] == 0
        kinds = {k["kind"]: k for k in s["kinds"]}
        assert kinds["qualifications"]["total"] == 3
        assert kinds["evidence_files"]["total"] == 2

    def test_safety_dimension(self, payload):
        d = _dim(payload, "safety")
        assert d["coverage"] == "active"           # 安许证已核验
        assert d["categories"] == ["hard", "subjective"]
        joined = "；".join(d["facts"])
        assert "安全生产许可证台账 1 项" in joined
        assert "最近有效期至 2028-11-04" in joined
        assert "项目经理 1/2 有安全 B 证编号" in joined
        assert any("安全 B 证编号待补" in g for g in d["gaps"])

    def test_credit_bucket_label_appears(self, payload):
        d = _dim(payload, "credit_awards")
        assert d["coverage"] == "pending"
        assert any("AAA" in f for f in d["facts"])

    def test_performance_dates_and_evidence(self, payload):
        d = _dim(payload, "similar_performance")
        assert d["coverage"] == "pending"
        joined = "；".join(d["facts"])
        assert "已完工业绩台账 3 条" in joined
        assert "近五年竣工 1 条" in joined  # PF-1 竣工 2024-06-30；PF-2/PF-3 无竣工日期
        # 两级证据口径（F027 §4）：单项证明 / 仅台账行回链 / 无回链 分开计数
        assert "挂单项证明文件（中标通知书/合同/验收）1 条" in joined
        assert "仅汇总台账行回链 1 条、无任何回链 1 条" in joined
        assert any("2 条业绩无单项证明文件" in g for g in d["gaps"])
        sched = _dim(payload, "schedule")
        assert any("2 条业绩缺少完整开竣工日期" in g for g in sched["gaps"])

    def test_managers_team_levels(self, payload):
        d = _dim(payload, "managers_team")
        joined = "；".join(d["facts"])
        assert "项目经理名录 2 人（一级 1、二级 1）" in joined
        assert "注册建造师 1 人、技术职称 1 人" in joined

    def test_missing_dimensions_reported_factually(self, payload):
        for key in ("quality", "green_bim", "equipment"):
            d = _dim(payload, key)
            assert d["coverage"] == "missing", key
            assert d["gaps"] and any("需先补录" in g or "需补录" in g for g in d["gaps"]), key

    def test_verification_status(self, payload):
        d = _dim(payload, "verification_status")
        assert any("共 10 条" in f for f in d["facts"])
        assert any("不可作为" in g for g in d["gaps"])

    def test_field_completeness(self, payload):
        d = _dim(payload, "field_completeness")
        joined = "；".join(d["facts"])
        assert "证书编号待补 1/3" in joined
        assert "注册编号待补 1 条" in joined

    def test_resource_availability(self, payload):
        d = _dim(payload, "resource_availability")
        joined = "；".join(d["facts"])
        assert "可用 1 / 在施 1 /" in joined
        assert "明确登记在施项目的项目经理 1 人" in joined

    def test_validity_management(self, payload):
        d = _dim(payload, "validity_management")
        joined = "；".join(d["facts"])
        assert "已过期（expired）记录：0 条" in joined
        assert "90 天内临期" in joined
        # 库内无临期记录 → 不输出「需安排续证」缺口
        assert d["gaps"] == []

    def test_evidence_integrity(self, payload):
        d = _dim(payload, "evidence_integrity")
        assert any(g.startswith("业绩：单项证明文件 1/3 条") for g in d["gaps"])
        joined = "；".join(d["facts"])
        assert "证据文件库 2 份" in joined and "低 OCR 置信度 1 份" in joined

    def test_bid_price_and_technical_plan_not_in_library(self, payload):
        assert _dim(payload, "bid_price")["sources"] == []
        assert "quoted_price" in _dim(payload, "bid_price")["remark"]
        assert "内部质量评审" in _dim(payload, "technical_plan")["remark"]
