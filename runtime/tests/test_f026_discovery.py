# -*- coding: utf-8 -*-
# F026/ADR-005：搜索预筛、待选池与下一步处置单测（沙盒 SQLite / 纯函数）
# 覆盖：
#   1. 非招标公告类型识别补齐（#3：中标公示/成交公示/采购结果/定标/评标结果不得混入）
#   2. 确定性预筛 screen_candidate 四态 + missing_fields
#   3. 待选池 sync_candidate：入库/同项目新顶旧/不覆盖人工处理态
#   4. 规则版本化 update_rules
#   5. 匹配结果「下一步处置」映射（#8）
from __future__ import annotations

import re as _re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.collecting.announcement_type import (
    CHANGE, LEASE, OTHER, PROCURE, TENDER, WIN, classify_type, is_default_include,
)
from runtime.core.next_step import (
    NEXT_STEP_MANUAL_REVIEW, NEXT_STEP_NO_ACTION, NEXT_STEP_SUBMIT_APPROVAL,
    NEXT_STEP_SUPPLY_EVIDENCE, NEXT_STEP_VERIFY_HARD, NEXT_STEP_WAIT_TASKS,
    next_step_of_admission, next_step_of_match,
)
from runtime.db.models import AnnouncementCandidate, Base, DiscoveryRuleProfile, SelectionPoolItem
from runtime.discovery import service as discovery

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


def _cand(title: str, *, publish_date=None, url: str = "https://x.test/a", category: str | None = "工程",
          candidate_id: str | None = None, job: str = "JOB-1"):
    return AnnouncementCandidate(
        candidate_id=candidate_id or (_re.sub(r"\W", "", title)[:16] or "cand"),
        search_job_id=job, source_id="hebtig", source_name="惠招标",
        title=title, url=url, category=category, publish_date=publish_date,
        import_status="pending",
    )


# ── 1. 非招标公告类型识别（#3）────────────────────────────

@pytest.mark.parametrize("title", [
    "XX公路工程施工中标公示",
    "XX学校改造项目中标候选人公示",
    "XX医院医疗设备采购成交公示",
    "XX园区道路工程成交公告",
    "XX办公设备采购结果公示",
    "XX园区物业服务采购结果公告",
    "XX项目定标结果公示",
    "XX桥梁工程评标结果公示",
    "XX单位食堂食材采购成交供应商公告",
    "XX项目中标通知书",
    "XX水库除险加固工程资格后审结果公告",
    "XX局物资采购招标失败后重新询价公告",
    "XX区临街商铺招租公告",
    "XX市场摊位竞租信息",
    "XX单位门面房出租公告",
    "XX县国有资产处置公告",
    "XX市公共资源交易中心产权交易挂牌出让公告",
    "XX小区地下车位租赁公告",
])
def test_non_tender_types_classified_excluded(title):
    t = classify_type(title)
    assert t in (WIN, PROCURE, CHANGE, LEASE, OTHER)
    assert not is_default_include(t), f"{title} 不应默认展示"


def test_lease_does_not_eat_real_tenders():
    # ADR-006：租赁「服务采购/招标」仍按招标/采购处理，不被 LEASE 误伤
    assert classify_type("XX机械设备租赁服务采购项目公开招标公告") == TENDER
    assert classify_type("XX建筑设备租赁服务公开招标公告") == TENDER
    assert classify_type("XX机械设备租赁项目竞争性磋商公告") == PROCURE


@pytest.mark.parametrize("title", [
    "河北交投XX高速公路施工总承包招标公告",
    "XX县农村公路改造提升工程招标公告",
    "XX污水处理厂一期工程EPC总承包招标公告",
    "XX市市政道路及管网建设工程施工招标公告",
    "XX产业园厂房建设项目资格预审公告",
    "XX老旧小区改造项目设计招标公告",
])
def test_real_tender_titles_kept(title):
    assert is_default_include(classify_type(title)), f"{title} 应保留为招标"
    assert classify_type(title) in (TENDER, "prequal")


def test_change_announcement_not_auto_deleted_as_tender():
    # 变更/延期公告是已开标项目的附属公告，不是新招标机会：默认排除但保留人工查看
    assert classify_type("XX项目施工招标延期开标公告") == CHANGE
    assert not is_default_include(CHANGE)


# ── 2. 确定性预筛四态 + missing_fields ────────────────────

DEFAULT_CFG = {"industry_keywords": ["施工", "市政"], "exclude_keywords": []}


