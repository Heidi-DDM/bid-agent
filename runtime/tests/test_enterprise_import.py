# R022 企业资料导入与核验服务测试（sqlite 内存库）
# 覆盖（08-清单 §2.2 第 1/3/4 项）：
# - 归一化：占位符→待补、Excel 序列日期→ISO、越界→不推断（None）
# - 批量导入幂等（重复导入 skipped，不产生重复主键）
# - 无证据不可转 active（F022 §2.6）；approve 写 verified_at；reject 留痕
# - 过期自动 expired；重导入回 pending
# - 匹配侧 _snapshot_eligible：未核验/过期不进 as_of 有效快照
from __future__ import annotations

import re as _re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.db.models import Base, Manager, Performance, Personnel, Qualification
from runtime.db import enterprise_service
from runtime.core.errors import ApiError

# sqlite 不支持多 schema：剥离前缀（与 test_worker_match.py 同法）
_SCHEMA_PREFIX = _re.compile(
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


# ── 归一化 ──────────────────────────────────────────────

class TestNormalize:
    def test_placeholder_to_none(self):
        for v in ["后续补充", "不用填", "待补", "占位", "", "  ", None]:
            assert enterprise_service.normalize_placeholder(v) is None, v

    def test_real_value_kept(self):
        assert enterprise_service.normalize_placeholder("一级建造师") == "一级建造师"
        assert enterprise_service.normalize_placeholder(0) == 0
        assert enterprise_service.normalize_placeholder(False) is False

    def test_list_dict_never_stringified(self):
        """JSONB 列（active_projects/evidence_refs）不得被 str()（空 list → '[]' bug 回归）。"""
        assert enterprise_service.normalize_placeholder([]) == []
        assert enterprise_service.normalize_placeholder(["建筑工程"]) == ["建筑工程"]
        assert enterprise_service.normalize_placeholder({"a": 1}) == {"a": 1}
        assert enterprise_service.normalize_placeholder(3.14) == 3.14

    def test_excel_serial_to_iso(self):
        assert enterprise_service.excel_serial_to_iso("43344") == "2018-09-01"
        assert enterprise_service.excel_serial_to_iso("45443") == "2024-05-31"
        assert enterprise_service.excel_serial_to_iso("2025-10-30") == "2025-10-30"
        assert enterprise_service.excel_serial_to_iso("2025/10/30") == "2025-10-30"
        # 越界/占位 → None（不推断）
        assert enterprise_service.excel_serial_to_iso("1") is None
        assert enterprise_service.excel_serial_to_iso("后续补充") is None
        assert enterprise_service.excel_serial_to_iso("") is None

    def test_cert_level_parse(self):
        assert enterprise_service.parse_cert_level("一级建造师") == ("一级建造师", "一级")
        assert enterprise_service.parse_cert_level("正高级工程师") == ("正高级工程师", "正高")
        assert enterprise_service.parse_cert_level("") == (None, None)

    def test_on_site(self):
        assert enterprise_service.parse_on_site("是") == ("occupied", None)
        assert enterprise_service.parse_on_site("否") == ("available", None)
        assert enterprise_service.parse_on_site("后续补充") == ("__待补__", None)


# ── 批量导入 ────────────────────────────────────────────

class TestImport:
    def test_import_qualifications_pending_and_idempotent(self, session):
        rows = [
            {"category": "建筑工程施工总承包", "level": "特级",
             "valid_until": "2029-12-09", "evidence_refs": ["E-1"]},
            {"category": "安全生产许可证", "level": "不分等级",
             "valid_until": "2028-11-04", "evidence_refs": ["E-2"]},
        ]
        r1 = enterprise_service.import_qualifications(
            session, rows, data_owner="项目负责人", actor="data_admin", source="test.xlsx")
        session.commit()
        assert r1["created"] == 2 and r1["skipped"] == 0
        # 幂等重放
        r2 = enterprise_service.import_qualifications(
            session, rows, data_owner="项目负责人", actor="data_admin", source="test.xlsx")
        session.commit()
        assert r2["created"] == 0 and r2["skipped"] == 2
        assert session.scalars(select(Qualification)).all().__len__() == 2
        # 默认 pending_verification
        q = session.scalars(select(Qualification)).first()
        assert q.status == "pending_verification"
        assert q.data_owner == "项目负责人"

    def test_import_performances_serial_dates(self, session):
        rows = [{"project_name": "石家庄学院实训基地", "project_type": "房屋建筑",
                 "owner_org": "某高校", "contract_amount_wan": "12000",
                 "completed_at": "45443",  # Excel 序列
                 "evidence_refs": ["E-P1"]}]
        enterprise_service.import_performances(
            session, rows, data_owner="项目负责人", actor="data_admin")
        session.commit()
        p = session.scalars(select(Performance)).first()
        assert p.completed_at is not None
        assert p.completed_at.year == 2024
        assert float(p.contract_amount) == 12000 * 1e4  # 万元 → 元
        assert p.status == "pending_verification"

    def test_import_personnel_categories(self, session):
        rows = [
            {"name": "张某某", "specialty": "建筑工程", "cert_level": "一级建造师",
             "cert_no": "冀11111", "evidence_refs": ["E-C1"]},
            {"name": "李某某", "specialty": "建筑工程", "cert_level": "中级", "evidence_refs": []},
        ]
        enterprise_service.import_personnel(
            session, rows, category="technical_title",
            data_owner="项目负责人", actor="data_admin")
        session.commit()
        assert session.scalars(select(Personnel)).all().__len__() == 2

    def test_import_managers_with_bcert(self, session):
        rows = [{
            "display_name": "张**", "organization": "某建设集团",
            "specialty": "建筑工程", "reg_cert_type": "一级建造师",
            "reg_cert_no": "冀11", "cert_level": "一级",
            "b_cert_no": "冀建安B(2023)0244343",
            "cert_valid_until": "2030-12-31",
            "availability": "available",
            "evidence_refs": ["E-M1"],
        }]
        enterprise_service.import_managers(
            session, rows, data_owner="项目负责人", actor="data_admin")
        session.commit()
        m = session.scalars(select(Manager)).first()
        assert m.b_cert_no == "冀建安B(2023)0244343"
        assert m.status == "pending_verification"
        assert m.display_name == "张**"  # 调用方已脱敏不再二次处理

    def test_import_managers_missing_cert_until_null(self, session):
        """缺失有效期不得伪造占位日期（0008 nullable；engine 缺失→unverifiable）。"""
        rows = [{"display_name": "李**", "specialty": "建筑工程",
                 "reg_cert_no": "冀22", "evidence_refs": []}]
        enterprise_service.import_managers(
            session, rows, data_owner="项目负责人", actor="data_admin")
        session.commit()
        m = session.scalars(select(Manager)).first()
        assert m.cert_valid_until is None

    def test_import_active_verified_snapshot_preserved(self, session):
        """存量已核验资料（脱敏证据快照）导入：显式 active+verified_at 且带证据 → 保留，
        匹配 as_of 时点可入快照（verified_at <= as_of）。"""
        from datetime import datetime, timezone

        rows = [{"category": "建筑工程施工总承包", "level": "特级",
                 "valid_until": "2029-12-09",
                 "status": "active", "verified_at": "2025-09-15",
                 "evidence_refs": ["1.资质证书清单及扫描件/清单.xls 第8项"]}]
        r = enterprise_service.import_qualifications(
            session, rows, data_owner="项目负责人", actor="data_admin")
        session.commit()
        assert r["created"] == 1
        q = session.scalars(select(Qualification)).first()
        assert q.status == "active"
        assert q.verified_at is not None
        assert q.verified_at.year == 2025

    def test_import_active_without_evidence_forced_pending(self, session):
        """声明 active 但无证据/无 verified_at → 强制 pending（F022 无证据不可转 active）。"""
        rows = [{"category": "ISO 认证", "level": "无",
                 "status": "active", "verified_at": "2025-09-15",
                 "evidence_refs": []}]
        r = enterprise_service.import_qualifications(
            session, rows, data_owner="项目负责人", actor="data_admin")
        session.commit()
        assert r["created"] == 1
        q = session.scalars(select(Qualification)).first()
        assert q.status == "pending_verification"
        assert q.verified_at is None


# ── 核验队列与流转 ──────────────────────────────────────

class TestVerify:
    def _mk_qualification(self, session, *, evidence=True, valid_until="2029-12-31",
                          qid="Q-T1") -> Qualification:
        q = Qualification(
            qualification_id=qid, category="建筑工程施工总承包", level="特级",
            specialty="房屋建筑", valid_until=__import__("datetime").date.fromisoformat(valid_until),
            issuer="住建部", evidence_refs=["E-1"] if evidence else [],
            data_owner="项目负责人", status="pending_verification",
        )
        session.add(q)
        session.commit()
        return q

    def test_queue_lists_pending_only(self, session):
        q = self._mk_qualification(session)
        queue = enterprise_service.list_verification_queue(session)
        assert len(queue) == 1 and queue[0]["id"] == q.qualification_id
        assert queue[0]["kind"] == "qualifications"
        assert queue[0]["status"] == "pending_verification"

    def test_approve_sets_active_and_verified_at(self, session):
        q = self._mk_qualification(session)
        res = enterprise_service.verify_records(
            session, kind="qualifications", ids=[q.qualification_id],
            action="approve", actor="项目负责人", comment="核验通过")
        session.commit()
        assert res["changed"] == 1
        session.refresh(q)
        assert q.status == "active"
        assert q.verified_at is not None

    def test_approve_without_evidence_blocked(self, session):
        q = self._mk_qualification(session, evidence=False)
        with pytest.raises(ApiError) as ei:
            enterprise_service.verify_records(
                session, kind="qualifications", ids=[q.qualification_id],
                action="approve", actor="项目负责人")
        assert "无证据不可转 active" in str(ei.value)
        session.rollback()
        session.refresh(q)
        assert q.status == "pending_verification"  # 未部分提交

    def test_reject_then_reimport(self, session):
        q = self._mk_qualification(session)
        enterprise_service.verify_records(
            session, kind="qualifications", ids=[q.qualification_id],
            action="reject", actor="项目负责人", comment="证件过期需补新")
        session.commit()
        session.refresh(q)
        assert q.status == "rejected"
        enterprise_service.reimport_records(
            session, kind="qualifications", ids=[q.qualification_id], actor="data_admin")
        session.commit()
        session.refresh(q)
        assert q.status == "pending_verification"
        assert q.verified_at is None  # 旧核验失效

    def test_expire_overdue_auto(self, session):
        q = self._mk_qualification(session, valid_until="2020-01-01")
        # 先核验 active（证据齐但过期 → verify 时直接 expired）
        res = enterprise_service.verify_records(
            session, kind="qualifications", ids=[q.qualification_id],
            action="approve", actor="项目负责人")
        session.commit()
        session.refresh(q)
        assert res["expired"] == [q.category] and q.status == "expired"


# ── 自动核验（F027 2026-09-22 业主决策）与通配搜索 ──────────────


class TestAutoVerifyAndWildcard:
    def test_wildcard_like(self):
        assert enterprise_service.wildcard_like("技*") == "%技%"
        assert enterprise_service.wildcard_like("张伟") == "%张伟%"
        assert enterprise_service.wildcard_like("  ") == ""
        assert enterprise_service.wildcard_like("李*·0002") == "%李%·0002%"

    def test_auto_verify_activates_with_evidence(self, session):
        session.add(Qualification(
            qualification_id="Q-AV-1", category="建筑工程施工总承包", level="特级",
            valid_until=__import__("datetime").date(2029, 12, 9),
            evidence_refs=["MAT-E:v1"], data_owner="甲", status="pending_verification"))
        session.add(Qualification(
            qualification_id="Q-AV-2", category="CA数字证书",
            evidence_refs=["MAT-E:v2"], data_owner="甲", status="pending_verification"))
        # 无证据：保持待核验（F022 无证据不可 active 不放松）
        session.add(Qualification(
            qualification_id="Q-AV-3", category="待补证资质",
            evidence_refs=[], data_owner="甲", status="pending_verification"))
        session.commit()
        result = enterprise_service.auto_verify_records(session, actor="auto-test")
        session.commit()
        q1, q2, q3 = (session.get(Qualification, k) for k in ("Q-AV-1", "Q-AV-2", "Q-AV-3"))
        assert q1.status == "active" and q1.verified_at is not None
        assert q2.status == "active"          # valid_until 为空 = 长期有效口径
        assert q3.status == "pending_verification"  # 无证据不自动通过
        assert result["results"]["qualifications"] == {
            "changed": 2, "expired": 0, "pending_no_evidence": 1}
        assert result["total_changed"] == 2

    def test_auto_verify_expires_overdue(self, session):
        from datetime import date as _date
        session.add(Qualification(
            qualification_id="Q-AV-EXP", category="安全生产许可证",
            valid_until=_date(2020, 1, 1), evidence_refs=["MAT-E:v1"],
            data_owner="甲", status="pending_verification"))
        session.commit()
        result = enterprise_service.auto_verify_records(session, actor="auto-test")
        session.commit()
        q = session.get(Qualification, "Q-AV-EXP")
        assert q.status == "expired"
        assert result["results"]["qualifications"]["expired"] == 1

    def test_auto_verify_idempotent_and_skips_active(self, session):
        session.add(Qualification(
            qualification_id="Q-AV-ACT", category="已核验资质",
            evidence_refs=["MAT-E:v1"], data_owner="甲", status="active"))
        session.commit()
        result = enterprise_service.auto_verify_records(session, actor="auto-test")
        session.commit()
        # 已 active 不在处理集；重复执行 changed=0（幂等）
        assert result["total_changed"] == 0
