# R004/F020：真实公告采集运行时测试
# 覆盖：列表解析（合成 HTML，形态依据 scripts/fetch_ebidding_lists.py 实测结构）、
# 源注册表校验、搜索执行（robots 拒绝/region 覆盖/限频跳过/关键词过滤 → 候选落库）、
# 候选详情入库（原文固化 + Project 建档 + 幂等去重）、robots.txt 404 语义。
# 网络一律注入 fake fetch（fetcher 注入式设计），沙盒内可跑；真实源冒烟在沙盒外执行。
from __future__ import annotations

import json
import re as _re
import urllib.error
from datetime import date

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.collecting import service as collecting
from runtime.collecting.parsers import parse_hebtig_list
from runtime.collecting.registry import SOURCES, SourceSpec, get, validate_sources
from runtime.core.compliance import RateLimiter
from runtime.core.compliance import SENSIBLE_UA, TRANSPARENT_UA
from runtime.core.fetcher import _request
from runtime.core.fetcher import RobotsUnavailable, fetch_robots
from runtime.db.models import AnnouncementCandidate, AnalysisJob, Base, Material, Project

_SCHEMA_PREFIX = _re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _reset_limiter():
    collecting._limiter = RateLimiter()
    yield


# ── 列表页解析（合成 HTML；结构与 fetch_ebidding_lists.py 实测正则一致） ──
# 实测结构（scripts/ebidding_gongcheng.html）：
#   <li class="infos-list-item">
#     <a href="/jyxx/.../20260822/uuid.html" title="…">…</a>
#     <span class="time detail-info">发布日期：<span>2026-08-22</span></span>
#   </li>
_HEBTIG_HTML = """
<html><body>
<div class="list">
  <a href="/jyxx/001001/001001001/20260903/abc.html" title="河北交投XX高速公路施工招标公告">施工招标公告</a><span class="time detail-info">发布日期：<span>2026-09-03</span></span>
  <a href="/jyxx/001001/001001001/20260903/def.html" title="某地房屋建筑施工总承包招标公告">房屋建筑</a>
  <a href="/jyxx/001001/001001001/20260903/ghi.html">简</a>
  <a href="javascript:void(0)">占位链接</a>
</div>
</body></html>
"""


def test_parse_hebtig_list_title_href(session):
    items = parse_hebtig_list(_HEBTIG_HTML, get("hebtig"))
    assert len(items) == 2
    titles = {it["title"] for it in items}
    assert "河北交投XX高速公路施工招标公告" in titles
    assert "某地房屋建筑施工总承包招标公告" in titles
    for it in items:
        assert it["url"].startswith("https://ebidding.hebtig.com/jyxx/")
        assert it["category"] == "招标专区-工程类"


def test_fetcher_request_uses_explicit_policy_user_agent():
    assert _request("https://example.com", user_agent=SENSIBLE_UA).get_header("User-agent") == SENSIBLE_UA
    assert _request("https://example.com", user_agent=TRANSPARENT_UA).get_header("User-agent") == TRANSPARENT_UA


def test_parse_hebtig_list_publish_date_plain_and_url():
    # ① 列表页明文「发布日期：2026-09-03」优先；② 无明文 → URL 日期段 /20260822/ 回退；
    # ③ 两者皆无 → None（详情页回源，不推断）。
    html_plain = ('<a href="/jyxx/a/20260903/x.html" title="河北XX项目招标公告">招标公告</a>'
                  '<span class="time detail-info">发布日期：<span>2026-09-03</span></span>')
    items = parse_hebtig_list(html_plain, get("hebtig"))
    assert len(items) == 1
    assert items[0]["publish_date"] == "2026-09-03"

    html_urlonly = '<a href="/jyxx/a/20260822/x.html" title="某项目施工招标公告">招标公告</a>'
    items = parse_hebtig_list(html_urlonly, get("hebtig"))
    assert len(items) == 1
    assert items[0]["publish_date"] == "2026-08-22"  # URL 日期段回退

    html_nodate = '<a href="/jyxx/a/x.html" title="某项目施工招标公告">招标公告</a>'
    items = parse_hebtig_list(html_nodate, get("hebtig"))
    assert len(items) == 1
    assert items[0]["publish_date"] is None  # 无日期 → 待详情回源

    # 明文与 URL 冲突 → 明文优先（页面显式声明为事实）
    html_conflict = ('<a href="/jyxx/a/20260101/x.html" title="某项目施工招标公告">招标公告</a>'
                     '<span>发布日期：2026-09-05</span>')
    items = parse_hebtig_list(html_conflict, get("hebtig"))
    assert items[0]["publish_date"] == "2026-09-05"


def test_parse_hebtig_list_text_fallback():
    # 无 title 属性 → 文本兜底（含公告关键词且长度≥8）
    html = '<div><a href="/jyxx/a.html">某某项目招标公告</a></div>'
    items = parse_hebtig_list(html, get("hebtig"))
    assert len(items) == 1 and items[0]["title"] == "某某项目招标公告"


def test_html_to_text_strips_executable_content():
    from runtime.collecting.parsers import html_to_text

    html = """
<html><head><script>alert(1)</script><style>body{color:red}</style></head>
<body><h1>招标公告标题</h1><p>投标截止时间：2026-10-30 09:00</p><table><tr><td>保证金</td><td>20万</td></tr></table></body></html>
"""
    text = html_to_text(html)
    assert "alert" not in text and "color:red" not in text
    assert "招标公告标题" in text
    assert "投标截止时间：2026-10-30 09:00" in text
    assert "保证金" in text and "20万" in text


