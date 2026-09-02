# F018 §4.3 / F019 §4：对象存储流程
# 上传：临时目录 -> SHA-256 -> 格式/大小校验 -> 原子移动 -> 不可变对象路径
# 对象路径按 owner_type/material_id/version/sha256 组织（F019 §4），不允许指向仓库内路径。
#
# 纯逻辑模块：只依赖标准库；原子移动在 Windows 上降级为普通 os.replace（仍原子）。
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

CHUNK_SIZE = 1024 * 1024

# 允许的对象类型（F019 §4；与 runtime/db/material_service.MATERIAL_TYPES 对齐，
# 否则 qualification_cert/performance_record/personnel_cert/evidence_file 等企业资料
# 在 ingest 层会被误拒，违反 F019 §6"F006-F009 对象均可创建"。）
ALLOWED_MATERIAL_TYPES = {
    "announcement", "tender_document", "qualification_cert",
    "performance_record", "personnel_cert", "evidence_file",
    "evidence", "other",  # 兼容早期调用方
}

# 常见恶意/可执行扩展名，一律拒绝（病毒/格式检查入口）
FORBIDDEN_SUFFIXES = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".bat", ".cmd", ".com",
    ".sh", ".scr", ".ps1", ".vbs", ".jar", ".class", ".py", ".pyc", ".js",
    ".html", ".htm", ".svg", ".apk", ".ipa",
}

# 大小上限（F018 未定具体值，先按 200MB）
MAX_SIZE_BYTES = 200 * 1024 * 1024


class ObjectStoreError(Exception):
    pass


class ValidationError(ObjectStoreError):
    pass


class HashMismatchError(ObjectStoreError):
    pass


@dataclass
class StoredObject:
    object_uri: str
    content_hash: str
    size_bytes: int
    version: int = 1
    material_type: str = "other"
    extra: dict = field(default_factory=dict)


@dataclass
class UploadResult:
    stored: StoredObject
    created: bool  # False = 内容重复，返回既有版本（F019 §4）
    duplicate_of: Optional[str] = None


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_object_uri(owner_type: str, material_id: str, version: int, content_hash: str) -> str:
    """对象路径：owner_type/material_id/version/sha256（F019 §4）。"""
    return f"{owner_type}/{material_id}/{version}/{content_hash}"


def _is_inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def validate_input(
    src_path: str | os.PathLike,
    *,
    material_type: str = "other",
    max_size: int = MAX_SIZE_BYTES,
) -> int:
    """格式/大小/扩展名检查；返回文件大小。"""
    path = Path(src_path)
    if not path.is_file():
        raise ValidationError(f"文件不存在: {path.name}")
    if material_type not in ALLOWED_MATERIAL_TYPES:
        raise ValidationError(f"不支持的对象类型: {material_type}")
    suffix = path.suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES:
        raise ValidationError(f"禁止的文件类型: {suffix}")
    size = path.stat().st_size
    if size <= 0:
        raise ValidationError("空文件")
    if size > max_size:
        raise ValidationError(f"文件过大: {size} > {max_size}")
    return size


def ingest(
    src_path: str | os.PathLike,
    *,
    owner_type: str,
    material_id: str,
    version: int,
    store_root: str | os.PathLike,
    material_type: str = "other",
    virus_check: Optional[Callable[[str], None]] = None,
) -> UploadResult:
    """将文件转入不可变对象路径。

    流程（F018 §4.3 / F019 §4）：
    1. 格式/大小/扩展名校验；
    2. 计算 SHA-256；
    3. 病毒检查钩子（本地/内网杀毒，未配置则跳过）；
    4. 内容重复 -> 返回既有版本（created=False）；
    5. 原子写入临时文件 -> os.replace 到最终路径。
    """
    src = Path(src_path)
    size = validate_input(src, material_type=material_type)
    digest = sha256_file(src)

    root = Path(store_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    rel = build_object_uri(owner_type, material_id, version, digest)
    final = root / rel
    if final.exists():
        # 内容重复：返回既有版本，不新建
        return UploadResult(
            stored=StoredObject(object_uri=rel, content_hash=digest, size_bytes=size,
                                version=version, material_type=material_type),
            created=False,
            duplicate_of=rel,
        )

    if virus_check is not None:
        virus_check(str(src))

    # 原子写入：临时文件 -> os.replace（F018 §4.3 先写临时文件）
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = root / f".tmp-{os.getpid()}-{material_id}-{digest[:12]}"
    try:
        with open(src, "rb") as fin, open(tmp_path, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=CHUNK_SIZE)
        os.replace(tmp_path, final)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return UploadResult(
        stored=StoredObject(object_uri=rel, content_hash=digest, size_bytes=size,
                            version=version, material_type=material_type),
        created=True,
    )


def verify_object(root: str | os.PathLike, object_uri: str) -> bool:
    """哈希复核（F019 §6：随机修改对象文件后哈希校验失败）。"""
    path = Path(root) / object_uri
    if not path.is_file():
        return False
    try:
        actual = sha256_file(path)
    except OSError:
        return False
    # object_uri 形如 owner/material/version/sha256
    parts = path.parts
    if len(parts) < 3:
        return False
    expected = parts[-1]
    return actual == expected


def clean_tmp_files(store_root: str | os.PathLike) -> int:
    """清理未完成的 .tmp-* 文件（F018 §7.6 验收结束后清理临时文件）。"""
    root = Path(store_root)
    if not root.is_dir():
        return 0
    removed = 0
    for f in root.iterdir():
        if f.is_file() and f.name.startswith(".tmp-"):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed