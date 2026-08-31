# F018 §4.3 / F019 §4：对象存储流程测试（纯标准库，无第三方依赖）
import os
import tempfile
from pathlib import Path

import pytest

from runtime.core import objects
from runtime.core.objects import HashMismatchError, ObjectStoreError, ValidationError

OWNER = "enterprise_data"
MATERIAL = "qual-001"


@pytest.fixture()
def store_root():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture()
def sample_file():
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"fake pdf content " * 100)
        name = f.name
    yield name
    os.unlink(name)


def test_ingest_creates_immutable_path(store_root, sample_file):
    result = objects.ingest(
        sample_file, owner_type=OWNER, material_id=MATERIAL, version=1, store_root=store_root
    )
    assert result.created is True
    assert "enterprise_data/qual-001/1/" in result.stored.object_uri
    assert result.stored.content_hash == objects.sha256_file(sample_file)
    assert Path(result.stored.object_uri).is_file()


def test_ingest_duplicate_returns_existing(store_root, sample_file):
    r1 = objects.ingest(sample_file, owner_type=OWNER, material_id=MATERIAL, version=1, store_root=store_root)
    r2 = objects.ingest(sample_file, owner_type=OWNER, material_id=MATERIAL, version=1, store_root=store_root)
    assert r1.created is True
    assert r2.created is False
    assert r2.duplicate_of == r1.stored.object_uri
    assert r2.stored.content_hash == r1.stored.content_hash


def test_ingest_rejects_forbidden_type(store_root):
    with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
        f.write(b"MZ")
        name = f.name
    try:
        with pytest.raises(ValidationError):
            objects.ingest(name, owner_type=OWNER, material_id=MATERIAL, version=1, store_root=store_root)
    finally:
        os.unlink(name)


def test_ingest_rejects_unknown_material_type(store_root, sample_file):
    with pytest.raises(ValidationError):
        objects.ingest(
            sample_file, owner_type=OWNER, material_id=MATERIAL, version=1,
            store_root=store_root, material_type="secret",
        )


def test_verify_object_ok_and_tampered(store_root, sample_file):
    result = objects.ingest(sample_file, owner_type=OWNER, material_id=MATERIAL, version=1, store_root=store_root)
    assert objects.verify_object(store_root, result.stored.object_uri) is True
    # 篡改对象文件 -> 哈希校验失败（F019 §6）
    path = Path(result.stored.object_uri)
    with open(path, "ab") as f:
        f.write(b"tampered")
    assert objects.verify_object(store_root, result.stored.object_uri) is False


def test_virus_check_hook_called(store_root, sample_file):
    calls = []

    def _check(path: str):
        calls.append(path)

    objects.ingest(
        sample_file, owner_type=OWNER, material_id=MATERIAL, version=1,
        store_root=store_root, virus_check=_check,
    )
    assert len(calls) == 1


def test_build_object_uri():
    uri = objects.build_object_uri("enterprise_data", "qual-001", 2, "abc123")
    assert uri == "enterprise_data/qual-001/2/abc123"


def test_clean_tmp_files(store_root):
    (Path(store_root) / ".tmp-1-qual-dead").write_text("x")
    (Path(store_root) / ".tmp-2-qual-dead").write_text("y")
    (Path(store_root) / "keep.txt").write_text("z")
    assert objects.clean_tmp_files(store_root) == 2
    assert (Path(store_root) / "keep.txt").exists()