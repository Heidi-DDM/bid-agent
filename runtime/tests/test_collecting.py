# R004/F020：真实公告采集运行时测试
# 覆盖：列表解析（合成 HTML，形态依据 scripts/fetch_ebidding_lists.py 实测结构）、
# 源注册表校验、搜索执行（robots 拒绝/region 覆盖/限频跳过/关键词过滤 → 候选落库）、
# 候选详情入库（原文固化 + Project 建档 + 幂等去重）、robots.txt 404 语义。
# 网络一律注入 fake fetch（fetcher 注入式设计），沙盒内可跑；真实源冒烟在沙盒外执行。
from __future__ import annotations

import json
import re as _re
import urllib.error

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.collecting import service as collecting
from runtime.collecting.parsers import parse_hebtig_list
from runtime.collecting.registry import SOURCES, get, validate_sources
from runtime.core.compliance import RateLimiter
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

_HEBTIG_HTML = """
<html><body>
<div class="list">
  <a href="/jyxx/001001/001001001/20260903/abc.html" title="河北交投XX高速公路施工招标公告">施工招标公告</a>
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

def test_registry_hebtig_enabled_l1():
    spec = get("hebtig")
    assert spec is not None and spec.level == "L1"
    assert spec.covers_region("河北省")
    assert spec.covers_region(None)
    assert not spec.covers_region("北京市")


def test_validate_sources_unknown_rejected():
    valid, unknown = validate_sources(["hebtig", "qianlima"])
    assert valid == ["hebtig"] and unknown == ["qianlima"]
    valid, unknown = validate_sources(None)
    assert valid == list(SOURCES) and unknown == []


# ── 搜索执行 ──

def _fake_fetch(url: str) -> str:
    if url.endswith("trade.html"):
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
    assert rows[0].region is None and rows[0].publish_date is None  # 列表页未标注 → 待补


def test_search_zero_hit_empty_candidates(session):
    summary = collecting.search_sources(
        session, keyword="不存在的关键词XYZ", region=None, sources=["hebtig"],
        search_job_id="job-2", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["count"] == 0
    assert session.scalars(select(AnnouncementCandidate)).all() == []


def test_search_skips_when_region_out_of_scope(session):
    summary = collecting.search_sources(
        session, keyword="施工", region="北京市", sources=["hebtig"],
        search_job_id="job-3", fetch_fn=_fake_fetch, robots_fn=_fake_robots,
    )
    assert summary[0]["status"] == "skipped" and "超出源覆盖" in summary[0]["note"]
    assert session.scalars(select(AnnouncementCandidate)).all() == []


def test_search_skips_when_robots_disallows(session):
    def robots_disallow(url: str) -> str:
        return "User-agent: *\nDisallow: /jyxx/"

    summary = collecting.search_sources(
        session, keyword="施工", region=None, sources=["hebtig"],
        search_job_id="job-4", fetch_fn=_fake_fetch, robots_fn=robots_disallow,
    )
    assert summary[0]["status"] == "skipped" and "禁止采集" in summary[0]["note"]


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