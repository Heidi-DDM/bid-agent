# R021/F004：采集合规校验器单测（纯逻辑，无网络）
# 覆盖：manual_trigger 红线 / UA 透明性 / robots 解析与路径判定 / 单源+全局限频 / 组合校验
# v2（2026-09-07）：新增 staging/development/mock 策略测试、build_user_agent、可配置限频
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from runtime.core.compliance import (
    CollectionPolicy,
    ComplianceError,
    RateLimiter,
    RobotsRules,
    TRANSPARENT_UA,
    SENSIBLE_UA,
    VALID_COLLECTION_POLICIES,
    build_user_agent,
    check_fetch_allowed,
    get_collection_policy,
    normalize_url,
    parse_robots,
    require_manual_trigger,
    ua_is_transparent,
)


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# ---------- 采集策略分级 ----------

def test_valid_policies():
    assert "production" in VALID_COLLECTION_POLICIES
    assert "staging" in VALID_COLLECTION_POLICIES
    assert "development" in VALID_COLLECTION_POLICIES
    assert "mock" in VALID_COLLECTION_POLICIES


def test_policy_production():
    p = get_collection_policy("production")
    assert p.name == "production"
    assert p.network_allowed is True
    assert p.enforce_rate_limit is True
    assert p.transparent_ua is True
    # 2026-09-08 放开：production 默认 60s/1000/h/5页（保底防封，非文档硬红线）
    assert p.max_pages == 5
    assert p.infer_region is False
    assert p.single_source_seconds == 60
    assert p.global_max_calls_per_hour == 1000


def test_policy_staging():
    p = get_collection_policy("staging")
    assert p.name == "staging"
    assert p.network_allowed is True
    assert p.enforce_rate_limit is True
    assert p.transparent_ua is False  # 合理标识UA
    assert p.max_pages == 5
    assert p.infer_region is True
    assert p.single_source_seconds == 30   # 1次/30秒
    assert p.global_max_calls_per_hour == 500


def test_policy_development():
    p = get_collection_policy("development")
    assert p.name == "development"
    assert p.network_allowed is True       # 允许联网
    assert p.enforce_rate_limit is False   # 不限频
    assert p.transparent_ua is False
    assert p.max_pages == 5
    assert p.infer_region is True


def test_policy_mock():
    p = get_collection_policy("mock")
    assert p.name == "mock"
    assert p.network_allowed is False
    assert p.fixture_only is True


def test_policy_default():
    """默认=production"""
    p = get_collection_policy(None)
    assert p.name == "production"


def test_policy_unknown_fail_closed():
    with pytest.raises(ComplianceError, match="未知 COLLECTION_POLICY"):
        get_collection_policy("hacker_mode")


def test_policy_zero_pages_fails_closed(monkeypatch):
    import runtime.core.compliance as compliance

    original = compliance._load_policy_config
    monkeypatch.setattr(
        compliance,
        "_load_policy_config",
        lambda: {
            "production": {
                "network_allowed": True,
                "enforce_robots": True,
                "enforce_rate_limit": True,
                "fixture_only": False,
                "single_source_seconds": 300,
                "global_max_calls_per_hour": 200,
                "transparent_ua": True,
                "max_pages": 0,
                "infer_region": False,
            }
        },
    )
    try:
        with pytest.raises(ComplianceError, match="max_pages.*>= 1"):
            get_collection_policy("production")
    finally:
        monkeypatch.setattr(compliance, "_load_policy_config", original)


def test_build_user_agent():
    """透明UA vs 合理标识UA"""
    assert build_user_agent(transparent=True) == TRANSPARENT_UA
    assert build_user_agent(transparent=False) == SENSIBLE_UA
    assert TRANSPARENT_UA.isascii()
    assert SENSIBLE_UA.isascii()


# ---------- manual_trigger ----------

def test_require_manual_trigger_ok():
    require_manual_trigger("manual_trigger")
    require_manual_trigger(None)  # 默认 manual_trigger
    require_manual_trigger("")    # 空 = manual_trigger


def test_require_manual_trigger_rejects_scheduled():
    with pytest.raises(ComplianceError, match="manual_trigger"):
        require_manual_trigger("scheduled")


# ---------- UA 透明性 ----------

