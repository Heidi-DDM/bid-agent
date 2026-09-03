# R021/F004 §6：采集合规校验器（纯逻辑，无网络依赖，可单测）
# 红线数值单一事实源：schema §5.1-5.6 与 F004 §6.2（旧 PRD §5.4）
#   - 测试期仅 manual_trigger；scheduled 未启用（切换门槛：P0 验收 + 法务签字）
#   - robots.txt：禁止路径不采集
#   - 限频：单源 ≤1 次/5 分钟（300s）；全平台 ≤200 次/小时（3600s）
#   - UA：透明标识（含组织信息），不伪装浏览器
#   - raw 不可变：原文 hash 只增不覆盖（F003 §4.2，由 material_service 保证，
#     本模块提供 collect_mode/manual_trigger 校验的纯逻辑）
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# ── 红线常量（与 schema §5 / F004 §6.2 对齐） ──────────────────────────
SINGLE_SOURCE_INTERVAL = timedelta(minutes=5)   # 单源 ≤1 次/5 分钟
GLOBAL_WINDOW = timedelta(hours=1)              # 全平台窗口 1 小时
GLOBAL_MAX_CALLS = 200                          # 全平台 ≤200 次/小时

# 透明标识 UA：含组织与用途，明确不伪装（不以浏览器 UA 开头）。
# 必须 ASCII-only：HTTP header 仅允许 latin-1 编码，中文字符会在 urllib
# 序列化 header 时抛 UnicodeEncodeError（2026-09-03 真实源冒烟实测暴露）。
TRANSPARENT_UA = ("Hebei-Jianshe-BidAgent/0.1 "
                  "(internal demo system; source compliance per "
                  "docs/data-source-compliance.md)")


class ComplianceError(Exception):
    """合规校验未通过（转 400/403/429 语义由调用方决定）。"""


# ── manual_trigger（F004 §6.1 / §6.2） ─────────────────────────────────

def require_manual_trigger(collect_mode: str | None) -> None:
    """测试期仅 manual_trigger；scheduled 未启用（F004 §6.1）。"""
    if collect_mode is None or collect_mode == "":
        collect_mode = "manual_trigger"
    if collect_mode != "manual_trigger":
        raise ComplianceError(
            f"测试期仅支持 manual_trigger（F004 红线）；收到 collect_mode={collect_mode}。"
            "scheduled 需 P0 验收（≥10 条真实公告）+ 法务《数据来源合规清单》签字后切换。"
        )


def normalize_url(url: str) -> str:
    """校验 URL 合法且为 http(s)；返回归一化（去空白尾斜杠保留路径）。"""
    url = (url or "").strip()
    if not url:
        raise ComplianceError("URL 为空")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ComplianceError(f"仅允许 http/https URL: {url}")
    return url


def ua_is_transparent(ua: str) -> bool:
    """UA 透明性断言：不得伪装成浏览器/搜索引擎爬虫（schema §5.3）。"""
    ua_l = ua.lower()
    for disguised in ("mozilla", "chrome/", "safari/", "googlebot", "bingbot"):
        if ua_l.startswith(disguised):
            return False
    return len(ua) > 0


# ── robots.txt 最小解析（schema §5.2：遵守 robots，禁止路径不采集） ─────
# 覆盖测试期所需子集：User-agent: * 下的 Allow/Disallow 精确与最长前缀匹配。
# robots.txt 内容由调用方注入（本模块不联网抓取）；缺失/空文本视为无限制。


@dataclass
class RobotsRules:
    allow_prefixes: list[str] = field(default_factory=list)
    disallow_prefixes: list[str] = field(default_factory=list)
    fetched: bool = False  # robots.txt 是否成功获取（False=未获取到 → 按无限制处理并记录）

    def allows(self, path: str) -> bool:
        """按 RFC 9309 近似：最长匹配优先，Allow 优先于 Disallow；默认允许。"""
        if not self.fetched:
            return True
        path = path or "/"
        best_rule: str | None = None  # None=无匹配；allow/disallow 标记
        best_len = -1
        for prefix, is_allow in list((p, True) for p in self.allow_prefixes) + list(
            (p, False) for p in self.disallow_prefixes
        ):
            if path.startswith(prefix) and len(prefix) > best_len:
                best_len = len(prefix)
                best_rule = "allow" if is_allow else "disallow"
        if best_rule is None:
            return True
        return best_rule == "allow"


