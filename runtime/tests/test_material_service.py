# F019 §6 材料持久化集成测试（真实 PostgreSQL，需要 DATABASE_URL）
# 覆盖：创建/版本化/查询、内容重复去重、raw 不可覆盖、哈希篡改阻断、审计留痕。
#
# 运行方式（在沙盒外终端，使用 .venv + 真实 PostgreSQL）：
#   source .venv/bin/activate
#   export DATABASE_URL="postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent"
#   export OBJECT_STORE_ROOT="$(pwd)/runtime/objects"
#   python -m pytest runtime/tests/test_material_service.py -m integration -v
# 注：bid_agent_dev 为 scripts/setup_local_env.sh 创建的本地默认密码；若已改密请替换，
#     切勿把 "<密码>" 占位符原样写入 DATABASE_URL（psycopg 会报 missing "=" after "<"）。
#
# 未设置 DATABASE_URL 时自动 skip（CI 默认不跑集成测试，见 quality.yml）。
import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from runtime.db.material_service import (
    HashMismatchBlocked,
    ValidationError,
    current_material,
    get_version,
    import_material,
    list_versions,
    update_metadata,
    verify_material,
)
from runtime.db.models import AuditEvent, Base, Material, MaterialVersion

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")
from runtime.tests._dbguard import integration_db_allowed
_DB_OK, _DB_WHY = integration_db_allowed()
needs_db = pytest.mark.skipif(not _DB_OK, reason=_DB_WHY)


@pytest.fixture()
def session():
    engine = create_engine(DATABASE_URL)
    # 测试间隔离：清空本服务域表（材料/版本/审计），避免上一用例残留版本号污染断言
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE public_data.materials, public_data.material_versions, "
                "audit_data.audit_events RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as s:
        yield s


@pytest.fixture()
def store(tmp_path):
    # 对象存储根目录：默认 runtime/objects（F019 §4 受控目录），测试用临时目录
    return tmp_path / "objects"


def _write_file(path, content: bytes):
    path.write_bytes(content)
    return str(path)


def _import(session, store, material_id="MAT-PUB-001", content=b"tender v1", **kw):
    # 源文件使用 .pdf（生产契约 FORBIDDEN_SUFFIXES 拒绝 .bin；测试须用合法扩展名模拟真实招标文件）
    src = store.parent / f"{material_id}.pdf"
    _write_file(src, content)
    return import_material(
        session,
        src_path=str(src),
        material_id=material_id,
        material_type="tender_document",
        source_type="uploaded",
        owner_type="public",
        classification="public",
        permission_scope="public_read",
        data_owner="数据管理员甲",
        store_root=str(store),
        **kw,
    )


@needs_db
def test_import_creates_version_1(session, store):
    result = _import(session, store)
    assert result.created is True
    material = current_material(session, "MAT-PUB-001")
    assert material is not None
    assert material.material_id == "MAT-PUB-001"
    assert material.version == 1
    assert material.parse_status == "pending"
    assert material.status == "active"
    assert material.data_owner == "数据管理员甲"
    versions = list_versions(session, "MAT-PUB-001")
    assert len(versions) == 1
    assert versions[0].version == 1
    # 对象文件已落盘
    import os as _os

    assert _os.path.isfile(_os.path.join(str(store), versions[0].object_uri))


@needs_db
def test_duplicate_content_reuses_version(session, store):
    first = _import(session, store, content=b"same content")
    second = _import(session, store, content=b"same content")
    assert first.created is True
    assert second.created is False
    assert second.duplicate_of == 1
    assert len(list_versions(session, "MAT-PUB-001")) == 1  # 不新增版本


@needs_db
def test_new_content_increments_version_and_keeps_history(session, store):
    _import(session, store, content=b"v1 content")
    _import(session, store, content=b"v2 content")
    versions = list_versions(session, "MAT-PUB-001")
    assert [v.version for v in versions] == [1, 2]  # 旧版本仍可回放
    v1 = get_version(session, "MAT-PUB-001", 1)
    v2 = get_version(session, "MAT-PUB-001", 2)
    assert v1 is not None and v2 is not None
    assert v1.content_hash != v2.content_hash
    current = current_material(session, "MAT-PUB-001")
    assert current.version == 2


@needs_db
def test_raw_not_overwritable_metadata_only(session, store):
    _import(session, store, content=b"immutable raw")
    updated = update_metadata(session, "MAT-PUB-001", parse_status="parsed", actor="解析服务")
    assert updated is not None
    material = current_material(session, "MAT-PUB-001")
    assert material.parse_status == "parsed"
    assert material.version == 1  # 版本不变
    v1 = get_version(session, "MAT-PUB-001", 1)
    assert v1.content_hash == material.content_hash


@needs_db
def test_validation_rejects_bad_enum(session, store):
    src = store.parent / "bad.pdf"
    _write_file(src, b"x")
    with pytest.raises(ValidationError):
        import_material(
            session,
            src_path=str(src),
            material_id="MAT-PUB-BAD",
            material_type="unknown_type",  # 非法枚举
            source_type="uploaded",
            owner_type="public",
            classification="public",
            permission_scope="public_read",
            data_owner="甲",
            store_root=str(store),
        )


@needs_db
def test_verify_detects_tampering(session, store):
    result = _import(session, store, content=b"tender original")
    versions = list_versions(session, "MAT-PUB-001")
    target = store / versions[0].object_uri
    target.write_bytes(b"tampered content")  # 随机修改对象文件
    with pytest.raises(HashMismatchBlocked):
        verify_material(session, "MAT-PUB-001", store_root=str(store), actor="worker")
    # 审计事件已记录
    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action == "material.hash_mismatch" and e.outcome == "blocked" for e in events)


@needs_db
def test_verify_ok_records_audit(session, store):
    _import(session, store, content=b"clean file")
    assert verify_material(session, "MAT-PUB-001", store_root=str(store), actor="worker") is True
    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action == "material.verify" and e.outcome == "hash_ok" for e in events)


@needs_db
def test_audit_trail_on_import(session, store):
    _import(session, store, content=b"audited", actor="数据管理员甲")
    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action == "material.import" and e.actor == "数据管理员甲" for e in events)