def test_transparent_ua_check():
    """透明UA必须合规"""
    assert ua_is_transparent(TRANSPARENT_UA)
    assert ua_is_transparent(SENSIBLE_UA)   # v2：合理标识UA也合规


def test_ua_rejects_searchengine():
    """搜索引擎爬虫伪装永远禁止"""
    assert not ua_is_transparent("googlebot/2.1")
    assert not ua_is_transparent("BingBot/1.0")
    assert not ua_is_transparent("baiduspider")


def test_ua_rejects_empty():
    assert not ua_is_transparent("")


def test_ua_rejects_too_short():
    assert not ua_is_transparent("curl/7.68")


def test_transparent_ua_ascii_only():
    # HTTP header 仅 latin-1：中文 UA 会在发送请求时 UnicodeEncodeError
    # （2026-09-03 惠招标真实抓取冒烟实测暴露），UA 必须 ASCII-only。
    assert TRANSPARENT_UA.isascii()


# ---------- URL ----------

def test_normalize_url_ok():
    assert normalize_url("https://ggzy.hebei.gov.cn/a/b") == "https://ggzy.hebei.gov.cn/a/b"
    assert normalize_url("  http://x.gov.cn/  ") == "http://x.gov.cn/"


def test_normalize_url_rejects_bad():
    with pytest.raises(ComplianceError, match="URL 为空"):
        normalize_url("")
    for bad in ("ftp://x.gov.cn/f", "file:///etc/passwd", "javascript:alert(1)", "ggzy.gov.cn"):
        with pytest.raises(ComplianceError, match="http"):
            normalize_url(bad)


# ---------- robots ----------

def test_parse_robots_disallow_path():
    robots = parse_robots(
        "User-agent: *\n"
        "Disallow: /private/\n"
        "Disallow: /search\n"
        "Allow: /public/\n"
    )
    assert robots.allows("/private/x.pdf") is False
    assert robots.allows("/search?q=1") is False
    assert robots.allows("/public/announce.pdf") is True
    assert robots.allows("/other/a.html") is True


def test_parse_robots_longest_prefix_and_allow_wins():
    robots = parse_robots(
        "User-agent: *\n"
        "Disallow: /a\n"
        "Allow: /a/b\n"
    )
    # /a/b 命中最长前缀 allow；/a/c 命中 disallow
    assert robots.allows("/a/b/c") is True
    assert robots.allows("/a/c") is False


def test_parse_robots_empty_disallow_allows_all():
    robots = parse_robots("User-agent: *\nDisallow:\n")
    assert robots.allows("/anything") is True


def test_parse_robots_none_fetched_false_allows_all():
    robots = parse_robots(None)
    assert robots.fetched is False
    assert robots.allows("/x") is True


def test_parse_robots_ignores_named_agent_group():
    # 具名 UA 组规则不生效（测试期只有 * 组声明）；但 * 组后到仍生效
    robots = parse_robots(
        "User-agent: SomeBot\nDisallow: /\n"
        "User-agent: *\nDisallow: /private/\n"
    )
    assert robots.allows("/public") is True
    assert robots.allows("/private/x") is False


def test_robots_rule_direct_allows():
    # 无 robots 文本（直接构造）：allow 前缀生效
    robots = RobotsRules(allow_prefixes=["/ok"], disallow_prefixes=["/no"], fetched=True)
    assert robots.allows("/ok/1") is True
    assert robots.allows("/no/1") is False
    assert robots.allows("/other") is True


# ---------- 限频（可配置策略） ----------

def test_rate_limiter_from_policy_staging():
    """staging 限频 1次/30秒"""
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    policy = get_collection_policy("staging")
    limiter = RateLimiter.from_policy(policy, now=lambda: now)
    limiter.check("ggzy.hebei.gov.cn", record=True)
    # 30 秒内再抓同一源 → 拒绝
    with pytest.raises(ComplianceError, match="单源限频"):
        limiter.check("ggzy.hebei.gov.cn", record=True)
    # 31秒后 → 允许
    now = now + timedelta(seconds=31)
    limiter.check("ggzy.hebei.gov.cn", record=True)


def test_rate_limiter_from_policy_development():
    """development 不限频"""
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    policy = get_collection_policy("development")
    limiter = RateLimiter.from_policy(policy, now=lambda: now)
    # 连续抓同一个源10次，不限频
    for i in range(10):
        limiter.check("ggzy.hebei.gov.cn", record=True)
    assert limiter.next_allowed_at("ggzy.hebei.gov.cn") is None
    assert limiter._events == []


