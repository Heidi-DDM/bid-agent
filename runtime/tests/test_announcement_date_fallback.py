# -*- coding: utf-8 -*-
# R004：日期兜底测试 —— 列表页无发布日时，详情页提取回填。
# 覆盖：详情正文日期 / 裸 ISO / URL 日期段 / 无法判定 → None（不编造）。
from __future__ import annotations

from datetime import date

from runtime.collecting.parsers import extract_detail_publish_date


def test_detail_publish_date_from_plain_label():
    html = "<html></html>"
    text = "发布时间：2026-09-03"
    assert extract_detail_publish_date(html, text) == date(2026, 9, 3)


def test_detail_publish_date_from_iso_in_text():
    html = ""
    text = "忻州市 招标公告 2026.09.03 发布"
    assert extract_detail_publish_date(html, text) == date(2026, 9, 3)


def test_detail_publish_date_from_url():
    html = ""
    text = "无日期正文"
    url = "https://hebtig.com/jyxx/20260822/abc.html"
    assert extract_detail_publish_date(html, text, url=url) == date(2026, 8, 22)


def test_detail_publish_date_missing_returns_none():
    html = ""
    text = "没有任何日期文字"
    url = "https://platform.com/notice/detail?id=123"
    assert extract_detail_publish_date(html, text, url=url) is None


def test_detail_publish_date_future_iso_rejected():
    # 未来日期不作为发布日（确定性防御：正文可能含开标/截止日）
    html = ""
    text = "投标截止 2099-01-01"
    # 详情页若无「发布时间/发布日期」标签，裸 ISO 只认 ≤today 的，2099 被拒 → None
    assert extract_detail_publish_date(html, text) is None