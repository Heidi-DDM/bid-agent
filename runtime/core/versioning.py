# F019 §4/§6：材料版本管理纯逻辑（无第三方依赖，本机可验证）
# 规则（F003 §4.2 / F019 §3）：
# - 内容重复（content_hash 已存在）-> 返回既有版本，不新建（F019 §4）；
# - 内容变化 -> 版本递增（max + 1），旧版本保留可回放；
# - raw 不可覆盖：version/content_hash 只增不改；
# - 有效期过期 -> expired，不得用于满分判定（F003 §4.2）。
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

ACTIVE = "active"
EXPIRED = "expired"
ARCHIVED = "archived"
INVALID = "invalid"


@dataclass(frozen=True)
class VersionDecision:
    version: int
    created: bool  # False = 内容重复，复用既有版本
    existing_version: int | None  # created=False 时的既有版本号


def decide_version(
    existing_hashes: dict[int, str],
    content_hash: str,
) -> VersionDecision:
    """根据既有版本哈希表决定新版本号。

    existing_hashes: {version: content_hash}，按版本升序。
    内容重复 -> 返回既有版本号（created=False）；否则 max+1（首个版本为 1）。
    """
    for version, digest in existing_hashes.items():
        if digest == content_hash:
            return VersionDecision(version=version, created=False, existing_version=version)
    next_ver = max(existing_hashes) + 1 if existing_hashes else 1
    return VersionDecision(version=next_ver, created=True, existing_version=None)


def material_status(valid_until: date | None, today: date) -> str:
    """有效期状态判定（F003 §4.2）：过期 -> expired；无有效期 -> active（证书类由导入方校验必填）。"""
    if valid_until is None:
        return ACTIVE
    return EXPIRED if valid_until < today else ACTIVE


def ensure_version_immutable(version: int, max_allowed: int) -> None:
    """raw 不可覆盖（F003 §4.2）：禁止对已有版本的 content/version 做更新。

    仅允许追加新版本（version == max_allowed 的后续版本）或查询旧版本；
    对已有版本行的更新必须走新版本，而不是覆盖。
    """
    if version <= max_allowed:
        raise ValueError(f"raw 不可覆盖：版本 {version} 已存在，只能新增更高版本（当前最高 {max_allowed}）")