def test_screen_brief_when_industry_hit():
    s = discovery.screen_candidate(_cand("XX市政道路施工招标公告"), rule_version="builtin-v1", config=DEFAULT_CFG)
    assert s["screen_state"] == "brief"
    assert s["pool_status"] == discovery.POOL_PENDING
    assert "市政" in s["matched_industries"] or "施工" in s["matched_industries"]
    assert s["announcement_type"] == TENDER


def test_screen_needs_review_when_no_industry_word():
    s = discovery.screen_candidate(_cand("XX信息咨询项目招标公告"), rule_version="builtin-v1", config=DEFAULT_CFG)
    assert s["screen_state"] == "needs_manual_review"
    assert s["pool_status"] == discovery.POOL_NEEDS_REVIEW
    assert s["matched_industries"] == []


def test_screen_excluded_non_tender():
    s = discovery.screen_candidate(_cand("XX设备采购中标公示"), rule_version="builtin-v1", config=DEFAULT_CFG)
    assert s["screen_state"] == "excluded_non_tender"
    assert s["pool_status"] == discovery.POOL_EXPIRED
    assert s["announcement_type"] == WIN


def test_screen_exclusion_keyword_keeps_manual_review():
    cfg = {"industry_keywords": ["施工"], "exclude_keywords": ["厂房"]}
    s = discovery.screen_candidate(_cand("XX厂房二期施工招标公告"), rule_version="r1", config=cfg)
    assert s["screen_state"] == "needs_manual_review"
    assert s["pool_status"] == discovery.POOL_NEEDS_REVIEW
    assert s["matched_exclusions"] == ["厂房"]


def test_screen_missing_fields_listed():
    s = discovery.screen_candidate(
        _cand("XX市政施工招标公告", publish_date=None, url="", category=None),
        rule_version="v1", config=DEFAULT_CFG,
    )
    assert "publish_date" in s["missing_fields"]
    assert "source_url" in s["missing_fields"]
    assert "category" in s["missing_fields"]


# ── 3. 待选池同步 ─────────────────────────────────────────

def test_sync_candidate_creates_pool_item(session):
    cand = _cand("XX市政道路施工招标公告")
    session.add(cand)
    item = discovery.sync_candidate(session, cand)
    assert item.pool_status == discovery.POOL_PENDING
    assert item.candidate_id == cand.candidate_id
    assert item.screening["rule_version"] == "builtin-v1"
    assert item.screening["screen_state"] == "brief"


def test_sync_candidate_supersedes_older_pending_same_project(session):
    # 同一项目（相同 project_key）出现新公告 → 旧 pending 被替换为 superseded
    old = _cand("XX市政道路施工招标公告", publish_date=None, candidate_id="cand-old")
    session.add(old)
    first = discovery.sync_candidate(session, old)
    session.flush()
    new = _cand("XX市政道路施工招标公告", candidate_id="cand-new")
    session.add(new)
    second = discovery.sync_candidate(session, new)
    session.flush()
    assert first.candidate_id == "cand-old" and second.candidate_id == "cand-new"
    assert first.pool_status == discovery.POOL_SUPERSEDED
    assert first.replaced_by_candidate_id == new.candidate_id
    assert second.pool_status == discovery.POOL_PENDING


def test_sync_candidate_never_overrides_deep_dive_or_dismissed(session):
    cand = _cand("XX市政道路施工招标公告")
    session.add(cand)
    item = discovery.sync_candidate(session, cand)
    session.flush()
    discovery.set_pool_status(session, candidate_id=cand.candidate_id,
                              status=discovery.POOL_DEEP_DIVE, actor="toubiao")
    session.flush()
    newer = _cand("XX市政道路施工招标公告（重招）")
    session.add(newer)
    discovery.sync_candidate(session, newer)
    session.flush()
    assert item.pool_status == discovery.POOL_DEEP_DIVE  # 人工选择深入不被自动覆盖
    assert item.selected_at is not None


