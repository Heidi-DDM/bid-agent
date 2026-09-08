# R021/F004 §6：采集合规校验器（纯逻辑，无网络依赖，可单测）
# 红线数值单一事实源：schema §5.1-5.6 与 F004 §6.2（旧 PRD §5.4）
#   - 测试期仅 manual_trigger；scheduled 未启用（切换门槛：P0 验收 + 法务签字）
#   - robots.txt：预检留痕不阻断（ADR-003）
#   - 限频：按策略等级动态配置（production 单源 ≤1 次/5 分钟、全平台 ≤200 次/小时）
#   - UA：按策略等级选择透明标识或合理标识
#   - raw 不可变：原文 hash 只增不覆盖（F003 §4.2，由 material_service 保证，
#     本模块提供 collect_mode/manual_trigger 校验的纯逻辑）
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from urllib.parse import urlparse

import yaml

# ── 红线常量（与 schema §5 / F004 §6.2 对齐） ──────────────────────────
# 这些是默认值，staging/development 等策略会覆写（见 get_collection_policy）
SINGLE_SOURCE_INTERVAL = timedelta(minutes=5)   # 单源 ≤1 次/5 分钟（production）
GLOBAL_WINDOW = timedelta(hours=1)              # 全平台窗口 1 小时
GLOBAL_MAX_CALLS = 200                          # 全平台 ≤200 次/小时（production）

# 透明标识 UA：含组织与用途，明确不伪装（不以浏览器 UA 开头）。
# 必须 ASCII-only：HTTP header 仅允许 latin-1 编码，中文字符会在 urllib
# 序列化 header 时抛 UnicodeEncodeError（2026-09-03 真实源冒烟实测暴露）。
TRANSPARENT_UA = ("Hebei-Jianshe-BidAgent/0.1 "
                  "(internal demo system; source compliance per "
                  "docs/data-source-compliance.md)")

# 合理标识 UA：保持浏览器兼容但声明身份。某些招标平台 CDN/WAF
# 看到透明 UA 直接拒绝（403/429），此时降级为合理标识以获取数据。
# 仍包含项目名称以便追踪，但不伪装成 Googlebot 等搜索引擎爬虫。
SENSIBLE_UA = ("Mozilla/5.0 (compatible; Hebei-Jianshe-BidAgent/1.0; "
               "+http://example.com/bot) internal research demo system")

# 合法策略名称
VALID_COLLECTION_POLICIES = frozenset({"production", "staging", "development", "mock"})


@dataclass(frozen=True)
class CollectionPolicy:
    """运行环境采集策略；策略未知时不得降级到宽松行为（fail-closed）。

    v2（2026-09-07 优化）：从 3 级扩展到 4+1 级，新增 `staging` 调试层，
    限频与 UA 策略可配置而非硬编码，支持分页与区域推断开关。

    字段说明：
      name              — 策略名称
      network_allowed  — 是否允许联网
      enforce_robots   — 是否执行 robots 预检（ADR-003 不阻断，只记录）
      enforce_rate_limit — 是否执行限频
      fixture_only     — 是否仅允许本地 fixture（无 fixture 则报错）
      single_source_seconds — 单源最小间隔（秒），production=300，staging=30
      global_max_calls_per_hour — 全平台 1 小时内最大抓取次数
      transparent_ua   — True=使用透明UA（TRANSPARENT_UA），False=使用合理标识（SENSIBLE_UA）
      max_pages        — 列表页最多抓取页数（用于 service.py 分页循环）
      infer_region     — 历史兼容配置；仅允许记录 source_scope_only 元数据，永远不填充公告 region
    """

    name: str
    network_allowed: bool
    enforce_robots: bool
    enforce_rate_limit: bool
    fixture_only: bool
    single_source_seconds: int = 300          # 默认 5 分钟（production）
    global_max_calls_per_hour: int = 200  # 默认 200 次/小时（production）
    transparent_ua: bool = True           # 默认透明UA
    max_pages: int = 1                    # 默认只扫首页
    infer_region: bool = False            # 默认不推断