def parse_robots(text: str | None, *, fetched: bool = True) -> RobotsRules:
    """解析 robots.txt 文本（仅 User-agent: * 组；其他 UA 组忽略）。

    text=None 表示 robots.txt 不可达（网络失败/无 robots）→ fetched=False 视为无限制
    （抓取动作仍受限频约束；记录 fetched=False 供审计复核）。
    """
    rules = RobotsRules(fetched=fetched)
    if text is None:
        rules.fetched = False
        return rules
    current_ua_all: bool = True  # 简化：先遇 User-agent: * 生效；非 * 组跳过
    saw_star = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("user-agent:"):
            agent = line.split(":", 1)[1].strip().lower()
            if agent == "*":
                saw_star = True
                current_ua_all = True
            elif not saw_star:
                current_ua_all = False  # 首组为具名 UA（忽略其规则，测试期无具名声明）
            continue
        if not current_ua_all:
            continue
        if line.lower().startswith("allow:"):
            rules.allow_prefixes.append(line.split(":", 1)[1].strip() or "/")
        elif line.lower().startswith("disallow:"):
            rules.disallow_prefixes.append(line.split(":", 1)[1].strip() or "")
    # Disallow: 空串 = 全部允许（无意义前缀，剔除）
    rules.disallow_prefixes = [p for p in rules.disallow_prefixes if p]
    return rules


# ── 限频（schema §5.2：单源 ≤1 次/5 分钟、全平台 ≤200 次/小时） ────────


class RateLimiter:
    """内存限频器（单 worker/进程测试期足够；多实例/正式期换 Redis 等共享存储）。

    记录每个抓取事件时间戳；check(domain) 违反红线抛 ComplianceError。
    """

    def __init__(self, *, now=None) -> None:
        self._events: list[tuple[datetime, str | None]] = []  # (time, domain)
        self._now = now or (lambda: datetime.now(timezone.utc))  # type: ignore[assignment]

    def _ts(self) -> datetime:
        t = self._now()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t

    def check(self, domain: str | None, *, record: bool = True) -> None:
        """校验单源与全局限频；record=True 时记录本次事件（调用方确认抓取后调用）。"""
        now = self._ts()
        cutoff_global = now - GLOBAL_WINDOW
        cutoff_single = now - SINGLE_SOURCE_INTERVAL
        # 全局窗口计数（1 小时内 ≤200）
        global_count = sum(1 for t, _ in self._events if t >= cutoff_global)
        if global_count >= GLOBAL_MAX_CALLS:
            raise ComplianceError(
                f"全平台限频：1 小时内已 {global_count} 次（上限 {GLOBAL_MAX_CALLS}），请稍后再试"
            )
        # 单源窗口（5 分钟内 ≤1 次）
        if domain:
            recent = [t for t, d in self._events if d == domain and t >= cutoff_single]
            if recent:
                raise ComplianceError(
                    f"单源限频：{domain} 5 分钟内已抓取 1 次（上限 1 次/5 分钟，schema §5.2）"
                )
        if record:
            self._events.append((now, domain))
            # 裁剪过期事件，避免无限增长
            self._events = [(t, d) for t, d in self._events if t >= cutoff_global]

    def reset(self) -> None:
        self._events = []


# ── 组合校验（URL 入库前调用；robots 文本由调用方注入，避免本模块联网） ─


def check_fetch_allowed(
    url: str,
    *,
    collect_mode: str | None = "manual_trigger",
    robots: RobotsRules | None = None,
    limiter: RateLimiter | None = None,
) -> tuple[str, str]:
    """URL 抓取前的完整合规校验。

    返回 (normalized_url, domain)；任一红线违反抛 ComplianceError。
    顺序：manual_trigger → URL 合法性 → robots → 限频（先查不记录，抓取成功后 record）。
    """
    require_manual_trigger(collect_mode)
    normalized = normalize_url(url)
    domain = urlparse(normalized).netloc
    path = urlparse(normalized).path or "/"
    if robots is not None and not robots.allows(path):
        raise ComplianceError(f"robots.txt 禁止采集路径: {path}（schema §5.2）")
    if limiter is not None:
        limiter.check(domain, record=False)
    return normalized, domain
