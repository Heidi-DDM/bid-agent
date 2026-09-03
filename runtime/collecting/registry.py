# R004/F004：公告源注册表（manual_trigger 真实抓取的源清单）
# 单一事实源：docs/合规数据源清单.md §2（引用 schema §1.4 平台注册表）。
# 启用原则（清单 §4）：法务签字未达成 → 仅 manual_trigger 通道、仅已冻结 L1 官方平台，
# scheduled 仍是正式期切换门槛；L0（待合同）/L2（待评估）一律不注册。
# 测试期演示启用（2026-09-03 登记）：惠招标（河北交投）——L1 官方页面、公告免费免登录、
# 静态列表页可解析；接入状态见 docs/合规数据源清单.md §4 启用登记。
from __future__ import annotations

from dataclasses import dataclass

# 源对 region 搜索条件的覆盖口径：返回 True 表示该源可能含目标地区公告。
# 平台自身即为河北域平台时，region 为 None 或含"河北"视为覆盖。
_DEFAULT_REGION = "河北省"


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    name: str                    # 展示名（对齐清单 §2.2）
    base_url: str
    level: str                   # L0/L1/L2/L3（schema §1.4 分级）
    list_path: str               # 搜索列表页路径（一次任务每源仅抓 1 页：单源限频 1 次/5 分钟）
    category: str                # 列表分区事实（公告页原文结构）
    region_scope: str = _DEFAULT_REGION
    legal_note: str = "法务签字待确认；测试期仅 manual_trigger 通道启用（scheduled 未达切换门槛）"

    @property
    def list_url(self) -> str:
        return f"{self.base_url}{self.list_path}"

    def covers_region(self, region: str | None) -> bool:
        """该源是否覆盖给定 region（平台为河北域平台；不做逐条公告地区推断）。"""
        if not region:
            return True
        return self.region_scope in region or region in self.region_scope


# ── 已注册源（v1：惠招标工程类分区） ───────────────────────────────────
# 工程类公告 URL 结构（scripts/fetch_ebidding_lists.py 实测正则：/jyxx/... trade.html 列表页）。
SOURCES: dict[str, SourceSpec] = {
    "hebtig": SourceSpec(
        source_id="hebtig",
        name="惠招标（河北交投）",
        base_url="https://ebidding.hebtig.com",
        level="L1",
        list_path="/jyxx/001001/001001001/trade.html",
        category="招标专区-工程类",
    ),
}


def get(source_id: str) -> SourceSpec | None:
    return SOURCES.get(source_id)


def validate_sources(sources: list[str] | None) -> tuple[list[str], list[str]]:
    """校验请求中的 sources：返回 (有效, 未知)。空/None → 全部已注册源。"""
    if not sources:
        return list(SOURCES), []
    valid, unknown = [], []
    for sid in sources:
        (valid if sid in SOURCES else unknown).append(sid)
    return valid, unknown