def test_rate_limiter_enforce_false_bypasses_even_with_nonzero_limits():
    """显式关闭限频时，check 不检查也不记录；避免靠 0 值隐式旁路。"""
    limiter = RateLimiter(
        single_interval_seconds=300,
        global_max_calls=1,
        enforce_rate_limit=False,
    )
    for _ in range(3):
        limiter.check("same.example", record=True)
    assert limiter.next_allowed_at("same.example") is None
    assert limiter._events == []


def test_rate_limiter_single_source_5min():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    limiter = RateLimiter(now=lambda: now, single_interval_seconds=300)
    limiter.check("ggzy.hebei.gov.cn", record=True)
    # 同一源 300 秒内再抓 → 拒绝
    with pytest.raises(ComplianceError, match="单源限频"):
        limiter.check("ggzy.hebei.gov.cn", record=True)
    # 不同源不受单源限制（record=False 探测）
    limiter.check("szj.hebei.gov.cn", record=False)


def test_rate_limiter_single_source_expires():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    limiter = RateLimiter(now=lambda: now, single_interval_seconds=300)
    limiter.check("ggzy.hebei.gov.cn", record=True)
    now = now + timedelta(seconds=301)
    limiter.check("ggzy.hebei.gov.cn", record=True)  # 窗口滑过，允许


def test_rate_limiter_global_200_per_hour():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    limiter = RateLimiter(now=lambda: now, global_max_calls=200)
    for i in range(200):
        limiter.check(f"s{i}.gov.cn", record=True)
    with pytest.raises(ComplianceError, match="全平台限频"):
        limiter.check("another.gov.cn", record=True)


def test_rate_limiter_global_window_slides():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    limiter = RateLimiter(now=lambda: now, global_max_calls=200)
    for i in range(200):
        limiter.check(f"s{i}.gov.cn", record=True)
    now = now + timedelta(hours=1, seconds=1)
    limiter.check("fresh.gov.cn", record=True)  # 窗口滑过


# ---------- 组合校验 ----------

def test_check_fetch_allowed_robots_precheck_not_blocking():
    # ADR-003：robots 预检留痕不阻断
    robots = parse_robots("User-agent: *\nDisallow: /private/\n")
    limiter = RateLimiter()
    url, domain = check_fetch_allowed(
        "https://ggzy.hebei.gov.cn/private/x.pdf",
        robots=robots, limiter=limiter,
    )
    assert domain == "ggzy.hebei.gov.cn"
    assert url.endswith("/private/x.pdf")


def test_check_fetch_allowed_scheduled_blocked():
    with pytest.raises(ComplianceError, match="manual_trigger"):
        check_fetch_allowed("https://ggzy.hebei.gov.cn/a.pdf", collect_mode="scheduled")


def test_check_fetch_allowed_ok():
    robots = parse_robots("User-agent: *\nDisallow: /private/\n")
    limiter = RateLimiter()
    url, domain = check_fetch_allowed(
        "https://ggzy.hebei.gov.cn/public/a.pdf",
        robots=robots, limiter=limiter,
    )
    assert domain == "ggzy.hebei.gov.cn"
    assert url.endswith("/public/a.pdf")
    limiter.check(domain, record=True)  # 抓取成功后记录


def test_check_fetch_allowed_rate_blocked():
    robots = parse_robots(None)
    limiter = RateLimiter(single_interval_seconds=300)
    url, domain = check_fetch_allowed("https://a.gov.cn/x.pdf", robots=robots, limiter=limiter)
    limiter.check(domain, record=True)
    with pytest.raises(ComplianceError, match="单源限频"):
        check_fetch_allowed("https://a.gov.cn/y.pdf", robots=robots, limiter=limiter)


def test_check_fetch_allowed_development_bypasses_limiter():
    policy = get_collection_policy("development")
    limiter = RateLimiter(single_interval_seconds=300, global_max_calls=1)
    for path in ("/a", "/b", "/c"):
        assert check_fetch_allowed(
            f"https://a.gov.cn{path}", limiter=limiter, policy=policy
        )[1] == "a.gov.cn"