def test_set_pool_status_state_transitions(session):
    cand = _cand("XX市政道路施工招标公告", candidate_id="cand-st")
    session.add(cand)
    item = discovery.sync_candidate(session, cand)
    session.flush()
    # 已深入 → 不可软删除（需走项目工作台处置）
    discovery.set_pool_status(session, candidate_id=cand.candidate_id,
                              status=discovery.POOL_DEEP_DIVE, actor="toubiao")
    session.flush()
    with pytest.raises(ValueError):
        discovery.set_pool_status(session, candidate_id=cand.candidate_id,
                                  status=discovery.POOL_DISMISSED, actor="jingying")
    # 未深入的新候认可直接软删除（软删除保留审计与事实链）
    cand2 = _cand("XX市政道路施工招标公告", candidate_id="cand-dismiss")
    session.add(cand2)
    discovery.sync_candidate(session, cand2)
    session.flush()
    discovery.set_pool_status(session, candidate_id=cand2.candidate_id,
                              status=discovery.POOL_DISMISSED, actor="jingying", reason="不符合")
    session.flush()
    item2 = session.scalar(select(SelectionPoolItem).where(SelectionPoolItem.candidate_id == "cand-dismiss"))
    assert item2.pool_status == discovery.POOL_DISMISSED
    assert item2.dismissed_by == "jingying"
    assert item2.dismissed_reason == "不符合"
    assert item.pool_status == discovery.POOL_DEEP_DIVE


def test_update_rules_creates_new_active_version(session):
    row = discovery.update_rules(
        session, config={"industry_keywords": ["水利"], "exclude_keywords": ["EPC"]}, actor="jingying",
    )
    session.flush()
    assert row.is_active is True
    version, config = discovery.active_rule(session)
    assert version == row.version
    assert config["industry_keywords"] == ["水利"]
    assert config["exclude_keywords"] == ["EPC"]
    # 未登记规则 → 内置缺省
    from sqlalchemy import delete
    session.execute(delete(DiscoveryRuleProfile))
    session.flush()
    version2, cfg2 = discovery.active_rule(session)
    assert version2 == "builtin-v1"
    assert "施工" in cfg2["industry_keywords"]


def test_sync_search_pool_filters_by_job(session):
    for title in ["XX市政道路施工招标公告", "XX设备采购中标公示"]:
        cand = _cand(title)
        cand.search_job_id = "JOB-A"
        session.add(cand)
    other = _cand("XX其他项目施工招标公告")
    other.search_job_id = "JOB-B"
    session.add(other)
    assert discovery.sync_search_pool(session, "JOB-A") == 2
    items = session.scalars(select(SelectionPoolItem)).all()
    assert len(items) == 2


# ── 5. 匹配结果「下一步处置」（#8）─────────────────────────

def test_next_step_of_match_mapping():
    assert next_step_of_match("blocked_missing_data") == NEXT_STEP_SUPPLY_EVIDENCE
    assert next_step_of_match("unverifiable") == NEXT_STEP_SUPPLY_EVIDENCE
    assert next_step_of_match("blocked_hard_requirement") == NEXT_STEP_VERIFY_HARD
    assert next_step_of_match("not_satisfied") == NEXT_STEP_VERIFY_HARD
    assert next_step_of_match("manual_review") == NEXT_STEP_MANUAL_REVIEW
    assert next_step_of_match("satisfied") == NEXT_STEP_NO_ACTION
    assert next_step_of_match("not_evaluated") == NEXT_STEP_WAIT_TASKS
    assert next_step_of_match(None) == NEXT_STEP_WAIT_TASKS


def test_next_step_of_admission_mapping():
    assert next_step_of_admission("blocked_missing_data") == NEXT_STEP_SUPPLY_EVIDENCE
    assert next_step_of_admission("blocked_hard_requirement") == NEXT_STEP_VERIFY_HARD
    assert next_step_of_admission("pending_bid_approval") == NEXT_STEP_SUBMIT_APPROVAL
    assert next_step_of_admission("pending_bid_approval", eligible=True) == NEXT_STEP_SUBMIT_APPROVAL
    assert next_step_of_admission("matching") == NEXT_STEP_WAIT_TASKS
    assert next_step_of_admission("archived") == NEXT_STEP_NO_ACTION
    assert next_step_of_admission("unknown_state") == NEXT_STEP_MANUAL_REVIEW


# ── 6. F026 v1.1：公告要点/截止提取、增量简报、批量删除、规则试跑 ────────────

def _job(session, job_id: str, *, kind: str = "announcement.search", input_ref: str | None = None,
         offset_seconds: float = 0):
    from datetime import datetime, timedelta

    from runtime.db.models import AnalysisJob

    row = AnalysisJob(
        job_id=job_id, kind=kind, status="completed",
        input_ref=input_ref or '{"keyword":"施工","region":"河北省"}',
        idempotency_key=f"idem-{job_id}",
        created_at=datetime.utcnow() - timedelta(seconds=offset_seconds),
    )
    session.add(row)
    session.flush()
    return row


