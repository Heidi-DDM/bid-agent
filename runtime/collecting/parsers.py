# R004/F004：公告列表页解析器（纯函数，无网络/DB 依赖）
# 实现口径：只提取页面中可确证的结构化事实（标题 + 详情链接 + 所属分区 + 列表页
# 明示发布日），解析不出的一律缺省（None），禁止推断（AGENTS.md 规则 1）。
# 页面结构依据 scripts/fetch_ebidding_lists.py 实测（惠招标列表每行：
#   <a title="…" href="/jyxx/…/20260822/uuid.html">…</a>
#   <span class="time detail-info">发布日期：<span>2026-08-22</span></span>）
# 发布日期提取口径（可确证，非推断）：行内「发布日期：YYYY-MM-DD」明文 > URL 日期段
# （/20260822/）> 无（None，详情页回源）。2026-09-07 补：此前只取 <a title> 丢弃了
# 日期结构→候选卡日期恒 null 标"待补"，是解析器缺陷，非平台"未达标"。
from __future__ import annotations

import html as _html
import re
from datetime import date, datetime
from urllib.parse import urljoin

from runtime.collecting.registry import SourceSpec

# 通用公告条目：<a href="…" title="标题">…</a>（惠招标/新点系平台公告链接均为站内相对路径）
_RE_HREF_TITLE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*title="([^"]*)"[^>]*>(.*?)</a>', re.S)
# 兜底：任意站内链接 + 公告类文本（过滤 javascript/空文本）
_RE_ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_ANNOUNCE_KEYWORDS = ("公告", "采购", "公示", "招标", "中标", "磋商", "比选", "询价")

# 发布日期提取（页面明文优先，URL 日期段回退）：
# 明文形态：发布日期：2026-08-22 ｜ 发布日期：<span>2026-08-22</span> ｜ 2026/08/22 ｜ 2026.08.22
_RE_DATE_PLAIN = re.compile(r"发布日期[:：]\s*(?:<span[^>]*>)?\s*(20\d\d[-/.]\d{1,2}[-/.]\d{1,2})", re.I)
_RE_DATE_ISO = re.compile(r"20\d\d[-/.]\d{1,2}[-/.]\d{1,2}")
# URL 日期段：/jyxx/…/20260822/uuid.html（惠招标详情 URL 内容路径自带发布日）
_RE_DATE_URL = re.compile(r"/(20\d{6})/")


def _to_date(text: str) -> date | None:
    """确定性解析日期字符串（严格格式，不推断）。"""
    s = (text or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _publish_date(href: str, tail: str) -> date | None:
    """从条目行内文本 + 链接提取发布日期：明文优先，URL 日期段回退。

    tail 为 <a> 闭合后的同行/行内片段（含「发布日期：」span）；href 为详情链接
    （惠招标 URL 带 /YYYYMMDD/ 日期段）。两者皆无 → None（详情页回源）。
    """
    m = _RE_DATE_PLAIN.search(tail or "")
    if m:
        d = _to_date(m.group(1))
        if d is not None:
            return d
    m = _RE_DATE_URL.search(href or "")
    if m:
        d = _to_date(m.group(1))
        if d is not None:
            return d
    return None


_WS = re.compile(r"\s+")
# 正文净化（对象库禁止 .html/.htm 扩展名——防脚本内容入库，F019 §4 FORBIDDEN_SUFFIXES）：
# 公告详情原文入库统一存"净化文本"（.txt）：剔除可执行内容后保留文档事实，
# 对象内容为净化后文本而非原始 HTML（口径见 docs/04-修改日志.md）。
_RE_SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
_RE_STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)
_RE_BLOCK = re.compile(r"</?(?:p|div|tr|li|h[1-6]|br|table|ul|ol)[^>]*>", re.I)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_BLANK = re.compile(r"[ \t\f\v]+")
_RE_NL = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    return _WS.sub(" ", _html.unescape(text)).strip()


def _entry(title: str, href: str, category: str, publish_date: date | None) -> dict:
    return {"title": title, "url": href, "category": category,
            "publish_date": publish_date.isoformat() if publish_date else None}


def parse_announce_list(html: str, source: SourceSpec) -> list[dict]:
    """解析公告列表页 → [{title, url, category, publish_date}]（惠招标等站内相对链接静态列表通用）。

    href 站内相对路径拼 base_url 绝对链接；正文兜底需含公告类关键词且长度 ≥ 8
    （与 fetch_ebidding_lists.py 口径一致，避免把导航/工具链接当公告）。
    publish_date：列表页明文「发布日期」> URL 日期段 > None（可确证，不推断）。
    """
    out: list[dict] = []
    seen: set[str] = set()

    # 列表页当前 URL，用于把站内相对路径（含 ../../../ 相对上级）解析为绝对 URL
    base_page = source.list_url

    def _add(title: str, href: str, tail: str = "") -> None:
        title = _clean(title)
        if not title or len(title) < 8 or title in seen:
            return
        href = href.strip()
        if not href or "javascript" in href or "void" in href:
            return
        if href.startswith("//"):          # 协议相对 URL
            href = f"https:{href}"
        try:
            abs_url = urljoin(base_page, href)
        except ValueError:
            return
        if not abs_url.startswith(("http://", "https://")):
            return
        seen.add(title)
        out.append(_entry(title, abs_url, source.category, _publish_date(href, tail)))

    for m in _RE_HREF_TITLE.finditer(html):
        href, title = m.group(1), m.group(2) or _clean(m.group(3))
        # tail：<a> 闭合后片段（列表行内「发布日期：…」span 紧随其后）
        tail = html[m.end():m.end() + 200]
        _add(title, href, tail)
    if out:
        return out
    # 无 title 属性命中（如部分服务类页面）→ 文本兜底（含公告类关键词且长度 ≥ 8）
    for m in _RE_ANCHOR.finditer(html):
        href, text = m.group(1), _clean(m.group(2))
        if "javascript" in href or "void" in href or len(text) < 8:
            continue
        if any(k in text for k in _ANNOUNCE_KEYWORDS):
            tail = html[m.end():m.end() + 200]
            _add(text, href, tail)
    return out


parse_hebtig_list = parse_announce_list  # 兼容别名（历史 import；新代码用 parse_announce_list）


def html_to_text(html: str) -> str:
    """公告详情页净化文本：剔除 script/style（可执行内容，禁止入库）后转可见文本。

    保留文档事实（标题/段落/表格文本），不解析登录/付费内容；
    净化为保真降级（结构丢失不推断补全），供对象库以 .txt 固化原文。
    """
    if not html or not html.strip():
        return ""
    text = _RE_SCRIPT.sub(" ", html)
    text = _RE_STYLE.sub(" ", text)
    text = _RE_BLOCK.sub("\n", text)  # 块级标签转行，保留阅读顺序
    text = _RE_TAG.sub("", text)
    text = _html.unescape(text)
    text = _RE_BLANK.sub(" ", text)
    text = _WS.sub(" ", text)
    text = _RE_NL.sub("\n\n", text)
    return text.strip()