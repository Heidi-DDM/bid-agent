# F022 §5.1 v1.3 Excel/CSV 台账受控导入测试（09-优化方案 §3.5）
# 分层覆盖：
# - excel_service 纯函数：嗅探（魔数/空/超大/加密/外部链接/宏）、预览（表头/行数/
#   前 5 行/unmapped_columns/占位符警告）、按映射读取（ledger:sheet:row 行回链）
# - 路由全链路（sqlite 内存库 + override get_db + 临时对象存储）：
#   preview → commit（原文 evidence 落库 + 结构化行导入 + 幂等）→ 风险文件拒绝无残留
from __future__ import annotations

import csv
import io
import os
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from openpyxl import Workbook

from runtime.db import excel_service
from runtime.db.models import Base, Material, Personnel, Qualification
from runtime.routers.deps import get_db

os.environ.setdefault("AUTH_DEV_HEADERS", "true")
os.environ.setdefault("APP_ENV", "test")

_SCHEMA_PREFIX = re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)


def _make_xlsx(headers: list[str], rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _make_csv(headers: list[str], rows: list[list], bom: bool = True) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    text = buf.getvalue()
    return ("\ufeff" + text if bom else text).encode("utf-8")


def _make_xlsx_with_external_links() -> bytes:
    """构造含 xl/externalLinks/ 的 zip：外链公式风险，必须拒绝。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/externalLinks/externalLink1.xml", "<links/>")
    return buf.getvalue()


# ── excel_service 纯函数：嗅探 / 预览 / 映射读取 ──────────────────

class TestSniff:
    def test_csv_ok_and_binary_rejected(self):
        assert excel_service.sniff_ledger(_make_csv(["a"], [["1"]]), ".csv") == ".csv"
        with pytest.raises(excel_service.LedgerError):
            excel_service.sniff_ledger(b"PK\x00\x01binary\x00content", ".csv")  # 二进制伪 csv

    def test_xlsx_magic_and_corrupt(self):
        assert excel_service.sniff_ledger(_make_xlsx(["a"], [["1"]]), ".xlsx") == ".xlsx"
        with pytest.raises(excel_service.LedgerError, match="魔数"):
            excel_service.sniff_ledger(b"%PDF-not-xlsx", ".xlsx")
        with pytest.raises(excel_service.LedgerError, match="魔数"):
            excel_service.sniff_ledger(_make_xlsx(["a"], [["1"]]), ".xls")  # xlsx 内容伪装 .xls

    def test_xls_ole_magic_ok(self):
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
        assert excel_service.sniff_ledger(ole, ".xls") == ".xls"

    def test_empty_rejected(self):
        with pytest.raises(excel_service.LedgerError, match="空文件"):
            excel_service.sniff_ledger(b"", ".xlsx")
        with pytest.raises(excel_service.LedgerError, match="空文件"):
            excel_service.sniff_ledger(b"", ".csv")

    def test_macro_and_unsupported_suffixes(self):
        with pytest.raises(excel_service.LedgerError, match="白名单"):
            excel_service.sniff_ledger(b"PK...", ".xlsm")
        with pytest.raises(excel_service.LedgerError, match="白名单"):
            excel_service.sniff_ledger(b"", ".doc")

    def test_too_large_rejected(self):
        big = b"a,b\n" + (b"1,2\n" * (excel_service.LEDGER_MAX_BYTES // 4 + 1000))
        assert len(big) > excel_service.LEDGER_MAX_BYTES
        with pytest.raises(excel_service.LedgerError, match="超过上限"):
            excel_service.sniff_ledger(big, ".csv")


class TestPreview:
    def test_xlsx_preview_with_kind_unmapped(self):
        content = _make_xlsx(
            ["资质类别", "资质等级", "备注"],
            [["建筑工程施工总承包", "特级", "新办"], ["市政公用工程", "一级", ""]],
        )
        pv = excel_service.preview_ledger(content, ".xlsx", kind="qualifications")
        assert pv["kind"] == "qualifications"
        assert pv["headers"] == ["资质类别", "资质等级", "备注"]
        assert pv["row_count"] == 2
        assert len(pv["preview_rows"]) == 2
        # 「备注」不在 qualifications 允许字段键 → 待人工确认，不静默丢弃
        assert "备注" in pv["unmapped_columns"]
        assert "资质类别" not in pv["unmapped_columns"]

    def test_preview_empty_header_column(self):
        content = _make_xlsx(["资质类别", ""], [["建筑工程", "特级"]])
        pv = excel_service.preview_ledger(content, ".xlsx", kind="qualifications")
        assert any("表头为空" in u for u in pv["unmapped_columns"])

    def test_preview_placeholder_warning(self):
        content = _make_xlsx(["资质类别", "资质等级"], [["建筑工程", "待补"]])
        pv = excel_service.preview_ledger(content, ".xlsx", kind="qualifications")
        assert pv["warnings"] and "待补" in pv["warnings"][0]

    def test_preview_unknown_kind(self):
        with pytest.raises(excel_service.LedgerError, match="未知资料类型"):
            excel_service.preview_ledger(b"a\n", ".csv", kind="nope")

    def test_preview_rejects_external_links_xlsx(self):
        with pytest.raises(excel_service.LedgerError, match="外部链接"):
            excel_service.preview_ledger(_make_xlsx_with_external_links(), ".xlsx",
                                         kind="qualifications")

    def test_xls_preview_old_format_honest(self):
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        pv = excel_service.preview_ledger(ole, ".xls", kind="qualifications")
        assert pv["notes"] == ["xls-old-format"]
        assert pv["warnings"] and "转存" in pv["warnings"][0]
        assert pv["headers"] == []

    def test_csv_bom_read(self):
        content = _make_csv(["资质类别"], [["建筑工程"]])
        pv = excel_service.preview_ledger(content, ".csv", kind="qualifications")
        assert pv["headers"] == ["资质类别"]
        assert pv["row_count"] == 1


class TestReadRows:
    def test_mapping_with_row_backlink(self):
        content = _make_csv(
            ["资质类别", "资质等级", "有效期至", "备注"],
            [["建筑工程施工总承包", "特级", "2028-12-31", "x"], ["市政公用工程", "一级", "2029-01-01", ""]],
        )
        rows = excel_service.read_ledger_rows(content, ".csv", {
            "资质类别": "category", "资质等级": "level", "有效期至": "valid_until",
        })
        assert len(rows) == 2
        assert rows[0]["category"] == "建筑工程施工总承包"
        assert rows[0]["valid_until"] == "2028-12-31"
        # 来源行回链：表头行号=1，数据行从 2 起（不映射列不进入字典）
        assert "备注" not in rows[0]
        assert rows[0]["evidence_refs"] == ["ledger:csv:row2"]
        assert rows[1]["evidence_refs"] == ["ledger:csv:row3"]

    def test_blank_rows_skipped(self):
        content = _make_csv(["资质类别"], [["建筑工程"], [""], ["市政公用"]])
        rows = excel_service.read_ledger_rows(content, ".csv", {"资质类别": "category"})
        assert len(rows) == 2

    def test_xlsx_rows_and_existing_refs_kept(self):
        content = _make_xlsx(
            ["项目名称", "证据位置"],
            [["石家庄学院实训基地", "E-1;E-2"]],
        )
        rows = excel_service.read_ledger_rows(content, ".xlsx", {
            "项目名称": "project_name", "证据位置": "evidence_refs",
        })
        assert rows[0]["project_name"] == "石家庄学院实训基地"
        assert rows[0]["evidence_refs"] == ["E-1", "E-2", "ledger:xlsx:row2"]

    def test_xls_import_rejected_with_guidance(self):
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        with pytest.raises(excel_service.LedgerError, match="转存"):
            excel_service.read_ledger_rows(ole, ".xls", {})

    def test_risky_xlsx_zip_rejected(self):
        with pytest.raises(excel_service.LedgerError):
            excel_service.read_ledger_rows(_make_xlsx_with_external_links(), ".xlsx", {})


# ── 路由全链路（sqlite + override get_db + 临时对象存储） ──────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    # TestClient 在独立线程执行端点：sqlite 内存库必须单连接共享（StaticPool）
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)

    def _db_override():
        with Session(engine) as s:
            yield s

    monkeypatch.setenv("APP_ENV", "test")
    # 端点内 object_store_root 已 import 进 enterprise 模块命名空间 → patch 该引用
    monkeypatch.setattr("runtime.routers.enterprise.object_store_root",
                        lambda: str(tmp_path / "objects"))
    from runtime.api import app

    app.dependency_overrides[get_db] = _db_override
    with TestClient(app, raise_server_exceptions=False) as c:
        c.engine = engine
        yield c
    app.dependency_overrides.clear()


def _headers(role: str = "data_admin", actor: str = "tester") -> dict:
    return {"X-Role": role, "X-Actor": actor}


class TestLedgerRoute:
    def test_preview_then_commit_qualifications_csv(self, client):
        csv_bytes = _make_csv(
            ["资质类别", "资质等级", "有效期至"],
            [["建筑工程施工总承包", "特级", "2028-12-31"], ["市政公用工程", "一级", "2029-06-30"]],
        )
        # preview
        resp = client.post("/api/v1/enterprise/import/preview",
                           headers=_headers(), files={"file": ("q.csv", csv_bytes, "text/csv")},
                           data={"kind": "qualifications"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["preview"]["row_count"] == 2
        assert body["preview"]["headers"] == ["资质类别", "资质等级", "有效期至"]
        assert body["preview"]["unmapped_columns"] == []
        # commit
        resp = client.post("/api/v1/enterprise/import/commit",
                           headers=_headers(), files={"file": ("q.csv", csv_bytes, "text/csv")},
                           data={"kind": "qualifications",
                                 "mapping": '{"资质类别": "category", "资质等级": "level", "有效期至": "valid_until"}',
                                 "data_owner": "项目负责人", "source": "q.csv"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rows"] == 2
        assert body["import"]["created"] == 2 and body["import"]["skipped"] == 0
        # 原文不可变落库（evidence_file / enterprise），回链 material_id
        assert body["material"]["material_id"].startswith("MAT-LEDGER-")
        mat = client.engine.connect()
        try:
            m = mat.execute(
                select(Material).where(Material.material_id == body["material"]["material_id"])
            ).first()
            assert m is not None and m.material_type == "evidence_file"
            q = mat.execute(select(Qualification)).all()
            assert len(q) == 2
            assert all(x.evidence_refs and x.evidence_refs[0].startswith("ledger:csv:row")
                       for x in q)
            assert all(x.data_owner == "项目负责人" for x in q)
        finally:
            mat.close()
        # 幂等重放：created=0，全部 skipped，不产生重复主键
        resp = client.post("/api/v1/enterprise/import/commit",
                           headers=_headers(), files={"file": ("q.csv", csv_bytes, "text/csv")},
                           data={"kind": "qualifications",
                                 "mapping": '{"资质类别": "category", "资质等级": "level", "有效期至": "valid_until"}',
                                 "data_owner": "项目负责人", "source": "q.csv"})
        body = resp.json()
        assert body["import"]["created"] == 0 and body["import"]["skipped"] == 2
        with client.engine.connect() as conn:
            assert conn.execute(select(Qualification)).all().__len__() == 2

    def test_commit_xlsx_path(self, client):
        xlsx_bytes = _make_xlsx(
            ["项目名称", "项目类型", "竣工时间"],
            [["石家庄学院实训基地", "房屋建筑", "2024-05-31"]],
        )
        resp = client.post("/api/v1/enterprise/import/commit",
                           headers=_headers(), files={"file": ("p.xlsx", xlsx_bytes,
                                                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                           data={"kind": "performances",
                                 "mapping": '{"项目名称": "project_name", "项目类型": "project_type", "竣工时间": "completed_at"}',
                                 "data_owner": "项目负责人"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["import"]["created"] == 1
        with client.engine.connect() as conn:
            row = conn.execute(select(Material)).first()
            assert row is not None and row.material_type == "evidence_file"

    def test_commit_personnel_requires_category(self, client):
        csv_bytes = _make_csv(["姓名", "专业"], [["张某某", "建筑工程"]])
        resp = client.post("/api/v1/enterprise/import/commit",
                           headers=_headers(), files={"file": ("p.csv", csv_bytes, "text/csv")},
                           data={"kind": "personnel",
                                 "mapping": '{"姓名": "name", "专业": "specialty"}',
                                 "data_owner": "项目负责人", "category": "registered_builder"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["import"]["kind"] == "registered_builder"
        with client.engine.connect() as conn:
            p = conn.execute(select(Personnel)).first()
            assert p is not None and p.category == "registered_builder"
            assert p.evidence_refs[0].startswith("ledger:csv:row2")

    def test_risky_file_rejected_no_residue(self, client):
        # 外链公式 XLSX：preview/commit 均 415（unsupported_format）；不产生材料与业务行
        risky = _make_xlsx_with_external_links()
        files = {"file": ("bad.xlsx", risky, "application/octet-stream")}
        resp = client.post("/api/v1/enterprise/import/preview", headers=_headers(),
                           files=files, data={"kind": "qualifications"})
        assert resp.status_code == 415, resp.text
        assert resp.json()["error"]["code"] == "unsupported_format"
        resp = client.post("/api/v1/enterprise/import/commit", headers=_headers(),
                           files=files,
                           data={"kind": "qualifications",
                                 "mapping": '{"资质类别": "category"}',
                                 "data_owner": "项目负责人"})
        assert resp.status_code == 415, resp.text
        with client.engine.connect() as conn:
            assert conn.execute(select(Material)).all().__len__() == 0
            assert conn.execute(select(Qualification)).all().__len__() == 0

    def test_xls_old_format_preview_honest_commit_rejected(self, client):
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        files = {"file": ("old.xls", ole, "application/vnd.ms-excel")}
        resp = client.post("/api/v1/enterprise/import/preview", headers=_headers(),
                           files=files, data={"kind": "qualifications"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["preview"]["notes"] == ["xls-old-format"]
        resp = client.post("/api/v1/enterprise/import/commit", headers=_headers(),
                           files=files,
                           data={"kind": "qualifications",
                                 "mapping": "{}", "data_owner": "项目负责人"})
        assert resp.status_code == 415, resp.text