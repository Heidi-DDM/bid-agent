# R004/F004：公告列表页解析器（纯函数，无网络/DB 依赖）
# 实现口径：只提取页面中可确证的结构化事实（标题 + 详情链接 + 所属分区），
# 解析不出的一律缺省（None），禁止推断（AGENTS.md 规则 1）。
# 页面结构依据 scripts/fetch_ebidding_lists.py 对惠招标列表页的实测正则；
# 列表页不逐条标注日期/地区 → publish_date/region 不产出，交由详情页人工回源确认。
from __future__ import annotations

import html as _html
import re

from runtime.collecting.registry import SourceSpec

# 惠招标列表页公告条目（实测结构：<a href="/jyxx/..." title="标题">…</a>）
_RE_HREF_TITLE = re.compile(r'<a[^>]+href="(/jyxx/[^"]+)"[^>]*title="([^"]*)"[^>]*>(.*?)</a>', re.S)
# 兜底：任意站内链接 + 公告类文本（过滤 javascript/空文本）
_RE_ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_ANNOUNCE_KEYWORDS = ("公告", "采购", "公示", "招标", "中标", "磋商", "比选", "询价")

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


def _entry(title: str, href: str, category: str) -> dict:
    return {"title": title, "url": href, "category": category}


def parse_hebtig_list(html: str, source: SourceSpec) -> list[dict]:
    """解析惠招标列表页 → [{title, url, category}]。

    href 站内相对路径拼 base_url 绝对链接；正文兜底需含公告类关键词且长度 ≥ 8
    （与 fetch_ebidding_lists.py 口径一致，避免把导航/工具链接当公告）。
    """
    out: list[dict] = []
    seen: set[str] = set()

    def _add(title: str, href: str) -> None:
        title = _clean(title)
        if not title or len(title) < 8 or title in seen:
            return
        if href.startswith("/") and not href.startswith("//"):
            seen.add(title)
            out.append(_entry(title, f"{source.base_url}{href}", source.category))

    for m in _RE_HREF_TITLE.finditer(html):
        href, title = m.group(1), m.group(2) or _clean(m.group(3))
        _add(title, href)
    if out:
        return out
    # 无 title 属性命中（如部分服务类页面）→ 文本兜底
    for m in _RE_ANCHOR.finditer(html):
        href, text = m.group(1), _clean(m.group(2))
        if "javascript" in href or "void" in href or len(text) < 8:
            continue
        if any(k in text for k in _ANNOUNCE_KEYWORDS):
            _add(text, href)
    return out


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