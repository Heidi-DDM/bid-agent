# R021/F004：采集合规校验器单测（纯逻辑，无网络）
# 覆盖：manual_trigger 红线 / UA 透明性 / robots 解析与路径判定 / 单源+全局限频 / 组合校验
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from runtime.core.compliance import (
    ComplianceError,
    RateLimiter,
    RobotsRules,
    TRANSPARENT_UA,
    check_fetch_allowed,
    normalize_url,
    parse_robots,
    require_manual_trigger,
    ua_is_transparent,
)


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# ---------- manual_trigger ----------

def test_require_manual_trigger_ok():
    require_manual_trigger("manual_trigger")
    require_manual_trigger(None)  # 默认 manual_trigger
    require_manual_trigger("")    # 空 = manual_trigger


def test_require_manual_trigger_rejects_scheduled():
    with pytest.raises(ComplianceError, match="manual_trigger"):
        require_manual_trigger("scheduled")


# ---------- UA 透明性 ----------

def test_transparent_ua_is_transparent():
    assert ua_is_transparent(TRANSPARENT_UA)
    assert not ua_is_transparent("Mozilla/5.0 (Macintosh) Chrome/120.0")
    assert not ua_is_transparent("googlebot/2.1")


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


# ---------- 限频 ----------

def test_rate_limiter_single_source_5min():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    limiter = RateLimiter(now=lambda: now)
    limiter.check("ggzy.hebei.gov.cn", record=True)
    # 同一源 5 分钟内再抓 → 拒绝
    with pytest.raises(ComplianceError, match="单源限频"):
        limiter.check("ggzy.hebei.gov.cn", record=True)
    # 不同源不受单源限制（record=False 探测）
    limiter.check("szj.hebei.gov.cn", record=False)


def test_rate_limiter_single_source_expires_after_5min():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    limiter = RateLimiter(now=lambda: now)
    limiter.check("ggzy.hebei.gov.cn", record=True)
    now = now + timedelta(minutes=5, seconds=1)
    limiter.check("ggzy.hebei.gov.cn", record=True)  # 窗口滑过，允许


def test_rate_limiter_global_200_per_hour():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    limiter = RateLimiter(now=lambda: now)
    for i in range(200):
        limiter.check(f"s{i}.gov.cn", record=True)  # 200 个不同源，规避单源 5 分钟限制
    with pytest.raises(ComplianceError, match="全平台限频"):
        limiter.check("another.gov.cn", record=True)


def test_rate_limiter_global_window_slides():
    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    limiter = RateLimiter(now=lambda: now)
    for i in range(200):
        limiter.check(f"s{i}.gov.cn", record=True)
    now = now + timedelta(hours=1, seconds=1)
    limiter.check("fresh.gov.cn", record=True)  # 窗口滑过


# ---------- 组合校验 ----------

def test_check_fetch_allowed_robots_blocked():
    robots = parse_robots("User-agent: *\nDisallow: /private/\n")
    limiter = RateLimiter()
    with pytest.raises(ComplianceError, match="robots"):
        check_fetch_allowed(
            "https://ggzy.hebei.gov.cn/private/x.pdf",
            robots=robots, limiter=limiter,
        )


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


def test_check_fetch_allowed_rate_blocked_on_record():
    robots = parse_robots(None)  # 无 robots → 允许
    limiter = RateLimiter()
    url, domain = check_fetch_allowed("https://a.gov.cn/x.pdf", robots=robots, limiter=limiter)
    limiter.check(domain, record=True)
    with pytest.raises(ComplianceError, match="单源限频"):
        check_fetch_allowed("https://a.gov.cn/y.pdf", robots=robots, limiter=limiter)
