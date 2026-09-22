# 台账在施状态一致性回归（2026-09-22 项目经理M实测矛盾修复后新增）
# 规则：主表「在施状态」=否 → 不采纳主表遗留/Sheet1 的在施项目名（两源冲突以主表为准，
# 计入 stats.onsite_status_project_conflicts，不静默）；=是 → 项目名保留；空+Sheet1 有项目 → occupied。
import importlib.util
import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "import_enterprise_ledgers", ROOT / "scripts" / "import_enterprise_ledgers.py")
iel = importlib.util.module_from_spec(_SPEC)
sys.modules["import_enterprise_ledgers"] = iel
_SPEC.loader.exec_module(iel)

# 主表列序对齐真实台账（仅本用例涉及的列有值即可）
HEADER = ["姓名", "建筑专业", "市政专业", "机电专业", "公路专业", "铁路专业", "民航专业",
          "水利水电专业", "港口与航道专业", "矿业专业", "通信专业", "等级", "联系电话",
          "注册编号", "在施状态(是/否)", "在施项目名称", "在施项目当前进度%（不用填）",
          "在施项目预计完工日（不用填）", None]


def _workbook(tmp_path, main_rows, sheet1_rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "注册建造师清单（含在施状态）"
    ws.append(HEADER)
    for r in main_rows:
        ws.append(r + [None] * (len(HEADER) - len(r)))
    ws1 = wb.create_sheet("Sheet1")
    ws1.append(["姓名", "在施状态(是/否)", "在施项目名称", "进度", "预计完工"])
    for r in sheet1_rows:
        ws1.append(r)
    path = tmp_path / "builders.xlsx"
    wb.save(path)
    return path


@pytest.fixture()
def patched_ledger(tmp_path, monkeypatch):
    def _make(main_rows, sheet1_rows):
        monkeypatch.setattr(iel, "F_BUILDER", _workbook(tmp_path, main_rows, sheet1_rows))
    return _make


def test_not_onsite_never_carries_project(patched_ledger):
    # 项目经理M场景：主表=否、无项目；Sheet1 却有项目名 → 可用且不带项目，冲突计数 1
    patched_ledger(
        main_rows=[
            ["项目经理M", "建筑", None, None, None, None, None, None, None, None, None,
             "一级建造师", "后续补充", "后续补充", "否", None],
            ["张三", "建筑", None, None, None, None, None, None, None, None, None,
             "一级建造师", "后续补充", "后续补充", "是", "某项目施工"],
        ],
        sheet1_rows=[["项目经理M", "保定市蔬菜育种基地项目设计施工总承包（二次）", "2024-12-25", "后续补充", "后续补充"]],
    )
    personnel, managers, stats = iel.build_builder_rows()
    assert [m["display_name"] for m in managers] == ["毛*·0002", "张*·0003"]
    assert managers[0]["availability"] == "available"
    assert managers[0]["active_projects"] == []
    assert managers[1]["availability"] == "occupied"
    assert managers[1]["active_projects"] == ["某项目施工"]
    assert stats["onsite_status_project_conflicts"] == 1
    # personnel 同步：否不带项目
    pnl = next(p for p in personnel if p["name"] == "项目经理M")
    assert pnl["on_site"] == "否" and pnl["on_site_project"] is None


def test_blank_status_adopts_sheet1_project_as_occupied(patched_ledger):
    # 主表状态空 + Sheet1 明确在施项目 → occupied + 项目（有据判定，非推断）
    patched_ledger(
        main_rows=[
            ["李四", "建筑", None, None, None, None, None, None, None, None, None,
             "一级建造师", None, None, None, None],
        ],
        sheet1_rows=[["李四", "某在建项目通风工程", "2024-12-25", "后续补充", "后续补充"]],
    )
    _, managers, stats = iel.build_builder_rows()
    assert managers[0]["availability"] == "occupied"
    assert managers[0]["active_projects"] == ["某在建项目通风工程"]
    assert stats["onsite_status_project_conflicts"] == 0