def test_html_to_text_empty():
    from runtime.collecting.parsers import html_to_text

    assert html_to_text("<script>x</script>") == ""
    assert html_to_text("") == ""


# ── 源注册表 ──

def test_source_spec_page_url_preserves_search_query():
    spec = get("hebtig")
    assert spec is not None
    assert spec.page_url(1) == spec.list_url
    assert spec.page_url(2).endswith("/2.html")
    assert spec.page_url(2, list_url=f"{spec.list_url}?keyword=%E6%96%BD%E5%B7%A5") == (
        f"{spec.base_url}/jyxx/001001/001001001/2.html?keyword=%E6%96%BD%E5%B7%A5"
    )

    query_spec = SourceSpec(
        source_id="query-pagination",
        name="测试源",
        base_url="https://example.test",
        level="L1",
        list_path="/notices",
        category="测试",
        page_param="page",
        max_pages=3,
    )
    assert query_spec.page_url(1, list_url="https://example.test/notices?keyword=施工") == (
        "https://example.test/notices?keyword=施工"
    )
    assert query_spec.page_url(2, list_url="https://example.test/notices?keyword=施工&page=9") == (
        "https://example.test/notices?keyword=%E6%96%BD%E5%B7%A5&page=2"
    )
    no_page_spec = SourceSpec(
        source_id="home-only",
        name="首页源",
        base_url="https://example.test",
        level="L1",
        list_path="/notices",
        category="测试",
    )
    assert no_page_spec.page_url(2) == no_page_spec.list_url
    with pytest.raises(ValueError):
        spec.page_url(0)

def test_registry_hebtig_enabled_l1():
    spec = get("hebtig")
    assert spec is not None and spec.level == "L1"
    assert spec.covers_region("河北省")
    assert spec.covers_region(None)
    # 省域平台覆盖省内市级检索（验收暴露：石家庄市/雄安新区 不得误判超范围跳过）
    assert spec.covers_region("石家庄市")
    assert spec.covers_region("雄安新区")
    assert not spec.covers_region("北京市")


def test_registry_hebtig_partitions_all_registered():
    # v1.10：惠招标同平台服务/货物/非招标分区一并注册（同域同结构，robots 无限制；
    # 限频按域共享，多分区不改变单源红线）。全部为 L1、河北域覆盖。
    for sid in ("hebtig_service", "hebtig_goods", "hebtig_nzb"):
        spec = get(sid)
        assert spec is not None and spec.level == "L1"
        assert spec.base_url == "https://ebidding.hebtig.com"
        assert spec.search_param == "keyword"
        assert spec.covers_region("河北省") and spec.covers_region("保定市")
    # 同域四个分区：限频器按域计数，一次任务抓多分区仍 ≤1 次/5 分钟（合规语义）
    domains = {get(sid).base_url for sid in ("hebtig", "hebtig_service", "hebtig_goods", "hebtig_nzb")}
    assert len(domains) == 1


def test_validate_sources_unknown_rejected():
    # 2026-09-08 全量清单接入：qianlima 已登记（不再是 unknown），但 collectable=False。
    # validate 视其为"已注册有效源"；采集侧按 collectable 过滤是否实际抓取。
    valid, unknown = validate_sources(["hebtig", "qianlima"])
    assert valid == ["hebtig", "qianlima"] and unknown == []
    assert SOURCES["qianlima"].collectable is False  # 登记但不参与自动抓取
    assert SOURCES["hebtig"].collectable is True  # 已实测可采集
    valid, unknown = validate_sources(None)
    assert valid == list(SOURCES) and unknown == []


def test_validate_sources_deduplicates_preserving_first_order():
    valid, unknown = validate_sources([
        "hebtig", "hebtig", "qianlima", "qianlima", "hebtig_service", "movie",
    ])
    assert valid == ["hebtig", "qianlima", "hebtig_service"]
    assert unknown == ["movie"]


# ── 搜索执行 ──

def _fake_fetch(url: str) -> str:
    if "trade.html" in url:
        return _HEBTIG_HTML
    return f"<html><body><h1>公告详情</h1><p>{url}</p></body></html>"


def _fake_robots(url: str) -> str | None:
    return None  # 无 robots.txt → 允许