def test_highlights_and_deadline_extraction(session):
    # 未建档候选：无要点、无截止（不推断）
    plain = _cand("XX市政道路施工招标公告")
    assert discovery.candidate_highlights(plain) is None
    assert discovery.candidate_deadline(plain) is None
    # 建档后：detail_summary 的非缺失值进入要点；missing/占位值不进入
    imported = _cand("XX厂房EPC总承包招标公告", candidate_id="cand-hl")
    imported.detail_summary = {
        "qualification": {"value": "建筑工程施工总承包二级及以上", "missing": False},
        "budget_amount": {"value": "1200万元", "missing": False},
        "deadline_bid": {"value": "2026-10-15 09:30", "missing": False},
        "purchaser": {"value": None, "missing": False},
        "duration": {"value": "null", "missing": False},
        "region": {"value": "X", "missing": True},
    }
    highlights = discovery.candidate_highlights(imported)
    assert highlights == {
        "qualification": "建筑工程施工总承包二级及以上",
        "budget_amount": "1200万元",
    }
    assert discovery.candidate_deadline(imported) == "2026-10-15 09:30"


def test_pool_payload_carries_deadline_and_highlights(session):
    imported = _cand("XX厂房EPC总承包招标公告", candidate_id="cand-pay")
    imported.detail_summary = {
        "qualification": {"value": "市政公用工程施工总承包一级", "missing": False},
        "deadline_bid": {"value": "2026-11-01", "missing": False},
    }
    imported.import_status = "imported"
    imported.project_id = "PJ-POOL-1"
    session.add(imported)
    item = discovery.sync_candidate(session, imported)
    session.flush()
    payload = discovery.pool_payload(session, item)
    fact = payload["candidate"]
    assert fact["deadline_bid"] == "2026-11-01"
    assert fact["highlights"]["qualification"] == "市政公用工程施工总承包一级"
    # 建档后必须带回 project_id（已深入页签的「上传招标文件/进入项目」入口依赖它）
    assert fact["project_id"] == "PJ-POOL-1"
    # 未建档候选 project_id 为 null（不推断）
    fresh = _cand("XX市政道路施工招标公告", candidate_id="cand-np")
    session.add(fresh)
    item2 = discovery.sync_candidate(session, fresh)
    session.flush()
    assert discovery.pool_payload(session, item2)["candidate"]["project_id"] is None


def test_search_briefing_counts_latest_job(session):
    _job(session, "JOB-OLD", offset_seconds=3600)
    _job(session, "JOB-NEW")
    tender = _cand("XX市政道路施工招标公告", candidate_id="cand-b1")
    tender.search_job_id = "JOB-NEW"
    win = _cand("XX设备采购中标公示", candidate_id="cand-b2")
    win.search_job_id = "JOB-NEW"
    session.add_all([tender, win])
    discovery.sync_search_pool(session, "JOB-NEW")
    session.flush()
    briefing = discovery.search_briefing(session)
    assert briefing["job"]["job_id"] == "JOB-NEW"
    assert briefing["added"] == 2
    assert briefing["by_status"][discovery.POOL_PENDING] == 1
    assert briefing["auto_excluded"] == 1
    assert briefing["rule_version"] == "builtin-v1"
    # 指定 job 查询口径一致
    assert discovery.search_briefing(session, job_id="JOB-NEW")["added"] == 2
    assert discovery.search_briefing(session, job_id="JOB-OLD")["added"] == 0
    # 无搜索任务 → None（前端显示"暂无简报"）
    from sqlalchemy import delete

    from runtime.db.models import AnalysisJob

    session.execute(delete(AnalysisJob))
    session.flush()
    assert discovery.search_briefing(session) is None