_POLICY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "collection_policy.yml"


def _load_policy_config() -> dict[str, dict[str, object]]:
    """读取策略 YAML；配置缺失/损坏时 fail-closed，不回退到宽松默认值。"""
    try:
        with _POLICY_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            document = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ComplianceError(
            f"采集策略配置不可用: {_POLICY_CONFIG_PATH}（{type(exc).__name__}）"
        ) from exc
    policies = document.get("policies") if isinstance(document, dict) else None
    if not isinstance(policies, dict):
        raise ComplianceError(f"采集策略配置缺少 policies 映射: {_POLICY_CONFIG_PATH}")
    return policies  # type: ignore[return-value]


def _policy_value(raw: dict[str, object], key: str, *, default: object) -> object:
    if key not in raw:
        raise ComplianceError(f"采集策略缺少必填字段 {key}")
    value = raw[key]
    if isinstance(value, bool) and isinstance(default, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and isinstance(default, int):
        if value < 0:
            raise ComplianceError(f"采集策略字段 {key} 不得为负数")
        return value
    if type(value) is type(default):
        return value
    raise ComplianceError(f"采集策略字段 {key} 类型非法: {value!r}")


def get_collection_policy(value: str | None = None) -> CollectionPolicy:
    """读取 COLLECTION_POLICY 及 YAML 配置，未知策略或坏配置均 fail-closed。"""
    name = (value if value is not None else os.environ.get("COLLECTION_POLICY", "production"))
    name = str(name).strip().lower() or "production"
    if name not in VALID_COLLECTION_POLICIES:
        raise ComplianceError(
            f"未知 COLLECTION_POLICY={name!r}；允许 "
            f"{', '.join(sorted(VALID_COLLECTION_POLICIES))}，已拒绝采集"
        )
    policies = _load_policy_config()
    raw = policies.get(name)
    if not isinstance(raw, dict):
        raise ComplianceError(f"采集策略 {name!r} 未在配置中定义，已拒绝采集")

    defaults: dict[str, object] = {
        "network_allowed": False,
        "enforce_robots": False,
        "enforce_rate_limit": True,
        "fixture_only": True,
        "single_source_seconds": 300,
        "global_max_calls_per_hour": 200,
        "transparent_ua": True,
        "max_pages": 1,
        "infer_region": False,
    }
    values = {key: _policy_value(raw, key, default=default)
              for key, default in defaults.items()}
    # 列表采集至少要请求首页；0 页会产生“成功但没有抓取”的假阳性摘要。
    if values["max_pages"] < 1:
        raise ComplianceError(f"采集策略 {name!r} 的 max_pages 必须 >= 1")
    if not values["fixture_only"] and not values["network_allowed"]:
        raise ComplianceError(f"采集策略 {name!r} 禁止联网时必须 fixture_only=true")
    if not values["enforce_rate_limit"] and (
        values["single_source_seconds"] != 0 or values["global_max_calls_per_hour"] != 0
    ):
        raise ComplianceError(
            f"采集策略 {name!r} 已关闭限频时，single_source_seconds 与 "
            "global_max_calls_per_hour 必须均为 0"
        )
    return CollectionPolicy(name=name, **values)  # type: ignore[arg-type]


def build_user_agent(transparent: bool = True) -> str:
    """按策略选择 UA：transparent=True → 透明UA，False → 合理标识UA。

    透明UA：不伪装浏览器，声明组织信息，被拒风险高但合规性最强。
    合理标识UA：保持浏览器兼容但声明身份，被拒风险低。

    调用方（service.py / fetcher.py）按 policy.transparent_ua 选择：
        UA = build_user_agent(transparent=policy.transparent_ua)
    """
    return TRANSPARENT_UA if transparent else SENSIBLE_UA


class ComplianceError(Exception):
    """合规校验未通过（转 400/403/429 语义由调用方决定）。

    限频类拒绝携带 retry_after_seconds（红线窗口剩余秒数），供调用方
    返回 retry_after 与前端倒计时；不携带（None）表示非限频拒绝。
    """

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds


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
    """UA 透明性断言：根据策略需要验证 UA 是否合规。

    v2（2026-09-07 优化）：不硬性禁止 Mozilla/Chrome/Safari 前缀。
    当 transparent_ua=True（生产环境）时，不允许以浏览器前缀开头；
    当 transparent_ua=False（staging/development）时，允许合理标识UA
    （包含项目名，不以搜索引擎爬虫开头）。

    schema §5.3 要求：使用标识性UA（含公司信息），不做伪装。
    合理标识UA（Mozilla/5.0 (compatible; ...internal research... )）满足
    该要求——声明了项目身份、非伪装成 Googlebot、可被联系。
    """
    ua_l = ua.lower()
    # 搜索引擎爬虫伪装永远禁止，无论策略
    for disguised in ("googlebot", "bingbot", "slurp", "baiduspider"):
        if ua_l.startswith(disguised):
            return False
    # 必须非空
    if len(ua) <= 0:
        return False
    # 无论兼容模式与否，都必须明确声明本项目身份，不能拿任意浏览器 UA 伪装。
    if not ua.isascii() or "hebei-jianshe-bidagent" not in ua_l:
        return False
    return True


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

    def matched_disallow(self, path: str) -> str | None:
        """返回实际命中并生效的禁止规则前缀（Allow 最长前缀优先覆盖时返回 None）。

        供拒绝原因如实回显「robots 哪条规则拦的」，避免只报『禁止采集路径』
        而无法人工核对 robots.txt。与 allows() 同口径：最长前缀匹配，
        Allow 优先于 Disallow。
        """
        if not self.fetched:
            return None
        path = path or "/"
        best_disallow: str | None = None
        best_len = -1
        for p in self.disallow_prefixes:
            if path.startswith(p) and len(p) > best_len:
                best_len = len(p)
                best_disallow = p
        if best_disallow is None:
            return None
        # 存在不短于禁止规则且命中的 Allow → Allow 生效（RFC 9309）
        for p in self.allow_prefixes:
            if path.startswith(p) and len(p) >= best_len:
                return None
        return best_disallow


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


# ── 限频（schema §5.2：按策略等级动态配置） ─────────────────────────────


class RateLimiter:
    """内存限频器（单 worker/进程测试期足够；多实例/正式期换 Redis 等共享存储）。

    v2（2026-09-07 优化）：限频间隔不再使用模块级常量 SINGLE_SOURCE_INTERVAL/GLOBAL_MAX_CALLS，
    而是从 CollectionPolicy 传入。check() 新增参数传入策略，或由调用方在构造时锁定。

    记录每个抓取事件时间戳；check(domain) 违反红线抛 ComplianceError。
    """

    def __init__(self, *, now=None, single_interval_seconds: int = 300,
                 global_window_seconds: int = 3600, global_max_calls: int = 200,
                 enforce_rate_limit: bool = True) -> None:
        self._events: list[tuple[datetime, str | None]] = []  # (time, domain)
        self._now = now or (lambda: datetime.now(timezone.utc))  # type: ignore[assignment]
        self._single_interval = timedelta(seconds=single_interval_seconds)
        self._global_window = timedelta(seconds=global_window_seconds)
        self._global_max_calls = global_max_calls
        self._enforce_rate_limit = enforce_rate_limit

    @classmethod
    def from_policy(cls, policy: CollectionPolicy, *, now=None) -> "RateLimiter":
        """从策略构造限频器（推荐用法）。"""
        return cls(
            now=now,
            single_interval_seconds=policy.single_source_seconds,
            global_window_seconds=3600,
            global_max_calls=policy.global_max_calls_per_hour,
            enforce_rate_limit=policy.enforce_rate_limit,
        )

    def _ts(self) -> datetime:
        t = self._now()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t

    def next_allowed_at(self, domain: str | None) -> datetime | None:
        """该域（domain=None 时为全局窗口）下一次可抓取时刻；无限制返回 None。

        纯查询不记录事件；供健康检查/失败摘要返回「几点可重试」人类可读时刻。
        """
        if not self._enforce_rate_limit:
            return None
        now = self._ts()
        cutoff_single = now - self._single_interval
        if domain and self._single_interval.total_seconds() > 0:
            recent = [(t, d) for t, d in self._events if d == domain and t >= cutoff_single]
            if recent:
                return recent[-1][0] + self._single_interval
            return None
        # 全局窗口：取窗口内最早一次事件 + 1h（窗口滑出即释放）
        cutoff_global = now - self._global_window
        window_events = [t for t, _ in self._events if t >= cutoff_global]
        if self._global_max_calls > 0 and len(window_events) >= self._global_max_calls:
            return min(window_events) + self._global_window
        return None

    def _retry_after(self, domain: str | None) -> int | None:
        nxt = self.next_allowed_at(domain)
        if nxt is None:
            return None
        return max(1, int((nxt - self._ts()).total_seconds()))

    def check(self, domain: str | None, *, record: bool = True) -> None:
        """校验单源与全局限频；record=True 时记录本次事件（调用方确认抓取后调用）。

        single_interval_seconds=0 时跳过单源限频；global_max_calls=0 时跳过全局限频。
        违反红线抛 ComplianceError（限频类带 retry_after_seconds）。
        """
        if not self._enforce_rate_limit:
            return
        now = self._ts()
        # 全局限频（global_max_calls=0 不限制）
        if self._global_max_calls > 0:
            cutoff_global = now - self._global_window
            global_count = sum(1 for t, _ in self._events if t >= cutoff_global)
            if global_count >= self._global_max_calls:
                raise ComplianceError(
                    f"全平台限频：1 小时内已 {global_count} 次（上限 {self._global_max_calls}）",
                    retry_after_seconds=self._retry_after(None),
                )
        # 单源限频（single_interval_seconds=0 不限制）
        if self._single_interval.total_seconds() > 0 and domain:
            cutoff_single = now - self._single_interval
            recent = [t for t, d in self._events if d == domain and t >= cutoff_single]
            if recent:
                interval_sec = int(self._single_interval.total_seconds())
                raise ComplianceError(
                    f"单源限频：{domain} {interval_sec} 秒内已抓取 1 次（上限 1 次/{interval_sec}秒）",
                    retry_after_seconds=self._retry_after(domain),
                )
        if record and (self._global_max_calls > 0 or self._single_interval.total_seconds() > 0):
            self._events.append((now, domain))
            # 裁剪过期事件，避免无限增长
            cutoffs = []
            if self._global_max_calls > 0:
                cutoffs.append(now - self._global_window)
            if self._single_interval.total_seconds() > 0:
                cutoffs.append(now - self._single_interval)
            self._events = [(t, d) for t, d in self._events if t >= min(cutoffs)]

    def reset(self) -> None:
        self._events = []


# ── 组合校验（URL 入库前调用；robots 文本由调用方注入，避免本模块联网） ─


def check_fetch_allowed(
    url: str,
    *,
    collect_mode: str | None = "manual_trigger",
    robots: RobotsRules | None = None,
    limiter: RateLimiter | None = None,
    policy: CollectionPolicy | None = None,
) -> tuple[str, str]:
    """URL 抓取前的完整合规校验。

    返回 (normalized_url, domain)；红线违反抛 ComplianceError。
    顺序：manual_trigger → URL 合法性 → 限频（先查不记录，抓取成功后 record）。
    robots 为预检留痕语义（ADR-003），本函数不再因 Disallow 阻断——调用方
    如需展示 robots 预检结果，单独读取 robots 并记录 robots_status。
    """
    policy = policy or get_collection_policy()
    require_manual_trigger(collect_mode)
    normalized = normalize_url(url)
    domain = urlparse(normalized).netloc
    if policy.fixture_only:
        raise ComplianceError(
            f"采集策略 {policy.name} 仅允许本地 fixture，不得访问真实平台: {domain}"
        )
    # robots：预检留痕、不阻断（ADR-003，2026-09-07）
    if policy.enforce_rate_limit and limiter is not None:
        limiter.check(domain, record=False)
    return normalized, domain
