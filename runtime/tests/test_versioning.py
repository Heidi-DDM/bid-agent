# F019 §4/§6 版本管理纯逻辑测试（无第三方依赖，本机/CI 均可执行）
import datetime as _dt

from runtime.core import versioning


def test_first_import_version_1():
    decision = versioning.decide_version({}, "hash-a")
    assert decision.version == 1
    assert decision.created is True
    assert decision.existing_version is None


def test_new_content_increments_version():
    decision = versioning.decide_version({1: "hash-a", 2: "hash-b"}, "hash-c")
    assert decision.version == 3
    assert decision.created is True


def test_duplicate_content_reuses_existing_version():
    decision = versioning.decide_version({1: "hash-a", 2: "hash-b"}, "hash-b")
    assert decision.created is False
    assert decision.existing_version == 2
    assert decision.version == 2  # 不新建版本


def test_duplicate_across_gap_versions():
    # 版本 1 与 5 内容相同 -> 复用 1，不产生新版本
    decision = versioning.decide_version({1: "hash-a", 5: "hash-z"}, "hash-a")
    assert decision.created is False
    assert decision.existing_version == 1


def test_status_active_when_valid():
    today = _dt.date(2026, 9, 1)
    assert versioning.material_status(_dt.date(2026, 12, 31), today) == versioning.ACTIVE


def test_status_expired_when_past():
    today = _dt.date(2026, 9, 1)
    assert versioning.material_status(_dt.date(2026, 8, 31), today) == versioning.EXPIRED


def test_status_active_when_no_valid_until():
    assert versioning.material_status(None, _dt.date(2026, 9, 1)) == versioning.ACTIVE


def test_version_immutable_rejects_existing_version():
    try:
        versioning.ensure_version_immutable(version=2, max_allowed=2)
        raise AssertionError("应拒绝覆盖已有版本")
    except ValueError:
        pass


def test_version_immutable_allows_newer_version():
    # 新版本（max+1）不触发不可覆盖保护
    versioning.ensure_version_immutable(version=3, max_allowed=2)