def test_dismiss_batch_walks_state_machine_per_item(session):
    keep = _cand("XX市政道路施工招标公告", candidate_id="cand-keep")
    drop1 = _cand("XX道路改造施工招标公告", candidate_id="cand-drop1")
    drop2 = _cand("XX管网改造施工招标公告", candidate_id="cand-drop2")
    session.add_all([keep, drop1, drop2])
    for cand in (keep, drop1, drop2):
        discovery.sync_candidate(session, cand)
    session.flush()
    discovery.set_pool_status(session, candidate_id=keep.candidate_id,
                              status=discovery.POOL_DEEP_DIVE, actor="toubiao")
    session.flush()
    result = discovery.dismiss_batch(
        session, candidate_ids=[drop1.candidate_id, drop2.candidate_id, keep.candidate_id,
                                drop1.candidate_id, "cand-404"],
        actor="toubiao", reason="批量清理",
    )
    assert result["dismissed"] == ["cand-drop1", "cand-drop2"]
    failed = {f["candidate_id"]: f["error"] for f in result["failed"]}
    assert "已选择深入" in failed["cand-keep"]
    assert failed["cand-404"] == "not_found"
    # 重复提交的 id 只处理一次
    assert result["dismissed"].count("cand-drop1") == 1
    item1 = session.scalar(select(SelectionPoolItem).where(SelectionPoolItem.candidate_id == "cand-drop1"))
    assert item1.pool_status == discovery.POOL_DISMISSED
    assert item1.dismissed_reason == "批量清理"


def test_preview_rules_replays_without_persisting(session):
    session.add_all([
        _cand("XX市政道路施工招标公告", candidate_id="cand-p1"),
        _cand("XX园区物业服务采购项目招标公告", candidate_id="cand-p2"),
        _cand("XX设备采购中标公示", candidate_id="cand-p3"),
    ])
    session.flush()
    result = discovery.preview_rules(
        session, config={"industry_keywords": ["市政"], "exclude_keywords": []}, limit=3,
    )
    assert result["total_sampled"] == 3
    assert result["distribution"]["brief"] == 1
    assert result["distribution"]["needs_manual_review"] == 1
    assert result["distribution"]["excluded_non_tender"] == 1
    assert result["rule_version"] == "preview"
    # 试跑不产生规则版本、不改待选池状态
    assert session.scalar(select(DiscoveryRuleProfile).where(DiscoveryRuleProfile.is_active.is_(True))) is None
    statuses = {i.pool_status for i in session.scalars(select(SelectionPoolItem)).all()}
    assert discovery.POOL_PENDING in statuses or not statuses


# ── 7. ADR-006：大模型辅助预筛与恢复 ─────────────────────

class _FakeResult:
    def __init__(self, data=None, ok=True):
        self.data = data
        self.ok = ok


def _fake_client(verdicts_by_title: dict[str, dict]):
    """按标题返回预置判定（真实链路里由模型产出；此处只测契约与落库）。"""
    def client(messages):
        lines = messages[-1]["content"].splitlines()
        titles = [line.split(". ", 1)[1] for line in lines if line[:1].isdigit()]
        items = []
        for i, t in enumerate(titles):
            v = verdicts_by_title.get(t)
            if v:
                items.append({"idx": i, **v})
        return _FakeResult({"items": items})
    return client


def test_parse_verdicts_guards():
    from runtime.discovery.llm_prescreen import parse_verdicts

    titles = ["XX商铺招租公告", "XX道路施工招标公告"]
    raw = {"items": [
        {"idx": 0, "category": "非招标-招租租赁", "relevant": "no", "reason": "招租", "confidence": "high"},
        {"idx": 1, "category": "不存在的类别", "relevant": "yes", "reason": "施工", "confidence": "high"},   # 词表外 → 丢
        {"idx": 1, "category": "工程施工", "relevant": "yes", "reason": "标题里没有的词", "confidence": "high"},  # 依据词非子串 → 丢
        {"idx": 1, "category": "工程施工", "relevant": "yes", "reason": "施工", "confidence": "high"},
        {"idx": 9, "category": "工程施工", "relevant": "yes", "reason": "施工", "confidence": "high"},          # 越界 idx → 丢
    ]}
    out = parse_verdicts(raw, titles)
    assert set(out) == {0, 1}
    assert out[0]["category"] == "非招标-招租租赁"
    assert out[1]["reason"] == "施工"