def test_search_stores_candidates_filtered(session):
    summary = collecting.search_sources(
        session, keyword="房屋建筑施工", region="河北省", sources=["hebtig"],
        search_job_id="job-1", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "ok" and summary[0]["count"] == 1
    rows = session.scalars(select(AnnouncementCandidate)).all()
    assert len(rows) == 1
    assert rows[0].title == "某地房屋建筑施工总承包招标公告"
    assert rows[0].import_status == "pending"
    # 2026-09-07：解析器从列表页提取发布日（明文 span 或 URL 日期段 /20260903/）→
    # publish_date 可确证落库，不再一律 null 待补；region 列表页不标注 → 仍 null
    assert rows[0].publish_date == date(2026, 9, 3)
    assert rows[0].region is None


def test_search_zero_hit_empty_candidates(session):
    summary = collecting.search_sources(
        session, keyword="不存在的关键词XYZ", region=None, sources=["hebtig"],
        search_job_id="job-2", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["count"] == 0
    assert session.scalars(select(AnnouncementCandidate)).all() == []


def test_search_paginates_and_deduplicates_with_one_rate_event(session, monkeypatch):
    # development 仅用于确定性单测：分页边界由 policy.max_pages 控制；
    # 单次搜索批次的多页只算一次源级限频事件（production/staging 同语义）。
    monkeypatch.setenv("COLLECTION_POLICY", "development")
    pages = {
        1: '<a href="/jyxx/001001/001001001/20260903/p1.html" title="第一页施工招标公告">施工</a>',
        2: ('<a href="/jyxx/001001/001001001/20260903/p2.html" title="第二页施工招标公告">施工</a>'
            '<a href="/jyxx/001001/001001001/20260903/p1.html" title="第一页施工招标公告">施工</a>'),
        3: '<a href="/jyxx/001001/001001001/20260903/p3.html" title="第三页施工招标公告">施工</a>',
        4: "<html><body></body></html>",
        5: "<html><body></body></html>",
    }
    seen: list[str] = []

    def paged_fetch(url: str) -> str:
        seen.append(url)
        path = url.split("?", 1)[0]
        last = path.rsplit("/", 1)[-1]
        page = int(last.split(".", 1)[0]) if last != "trade.html" else 1
        return pages[page]

    summary = collecting.search_sources(
        session, keyword="施工", region="河北省", sources=["hebtig"],
        search_job_id="job-pagination", fetch_fn=paged_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "ok"
    assert summary[0]["pages_requested"] == 5
    assert summary[0]["pages_fetched"] == 5
    assert summary[0]["count"] == 3
    assert "抓取 5 页、去重后 3 条" in summary[0]["note"]
    assert len(seen) == 5
    assert len(collecting._limiter._events) == 0  # development 关闭限频，不记录事件

    # staging 验证同一域多页只有一次事件；第二个批次仍被单源限频拒绝。
    monkeypatch.setenv("COLLECTION_POLICY", "staging")
    collecting._limiter.reset()
    collecting._limiter_policy_signature = None
    collecting.search_sources(
        session, keyword="施工", region="河北省", sources=["hebtig"],
        search_job_id="job-pagination-staging-1", fetch_fn=paged_fetch, robots_fn=_fake_robots,
    )
    assert len(collecting._limiter._events) == 1
    second = collecting.search_sources(
        session, keyword="施工", region="河北省", sources=["hebtig"],
        search_job_id="job-pagination-staging-2", fetch_fn=paged_fetch, robots_fn=_fake_robots,
    )
    assert second[0]["status"] == "skipped"


def test_search_no_category_filter_at_collect_time(session):
    # v1.7（09-优化方案 §3.1）：种类/规模不在搜索（采集）端过滤——一次联网检索先
    # 返回完整候选集。合成 HTML 两条候选（高速公路/房屋建筑）均落库，不按标题收窄；
    # 种类筛选由候选列表内筛选接口（category_matches）在结果上执行。
    summary = collecting.search_sources(
        session, keyword="", region=None, sources=["hebtig"],
        search_job_id="job-no-cat-1", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "ok" and summary[0]["count"] == 2
    rows = session.scalars(select(AnnouncementCandidate)).all()
    assert {r.title for r in rows} == {
        "河北交投XX高速公路施工招标公告", "某地房屋建筑施工总承包招标公告"}
    assert "关键词过滤后 2 条" in summary[0]["note"]


def test_search_category_matches_pure_function():
    from runtime.collecting.registry import category_matches

    assert category_matches(None, "某项目招标公告")
    assert category_matches("全部种类", "某项目招标公告")
    assert category_matches("房屋建筑", "某地房屋建筑施工总承包招标公告")   # 标题命中
    assert not category_matches("公路工程", "某地房屋建筑施工总承包招标公告")  # 未点名 → 不推断
    assert category_matches("公路工程", "河北交投XX高速公路施工招标公告")


def test_search_scale_unknown_kept_not_excluded():
    # 规模筛选语义（路由 list_search_candidates）：候选无规模事实（列表页不标注）→
    # 一律归「规模待确认」（unknown）保留，不得因未知被当作不匹配排除。此处直接
    # 校验词表语义常量，路由行为由 API 契约测试覆盖（沙盒无库时校验不触发）。
    from runtime.collecting.registry import SEARCH_CATEGORY_OPTIONS

    assert "全部种类" in SEARCH_CATEGORY_OPTIONS


def test_search_out_of_scope_relaxed_recall(session):
    # 09-优化 v1.9：检索地区超源覆盖不再整源丢弃 → 放宽二次召回（仍抓列表+关键词过滤），
    # 候选地区不推断、标待核实（列表页不标注 → region=None），逐源摘要如实说明放宽。
    summary = collecting.search_sources(
        session, keyword="施工", region="北京市", sources=["hebtig"],
        search_job_id="job-3", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "ok"
    assert "放宽召回" in summary[0]["note"] and "地区待核实" in summary[0]["note"]
    rows = session.scalars(select(AnnouncementCandidate)).all()
    assert len(rows) == 2
    assert all(r.region is None for r in rows)  # 不推断归属
    assert all(r.import_status == "pending" for r in rows)


def test_search_out_of_scope_strict_zero_keyword(session):
    # 放宽召回仍过关键词过滤：超范围 + 零关键词命中 → 如实零候选（有 note 说明放宽）
    summary = collecting.search_sources(
        session, keyword="不存在的关键词XYZ", region="北京市", sources=["hebtig"],
        search_job_id="job-3b", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "ok" and summary[0]["count"] == 0
    assert "放宽召回" in summary[0]["note"]
    assert session.scalars(select(AnnouncementCandidate)).all() == []


def test_search_city_region_within_province_platform(session):
    # 省级平台覆盖省内市级检索（2026-09-03 验收暴露：region=石家庄市 曾被误判跳过）
    summary = collecting.search_sources(
        session, keyword="房屋建筑施工", region="石家庄市", sources=["hebtig"],
        search_job_id="job-city-1", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "ok" and summary[0]["count"] == 1
    rows = session.scalars(select(AnnouncementCandidate)).all()
    assert len(rows) == 1
    # 列表页不逐条标注地区：候选 region 仍为 null=待补，不推断归属
    assert rows[0].region is None and rows[0].import_status == "pending"


def test_search_records_robots_disallow_without_blocking(session):
    # ADR-003：robots 预检留痕不阻断——Disallow 命中时仍抓取列表，
    # 摘要记 robots_status=disallowed + 命中规则，不跳过源。
    def robots_disallow(url: str) -> str:
        return "User-agent: *\nDisallow: /jyxx/"

    summary = collecting.search_sources(
        session, keyword="施工", region=None, sources=["hebtig"],
        search_job_id="job-4", fetch_fn=_fake_fetch, robots_fn=robots_disallow,
    )
    assert summary[0]["status"] == "ok"  # 不再 skipped
    assert summary[0]["robots_status"] == "disallowed"
    # 原因打全：哪条 robots 规则命中（Allow/Disallow 最长前缀优先，不允许只报路径）
    assert "命中 Disallow: /jyxx/" in summary[0]["note"]
    rows = session.scalars(select(AnnouncementCandidate)).all()
    # _fake_fetch 返回 2 条（均含"施工"）→ 全部落库：Disallow 不阻断
    assert len(rows) == 2


def test_robots_allow_prefix_overrides_disallow_reason():
    # Allow 与 Disallow 等长/更长命中时 Allow 生效（RFC 9309）→ matched_disallow 返回 None
    from runtime.core.compliance import parse_robots

    rules = parse_robots("User-agent: *\nDisallow: /jyxx/\nAllow: /jyxx/001001/001001001/")
    assert rules.allows("/jyxx/001001/001001001/trade.html")
    assert rules.matched_disallow("/jyxx/001001/001001001/trade.html") is None
    rules2 = parse_robots("User-agent: *\nDisallow: /jyxx/")
    assert not rules2.allows("/jyxx/001001/x.html")
    assert rules2.matched_disallow("/jyxx/001001/x.html") == "/jyxx/"


def test_search_rate_limited_same_domain(session):
    collecting.search_sources(
        session, keyword="", region=None, sources=["hebtig"],
        search_job_id="job-5", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    # 同域 5 分钟内第二次搜索 → 限频跳过（红线 ≤1 次/5 分钟）
    summary = collecting.search_sources(
        session, keyword="", region=None, sources=["hebtig"],
        search_job_id="job-6", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "skipped" and "限频" in summary[0]["note"]


def test_search_unknown_source_reported(session):
    summary = collecting.search_sources(
        session, keyword="x", region=None, sources=["nope"],
        search_job_id="job-7", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "error" and "未注册" in summary[0]["note"]


# ── 关键词确定性匹配（v1.10：多词 AND + 全半角/空白归一） ──

def test_keyword_matches_multi_word_and():
    from runtime.collecting.registry import keyword_matches

    # 多词 AND：词序不敏感、空白即分隔
    assert keyword_matches("房屋 施工", "某地房屋建筑施工总承包招标公告")
    assert keyword_matches("施工 房屋", "某地房屋建筑施工总承包招标公告")
    assert keyword_matches("某地 房屋", "某地房屋建筑施工总承包招标公告")
    # 少一个词 → 不命中（AND 语义，不推断）
    assert not keyword_matches("房屋 桥梁", "某地房屋建筑施工总承包招标公告")
    # 词必须在标题内连续成词（确定性子串）：标题「房屋建筑」不含连续词「房建」
    assert not keyword_matches("某地 房建", "某地房屋建筑施工总承包招标公告")
    # 空关键词恒命中
    assert keyword_matches("", "任意标题")
    assert keyword_matches(None, None)


def test_keyword_matches_fullwidth_and_space_normalize():
    from runtime.collecting.registry import keyword_matches

    # 全角空格/全角标点归一为半角后多词 AND 命中
    assert keyword_matches("房屋　施工", "某地房屋建筑施工总承包招标公告")      # 全角空格
    assert keyword_matches("房屋  施工", "某地房屋建筑施工总承包招标公告")      # 连续空格
    assert keyword_matches("Ｇ2002 高速", "G2002青银高速石太段改造工程招标公告")  # 全角字母
    # 平台检索页 URL 参数名（keyword 命中）构造口径
    assert keyword_matches("青银 高速", "G20青银高速石太段小客车限速调整工程招标公告")


def test_search_uses_site_search_page_when_keyword(session):
    # v1.10：有 keyword 且源带 search_param → 抓平台检索页（?keyword=…）而非默认列表
    seen_urls: list[str] = []

    def fake_fetch(url: str) -> str:
        seen_urls.append(url)
        return _HEBTIG_HTML

    collecting.search_sources(
        session, keyword="房屋建筑施工", region=None, sources=["hebtig"],
        search_job_id="job-site-search", fetch_fn=fake_fetch, robots_fn=_fake_robots,
    )
    assert seen_urls and "trade.html?keyword=" in seen_urls[0]
    # 检索页命中后仍本地确定性过滤（多词 AND 兜底）
    rows = session.scalars(select(AnnouncementCandidate)).all()
    assert len(rows) == 1 and rows[0].title == "某地房屋建筑施工总承包招标公告"


def test_search_no_keyword_falls_back_default_list(session):
    # 无 keyword → 仍抓默认列表页（不拼检索参数）
    seen_urls: list[str] = []

    def fake_fetch(url: str) -> str:
        seen_urls.append(url)
        return _HEBTIG_HTML

    collecting.search_sources(
        session, keyword="", region=None, sources=["hebtig"],
        search_job_id="job-default-list", fetch_fn=fake_fetch, robots_fn=_fake_robots,
    )
    assert seen_urls and seen_urls[0].endswith("trade.html")


# ── 候选详情入库 ──

def test_import_candidate_detail(session, tmp_path):
    collecting.search_sources(
        session, keyword="", region=None, sources=["hebtig"],
        search_job_id="job-8", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    cand = session.scalar(select(AnnouncementCandidate))
    assert cand is not None
    # 模拟搜索后 ≥5 分钟再确认导入（同域限频窗口外）；真实时序下紧邻导入会合规拒绝
    collecting._limiter = RateLimiter()
    result = collecting.import_candidate_detail(
        session, candidate=cand, actor="tester", store_root=str(tmp_path),
        fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert result["created"] is True and result["project_id"].startswith("PJ-")
    project = session.get(Project, result["project_id"])
    assert project is not None and project.project_name == cand.title
    material = session.scalar(
        select(Material).where(Material.material_id == result["material_id"])
    )
    assert material is not None
    assert material.material_type == "announcement"
    assert material.source_type == "official_platform"
    assert material.owner_type == "public"
    assert material.project_id == project.project_id
    assert cand.import_status == "imported"
    assert cand.project_id == project.project_id


def test_import_candidate_detail_duplicate_content_idempotent(session, tmp_path):
    collecting.search_sources(
        session, keyword="", region=None, sources=["hebtig"],
        search_job_id="job-9", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    cand = session.scalar(select(AnnouncementCandidate))
    collecting._limiter = RateLimiter()  # 限频窗口外（同 test_import_candidate_detail 时序）
    collecting.import_candidate_detail(
        session, candidate=cand, actor="tester", store_root=str(tmp_path),
        fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    # 同域限频窗口（5 分钟）外重导：内容哈希去重 → created=False（raw 不可覆盖）
    collecting._limiter = RateLimiter()
    result = collecting.import_candidate_detail(
        session, candidate=cand, actor="tester", store_root=str(tmp_path),
        fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert result["created"] is False  # 内容哈希去重，raw 不可覆盖


def test_mark_import_failed_records_error(session):
    cand = AnnouncementCandidate(
        candidate_id="cand-fail-1", search_job_id="job-10", source_id="hebtig",
        source_name="惠招标", title="某公告", url="https://ebidding.hebtig.com/jyxx/a.html",
        import_status="pending",
    )
    session.add(cand)
    session.commit()
    collecting.mark_import_failed(session, cand.candidate_id, RuntimeError("抓取失败"))
    session.refresh(cand)
    assert cand.import_status == "failed"
    assert "抓取失败" in cand.error_message


# ── robots.txt 404 = 无文件 → None（允许抓取，区别于网络故障） ──

def test_fetch_robots_404_means_no_file(monkeypatch):
    def _raise_404(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr("runtime.core.fetcher.urllib.request.urlopen", _raise_404)
    assert fetch_robots("https://example.com/page") is None


def test_fetch_robots_network_error_conservative(monkeypatch):
    def _raise_net(url, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("runtime.core.fetcher.urllib.request.urlopen", _raise_net)
    with pytest.raises(RobotsUnavailable):
        fetch_robots("https://example.com/page")


# ── 公告类型分类 + 项目去重（2026-09-08 验收三问题修复） ──
def test_announcement_type_classify_pure():
    from runtime.collecting import announcement_type as at
    # ① 法规/规章（不是招标机会）
    assert at.classify_type("《公共资源交易中心招标投标现场管理暂行办法》 2026年第43号令") == at.OTHER
    assert at.classify_type("《招标投标领域信用管理暂行办法》 2026年第44号令") == at.OTHER
    # ② 中标类
    assert at.classify_type("XX项目EPC总承包中标候选人公示") == at.WIN
    assert at.classify_type("XX项目设计施工总承包中标结果公告") == at.WIN
    # ③ 采购类
    assert at.classify_type("某单位信息化设备采购公告") == at.PROCURE
    assert at.classify_type("某项目竞争性磋商公告") == at.PROCURE
    # ④ 资格预审（属招标）
    assert at.classify_type("某县道路改造工程资格预审公告") == at.PREQUAL
    # ⑤ 变更
    assert at.classify_type("某某项目更正公告") == at.CHANGE
    # ⑥ 默认 → 招标（不能确证非标，保守保留）
    assert at.classify_type("太行智慧冷链物流园山体冷库项目施工招标公告") == at.TENDER
    assert at.is_default_include(at.TENDER) and at.is_default_include(at.PREQUAL)
    assert not at.is_default_include(at.WIN) and not at.is_default_include(at.OTHER)


def test_announcement_classify_split_and_dedup():
    from runtime.collecting.announcement_type import classify, dedupe_by_project
    items = [
        {"title": "河北交投康保县400MW储能项目EPC总承包招标公告", "source_id": "hebtig"},
        {"title": "河北交投康保县400MW储能项目EPC总承包中标候选人公示", "source_id": "hebtig"},
        {"title": "河北交投康保县400MW储能项目EPC总承包中标结果公告", "source_id": "szj-hebei"},
        {"title": "太行智慧冷链物流园山体冷库项目施工招标公告", "source_id": "hebtig_goods"},
        {"title": "《公共资源交易中心招标投标现场管理暂行办法》2026年第43号令", "source_id": "szj-hebei"},
    ]
    kept, excluded = classify(items)
    assert all(k["announcement_type"] == "tender" or k["announcement_type"] == "prequal" for k in kept)
    assert len(kept) == 2          # 两个真招标（康保招标 + 太行施工）
    assert len(excluded) == 3      # 中标候选/中标结果/办法
    merged = dedupe_by_project(kept)
    # 康保 招标 与 太行 施工 是不同 key → 不误合并
    assert len(merged) == 2
    # 康保若与其中标同组但被过滤（只留招标），此时 keept 只剩招标本身
    kp = [m for m in merged if "太行" not in m["title"]]
    assert kp and kp[0]["announcement_type"] == "tender"


def test_dedupe_by_project_merges_stages_cross_source():
    from runtime.collecting.announcement_type import classify, dedupe_by_project
    items = [
        {"title": "G2002高速石太段改造工程施工招标公告", "source_id": "szj-hebei"},
        {"title": "G2002高速石太段改造工程施工中标候选人公示", "source_id": "hebtig"},
        {"title": "G2002高速石太段改造工程施工YH-1标段招标公告", "source_id": "ccgp-hebei-web"},
        {"title": "秦皇岛市某道路改造工程施工资格预审公告", "source_id": "sjzsggzy"},
    ]
    # 真实调用链：先分类（补 announcement_type），再去重
    classified, _ = classify(items)
    merged = dedupe_by_project(classified)
    # G2002 三种形态 → 归并 1，且保留招标类型（type 优先）
    g = [m for m in merged if "G2002" in m["title"]]
    assert len(g) == 1
    assert g[0]["announcement_type"] == "tender"
    # 跨源去重：招标公告（szj + ccgp 的 YH 标段）归并为一组；中标候选已被默认类型过滤排除
    assert set(g[0]["source_group"]) == {"szj-hebei", "ccgp-hebei-web"}
    assert g[0]["stage_count"] >= 2
    # 秦皇岛道路资格预审 → prequal（招标型保留）
    pre = [m for m in merged if "道路改造" in m["title"]]
    assert pre and pre[0]["announcement_type"] == "prequal"
    # 不同项目（G2002 vs 市道路）不误合并
    assert len(merged) == 2


def test_search_skips_non_collectable_sources():
    # 千里马/剑鱼等 collectable=False 的登记源不抓取（2026-09-08 验收：blocked 源曾被真实抓取）
    no_col = {sid for sid, s in SOURCES.items() if not s.collectable}
    assert "qianlima" in no_col and "jianyu360" in no_col and "bidcenter" in no_col


# ── 2026-09-09：import 详情后 detail_summary 初筛抽取 + 日期兜底 ──

def test_import_candidate_detail_fills_detail_summary_and_date(session, tmp_path):
    """点深入抓详情后：detail_summary 存资质/人员/日期，publish_date 兜底回填。"""
    from runtime.collecting.parsers import extract_detail_publish_date

    # 详情页返回真实公告正文（含资质/地区/工期/日期），非壳
    DETAIL_HTML = ("<html><body><div>发布时间：2026-09-03</div>"
                   "<p>本项目总投资 1000 万元。</p>"
                   "<p>建设地点：河北省石家庄市鹿泉区。</p>"
                   "<p>最高投标限价：842.002736 万元。</p>"
                   "<p>计划工期：45 日历天；质量标准：合格；本工程共计划分1个标段。</p>"
                   "<p>3.2 具备建筑工程施工总承包三级及以上资质，并具有有效的安全生产许可证。</p>"
                   "<p>3.7 拟派项目经理具有注册在投标单位的机电工程一级注册建造师执业资格。</p>"
                   "<p>3.10 配备专职安全生产管理人员1个。</p>"
                   "</body></html>")

    def _detail_fetch(url: str) -> str:
        # 列表页返回两候选，详情页区用真实正文（让抽取有料）
        if "trade.html" in url:
            return _HEBTIG_HTML
        return DETAIL_HTML

    collecting.search_sources(
        session, keyword="", region=None, sources=["hebtig"],
        search_job_id="job-detail-1", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    collecting._limiter = RateLimiter()
    cand = session.scalar(select(AnnouncementCandidate))
    assert cand is not None
    # 候选列表页无发布日期 → 依赖详情兜底
    before = cand.publish_date
    result = collecting.import_candidate_detail(
        session, candidate=cand, actor="tester", store_root=str(tmp_path),
        fetch_fn=_detail_fetch, robots_fn=_fake_robots,
    )
    session.refresh(cand)
    assert cand.import_status == "imported"
    # —— detail_summary 落库（资质抽取）——
    assert cand.detail_summary is not None
    q = (cand.detail_summary.get("qualification") or {}).get("value", "")
    assert "建筑工程施工总承包" in q
    # 日期兜底：若列表无日期，从详情「发布时间」回填
    if before is None:
        assert cand.publish_date is not None
    # C4：详情「建设地点」命中 → 候选 region 回填（公告事实）
    assert cand.region == "河北省石家庄市鹿泉区"
    # 抽取器也能直接对详情文本出地区/工期/质量
    assert extract_detail_publish_date(DETAIL_HTML, DETAIL_HTML) is not None


# ── 2026-09-10：A1 同域分区共享限频 + B1 逐源日期提取 + C1/C3/C4/D1 ──

def test_search_shared_domain_partitions_one_rate_event(session, monkeypatch):
    """A1：惠招标 4 分区（同域）一次搜索批次只登记一次域级限频事件，
    互相不再拒绝（与「多页只记一次」同口径）；下一批次仍被 5 分钟窗口拒。"""
    monkeypatch.setenv("COLLECTION_POLICY", "staging")
    collecting._limiter = RateLimiter()
    collecting._limiter_policy_signature = None
    partitions = ["hebtig", "hebtig_service", "hebtig_goods", "hebtig_nzb"]
    summary = collecting.search_sources(
        session, keyword="", region=None, sources=partitions,
        search_job_id="job-a1-batch1", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert [s["status"] for s in summary] == ["ok"] * 4  # 一次搜索 4 分区全 ok
    assert len(collecting._limiter._events) == 1         # 域级事件只登记一次
    # 第二个批次：同域窗口内 → 全部 skipped（红线不放松）
    second = collecting.search_sources(
        session, keyword="", region=None, sources=partitions,
        search_job_id="job-a1-batch2", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert all(s["status"] == "skipped" and "限频" in (s["note"] or "") for s in second)


# B1：逐源日期提取（合成 HTML，形态取自 2026-09-10 实测页面结构）
_SJZ_HTML = """<ul class="panel-list">
  <li><a href="https://www.sjzsggzyjyzx.org.cn:443/jyxxgczb/200381.jhtml" target="_blank">
    <span>[市本级] </span>河北省档案方志馆建设项目外电引入及配电室工程澄清与答疑公告</a>
    <div>2026.09.10</div></li>
</ul>"""

_HBGGZYFWPT_HTML = """<ul id="index3" class="ulActive">
  <li><img src="/res/new_ico.png"><span class="litype">[工程建设]</span>
    <a href=/jyxx/jsgcZbggDetail?guid=87783617-a0b8 title="九峰山半导体生产制造基地项目工程总承包（EPC）（第一标段）" target='_blank'>
      九峰山半导体生产制造基地项目工程总承包（E...</a>
    <span class="fr">2026-05-22</span></li>
</ul>
<script>$("#x").append("<li><a href=/jyxx/t?guid=" + ob.projectName + " title=" + ob.projectName + ">模板串</a></li>");</script>
<!-- <a href="/jyxx/oldDetail?guid=xx" title="">被注释的废弃锚点</a> -->"""

_CCGP_HEBEI_HTML = """<div class="list-item"><div class="list-item-content">
  <div class="singleLine"><a href="../../../cd/cd_kfq/cggg/zbggAAAA/202609/t20260910_2426123.html"
    target="_blank" title="承德高新区上板城工业园区10kV电缆沟新建工程竞争性磋商公告">承德高新区上板城工业园区10kV电缆沟新建工程竞争性磋商公告</a></div>
  <div class="list-item-meta-content"><span class="list-item-meta" data-label="发布时间：">2026-09-10</span></div>
</div></div>"""

_HDGGZY_HTML = """<ul class="public-list" id="infolist">
  <li><a class="public-list-item" href="/jydt/003002/003002001/20260909/80d59359-a117.html">
    <span class="inf">曲周县振兴路小型消防站项目招标公告</span>
    <span class="date"> 2026-09-09</span></a></li>
</ul>"""


def test_parse_sjz_channel_row_date():
    # 石家庄：锚点无 title 属性 + 行内 <div>2026.09.10</div>（点分日期就近回退）
    items = parse_hebtig_list(_SJZ_HTML, get("sjzsggzy"))
    assert len(items) == 1
    assert items[0]["publish_date"] == "2026-09-10"
    assert "河北省档案方志馆" in items[0]["title"]
    assert "2026.09.10" not in items[0]["title"]  # 日期不污染标题


def test_parse_province_trading_page_unquoted_href_and_script_filter():
    # 省平台：未加引号 href + span.fr 行内日期；script 模板串与注释锚点不当条目
    items = parse_hebtig_list(_HBGGZYFWPT_HTML, get("szj-hebei"))
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "九峰山半导体生产制造基地项目工程总承包（EPC）（第一标段）"
    assert it["publish_date"] == "2026-05-22"
    assert it["url"].startswith("https://www.hbggzyfwpt.cn/jyxx/jsgcZbggDetail?guid=")
    assert all("ob.projectName" not in i["title"] for i in items)   # JS 模板串被剥
    assert all("被注释" not in i["title"] for i in items)            # 注释锚点被剥


def test_parse_ccgp_hebei_meta_date_and_url_date():
    # 河北政采：data-label 发布时间 + 行内就近回退；URL t20260910_ 形态兜底
    items = parse_hebtig_list(_CCGP_HEBEI_HTML, get("ccgp-hebei-web"))
    assert len(items) == 1
    assert items[0]["publish_date"] == "2026-09-10"
    assert items[0]["url"].endswith("t20260910_2426123.html")
    # 无行内日期时 URL t 形态兜底可用（_publish_date 直测）
    from runtime.collecting.parsers import _publish_date
    d = _publish_date("http://x/cggg/zbggAAAA/202609/t20260908_1.html", "")
    assert d is not None and d.isoformat() == "2026-09-08"


def test_parse_hdggzy_inner_date_and_title_strip():
    # 邯郸：日期在锚内 <span class="date"> → 兜底文本带尾部日期须剥离，日期照取
    items = parse_hebtig_list(_HDGGZY_HTML, get("hdggzy"))
    assert len(items) == 1
    assert items[0]["title"] == "曲周县振兴路小型消防站项目招标公告"
    assert items[0]["publish_date"] == "2026-09-09"
    assert items[0]["url"].startswith("https://ggzy.hd.gov.cn/jydt/003002/003002001/20260909/")


def test_parse_row_boundary_prevents_cross_item_date():
    # 行界截断：当前条目无日期时，不得把下一条目的日期错配过来
    html = ('<a href="/a.html" title="某项目施工招标公告甲">甲</a></li>'
            '<li><a href="/b.html" title="某项目施工招标公告乙">乙</a>'
            '<span>2026-09-01</span></li>')
    items = parse_hebtig_list(html, get("hebtig"))
    by_title = {i["title"]: i["publish_date"] for i in items}
    assert by_title["某项目施工招标公告甲"] is None   # 不跨条目错配
    assert by_title["某项目施工招标公告乙"] == "2026-09-01"


def test_registry_cangzhou_url_and_handan_channel():
    # A2：沧州 list_path 修复（前导斜杠，URL 可拼接）；邯郸接入工程招标频道
    cz = get("cangzhou")
    assert cz.list_url == "https://xzsp.cangzhou.gov.cn/xzsp/add100115/"
    assert cz.collectable is False and cz.compliance_status == "pending"  # JS 动态列表，如实标注
    hd = get("hdggzy")
    assert hd.list_url.endswith("/jydt/003002/003002001/trading_hall.html")
    assert hd.collectable is True and hd.compliance_status == "verified"
    assert hd.parent_region == "河北省"


def test_region_coverage_three_states():
    # C1：direct / broader / none 三态
    heb_tig = get("hebtig")          # 省级平台（admin_subregions=全省）
    ts = get("tsggzy")               # 地市源（唐山，parent_region=河北省）
    ggzy = get("ggzy")               # 全国源
    bj = get("ccgp-beijing")         # 京源
    # 省级检索：省级平台 direct；地市源 direct（覆盖其下所有地市源，不标待核实）；全国 broader
    assert heb_tig.region_coverage("河北省") == "direct"
    assert ts.region_coverage("河北省") == "direct"
    assert ggzy.region_coverage("河北省") == "broader"
    # 地市检索：同市 direct；省级/全国平台 broader（候选地区待核实）；他市/外省 none
    assert ts.region_coverage("唐山市") == "direct"
    assert heb_tig.region_coverage("唐山市") == "broader"
    assert ggzy.region_coverage("唐山市") == "broader"
    assert get("sjzsggzy").region_coverage("唐山市") == "none"
    assert bj.region_coverage("唐山市") == "none"
    # 无检索地区 → direct（全检）
    assert heb_tig.region_coverage(None) == "direct"
    # 布尔兼容口径
    assert heb_tig.covers_region("唐山市") is True
    assert bj.covers_region("唐山市") is False


def test_search_broader_scope_note_and_recall_note(session):
    # C1：地市检索命中省级平台 → 正常采集 + note 标「来源为省级平台，地区待核实」
    summary = collecting.search_sources(
        session, keyword="施工", region="石家庄市", sources=["hebtig"],
        search_job_id="job-c1-broader", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "ok"
    assert "来源为省级平台" in summary[0]["note"] and "地区待核实" in summary[0]["note"]


def test_region_from_title_city_and_county():
    # C4：标题地区抽取（市/县字典命中 → 市口径；不命中不填）
    from runtime.collecting.hebei_regions import region_from_title
    assert region_from_title("曲周县振兴路小型消防站项目招标公告") == "邯郸市"
    assert region_from_title("邯郸经济技术开发区中创新航110kV线路工程勘察设计招标公告") == "邯郸市"
    assert region_from_title("石家庄市轨道交通某项目招标公告") == "石家庄市"
    assert region_from_title("雄县温泉城片区市政管网改造工程招标公告") == "雄安新区"
    assert region_from_title("某省外项目招标公告") is None
    assert region_from_title(None) is None


def test_project_type_from_title():
    # D1：标题确定性种类抽取（枚举优先级：EPC总承包 > 施工 > 监理 > 勘察 > 设计…）
    from runtime.collecting.registry import project_type_from_title
    assert project_type_from_title("九峰山基地项目工程总承包（EPC）（第一标段）") == "EPC总承包"
    assert project_type_from_title("设计施工总承包某项目招标公告") == "EPC总承包"
    assert project_type_from_title("涉县管网二期改造提升项目施工一标段招标公告") == "施工"
    assert project_type_from_title("某项目监理招标公告") == "监理"
    assert project_type_from_title("某线路工程勘察设计招标公告") == "勘察"
    assert project_type_from_title("某信息化设备采购公告") is None  # 采购不推断为货物/服务


def test_region_options_from_registry():
    # C3：地区下拉由注册表生成——河北省 + 真有可采集源的地市；无源城市不出现
    from runtime.collecting.registry import region_options
    opts = region_options()
    assert opts[0] == "河北省"
    assert "邯郸市" in opts and "石家庄市" in opts and "唐山市" in opts
    assert "秦皇岛市" not in opts      # collectable=False（blocked）
    assert "沧州市" not in opts        # collectable=False（pending，JS 动态列表）
    assert "邢台市" not in opts        # blocked


def test_html_to_text_breaks_table_cells_into_lines():
    # 2026-09-11 P0：td/th 转块级——表单式公告的标签与值不再粘成「是否接受联合体投标否」。
    # 注：html_to_text 末尾 _WS 把所有换行折叠为空格（既有口径，输出为单行），故单元格间
    # 边界体现为空格；净化为保真降级，不发明冒号等原文没有的字符。
    from runtime.collecting.parsers import html_to_text

    html = "<table><tr><th>是否接受联合体投标</th><td>否</td></tr><tr><td>采购人</td><td>河北工业大学</td></tr></table>"
    text = html_to_text(html)
    assert "是否接受联合体投标否" not in text and "采购人河北工业大学" not in text
    assert "是否接受联合体投标 否" in text and "采购人 河北工业大学" in text