def test_llm_prescreen_excludes_non_tender_and_keeps_relevant(session):
    from runtime.discovery import llm_prescreen

    # 两条均需是规则层无法判定的标题（TENDER 类型且无行业词命中 → needs_manual_review）
    nontender = _cand("XX会员单位招募联合公告", candidate_id="cand-l1")
    relevant = _cand("XX园区综合咨询服务项目招标公告", candidate_id="cand-l2")
    session.add_all([nontender, relevant])
    item_nontender = discovery.sync_candidate(session, nontender)
    item_relevant = discovery.sync_candidate(session, relevant)
    session.flush()
    assert item_nontender.pool_status == discovery.POOL_NEEDS_REVIEW
    assert item_relevant.pool_status == discovery.POOL_NEEDS_REVIEW
    client = _fake_client({
        "XX会员单位招募联合公告": {"category": "非招标-其他", "relevant": "no", "reason": "招募", "confidence": "high"},
        "XX园区综合咨询服务项目招标公告": {"category": "服务采购", "relevant": "yes", "reason": "服务", "confidence": "high"},
    })
    stats = llm_prescreen.prescreen_pool_items(session, items=[item_nontender, item_relevant],
                                               client=client, enabled=True)
    assert stats["classified"] == 2 and stats["ai_excluded"] == 1 and stats["promoted"] == 1
    assert item_nontender.pool_status == discovery.POOL_EXPIRED
    assert item_nontender.screening["excluded_by"] == "llm"
    assert "招募" in item_nontender.screening["excluded_reason"]
    # v2：相关 → 晋升 pending（主列表，可直接选择深入）
    assert item_relevant.pool_status == discovery.POOL_PENDING
    assert item_relevant.screening["llm_assist"]["category"] == "服务采购"
    # 低置信不相关不排除（low 回落待人工确认）
    unsure = _cand("XX综合服务信息公告", candidate_id="cand-l3")
    session.add(unsure)
    item_unsure = discovery.sync_candidate(session, unsure)
    session.flush()
    stats2 = llm_prescreen.prescreen_pool_items(
        session, items=[item_unsure],
        client=_fake_client({"XX综合服务信息公告": {"category": "非招标-其他", "relevant": "no",
                                                 "reason": "服务", "confidence": "low"}}),
        enabled=True)
    assert stats2["ai_excluded"] == 0
    assert item_unsure.pool_status == discovery.POOL_NEEDS_REVIEW


def test_llm_prescreen_v2_business_profile_irrelevant_procurement(session):
    """ADR-006 v2：与施工企业业务无关的设备采购（用户实测「兴隆县人民法院智慧警务设备采购」）
    即便它本身是招标公告，也必须 relevant=no → 排除，不进主列表。"""
    from runtime.discovery import llm_prescreen

    it_proc = _cand("兴隆县人民法院智慧警务设备采购项目公开招标公告", candidate_id="cand-x1")
    it_eng = _cand("XX县老旧小区改造工程施工招标公告", candidate_id="cand-x2")
    session.add_all([it_proc, it_eng])
    item_proc = discovery.sync_candidate(session, it_proc)
    item_eng = discovery.sync_candidate(session, it_eng)
    session.flush()
    client = _fake_client({
        "兴隆县人民法院智慧警务设备采购项目公开招标公告": {"category": "设备采购", "relevant": "no",
                                                       "reason": "智慧警务设备采购", "confidence": "high"},
        "XX县老旧小区改造工程施工招标公告": {"category": "房建装修", "relevant": "yes",
                                            "reason": "改造工程", "confidence": "high"},
    })
    llm_prescreen.prescreen_pool_items(session, items=[item_proc, item_eng],
                                       client=client, enabled=True,
                                       business_profile="仅工程施工类企业")
    assert item_proc.pool_status == discovery.POOL_EXPIRED
    assert item_proc.screening["excluded_by"] == "llm"
    assert "业务无关" in item_proc.screening["excluded_reason"]
    assert item_eng.pool_status == discovery.POOL_PENDING


def test_llm_prescreen_fail_open_and_idempotent(session):
    from runtime.discovery import llm_prescreen

    cand = _cand("XX园区综合咨询服务项目招标公告", candidate_id="cand-f1")
    session.add(cand)
    item = discovery.sync_candidate(session, cand)
    session.flush()
    # 开关关闭 → 零副作用
    stats = llm_prescreen.prescreen_pool_items(session, items=[item], enabled=False)
    assert stats["classified"] == 0 and item.pool_status == discovery.POOL_NEEDS_REVIEW
    # 调用失败 → 回落人工，不抛异常
    def broken(messages):
        raise RuntimeError("network down")
    stats = llm_prescreen.prescreen_pool_items(session, items=[item], client=broken, enabled=True)
    assert stats["classified"] == 0
    assert item.pool_status == discovery.POOL_NEEDS_REVIEW
    # 已有 llm_assist → 幂等跳过
    good = _fake_client({"XX园区综合咨询服务项目招标公告": {"category": "服务采购", "relevant": "yes",
                                                    "reason": "服务", "confidence": "high"}})
    llm_prescreen.prescreen_pool_items(session, items=[item], client=good, enabled=True)
    first_at = item.screening["llm_assist"]["at"]
    stats = llm_prescreen.prescreen_pool_items(session, items=[item], client=good, enabled=True)
    assert stats["processed"] == 0  # 已分类，不再调用
    assert item.screening["llm_assist"]["at"] == first_at


def test_llm_prescreen_strategy_exclusion_takes_precedence(session):
    """策略排除优先于 AI（2026-09-24）：规则层命中排除关键词（如监理/咨询）的条目
    停留待人工确认，AI 不得晋级回主列表，也不代为排除。"""
    from runtime.discovery import llm_prescreen

    # 先落一条规则版本，使 sync_candidate 命中排除关键词（标题含"监理"）
    discovery.update_rules(session, config={
        "industry_keywords": ["施工", "工程"],
        "exclude_keywords": ["咨询", "科研", "监理", "勘察设计", "设备采购"],
        "business_profile": "",
    }, actor="fuzeren")
    session.flush()
    excluded = _cand("XX零碳光伏发电及储能项目监理招标公告", candidate_id="cand-se1")
    normal = _cand("XX老旧小区改造工程施工招标公告", candidate_id="cand-se2")
    session.add_all([excluded, normal])
    item_excluded = discovery.sync_candidate(session, excluded)
    item_normal = discovery.sync_candidate(session, normal)
    session.flush()
    # 规则层：命中排除词 → needs_manual_review 且带 matched_exclusions
    assert item_excluded.pool_status == discovery.POOL_NEEDS_REVIEW
    assert "监理" in item_excluded.screening["matched_exclusions"]
    assert item_normal.pool_status == discovery.POOL_PENDING

    client = _fake_client({
        "XX零碳光伏发电及储能项目监理招标公告": {"category": "监理造价", "relevant": "yes",
                                              "reason": "监理", "confidence": "high"},
        "XX老旧小区改造工程施工招标公告": {"category": "房建装修", "relevant": "yes",
                                            "reason": "改造工程", "confidence": "high"},
    })
    stats = llm_prescreen.prescreen_pool_items(session, items=[item_excluded, item_normal],
                                               client=client, enabled=True)
    # 排除词条目不进 LLM（kept_manual）；normal（pending 无 llm_assist）正常走 LLM
    assert stats["classified"] == 1
    assert stats["promoted"] == 1
    assert stats["kept_manual"] == 1
    assert item_excluded.pool_status == discovery.POOL_NEEDS_REVIEW
    assert "llm_assist" not in (item_excluded.screening or {})
    assert item_normal.pool_status == discovery.POOL_PENDING


def test_restore_only_for_llm_excluded(session):
    lease = _cand("XX区临街商铺招租公告", candidate_id="cand-r1")
    win = _cand("XX设备采购中标公示", candidate_id="cand-r2")
    session.add_all([lease, win])
    item_lease = discovery.sync_candidate(session, lease)
    item_win = discovery.sync_candidate(session, win)   # 确定性排除 → expired，无 excluded_by
    session.flush()
    item_lease.pool_status = discovery.POOL_EXPIRED
    item_lease.screening = {**item_lease.screening, "excluded_by": "llm",
                            "excluded_reason": "AI 判定非招标"}
    session.flush()
    restored = discovery.restore_candidate(session, candidate_id="cand-r1", actor="toubiao")
    assert restored.pool_status == discovery.POOL_NEEDS_REVIEW
    assert restored.screening["restored_by"] == "toubiao"
    # 确定性排除不可恢复；人工删除不可恢复
    with pytest.raises(ValueError):
        discovery.restore_candidate(session, candidate_id="cand-r2", actor="toubiao")
    # 造一条人工删除态：同样不可经 restore 恢复
    item_win.pool_status = discovery.POOL_NEEDS_REVIEW
    session.flush()
    discovery.set_pool_status(session, candidate_id="cand-r2", status=discovery.POOL_DISMISSED, actor="toubiao")
    with pytest.raises(ValueError):
        discovery.restore_candidate(session, candidate_id="cand-r2", actor="toubiao")
    with pytest.raises(KeyError):
        discovery.restore_candidate(session, candidate_id="cand-404", actor="toubiao")