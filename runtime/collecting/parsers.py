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

# 通用公告条目：<a href="…" title="标题">…</a>（惠招标/新点系平台公告链接均为站内相对路径）。
# href 允许未加引号形态（省平台交易页 <a href=/jyxx/… title=…>，2026-09-10 实测）。
_RE_HREF_TITLE = re.compile(
    r'<a[^>]+href=(?:"([^"]+)"|([^\s>]+))[^>]*title="([^"]*)"[^>]*>(.*?)</a>', re.S)
# 兜底：任意站内链接 + 公告类文本（过滤 javascript/空文本）
_RE_ANCHOR = re.compile(
    r'<a[^>]+href=(?:"([^"]+)"|([^\s>]+))[^>]*>(.*?)</a>', re.S)
_ANNOUNCE_KEYWORDS = ("公告", "采购", "公示", "招标", "中标", "磋商", "比选", "询价")

# 发布日期提取（页面明文优先，URL 日期段回退）：
# 明文形态：发布日期：2026-08-22 ｜ 发布日期：<span>2026-08-22</span> ｜ 2026/08/22 ｜ 2026.08.22
_RE_DATE_PLAIN = re.compile(r"发布日期[:：]\s*(?:<span[^>]*>)?\s*(20\d\d[-/.]\d{1,2}[-/.]\d{1,2})", re.I)
_RE_DATE_ISO = re.compile(r"20\d\d[-/.]\d{1,2}[-/.]\d{1,2}")
# URL 日期段：/jyxx/…/20260822/uuid.html（惠招标详情 URL 内容路径自带发布日）
_RE_DATE_URL = re.compile(r"/(20\d{6})/")
# URL 日期段（河北政采形态）：/cd/cd_kfq/cggg/zbggAAAA/202609/t20260910_2426123.html
_RE_DATE_URL_T = re.compile(r"[/?]t(20\d{6})_")
# 同一行边界：tail 在遇到下一个条目起点（<a / <li）即截断，防止把下一条目的
# 日期错配给当前条目（就近回退只在「本行」内找，B1 2026-09-10）
_RE_ROW_BOUNDARY = re.compile(r"<a[\s>]|<li[\s>]")
# HTML 注释剥离：省平台交易页把废弃锚点整段注释（<!-- <a href=… title=""> -->，
# 无闭合 </a>），不剥会让 (.*?)</a> 跨注释吞掉真实条目（2026-09-10 实测）
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
# <script> 块（列表解析用）：脚本内的 "<li><a href=…>" 是 JS 模板串而非静态条目
_RE_SCRIPT_BLOCK = re.compile(r"<script\b.*?</script>", re.S | re.I)
# 标题尾部独立日期剥离：邯郸列表锚内 <span class="date"> 2026-09-09</span> 会让
# 兜底文本变成「标题 2026-09-09」，展示/关键词匹配前先确定性去掉
_RE_TITLE_TRAILING_DATE = re.compile(r"[\s\u3000]*\[?(20\d\d[-/.]\d{1,2}[-/.]\d{1,2})\]?\s*$")


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


# ── 详情页发布日期回填（2026-09-09 日期兜底）───────────────────────────────
# 背景：列表页 `_publish_date` 只认「发布日期：[大写]」+ URL 日期段，很多平台列表页两者皆无
#   → 候选 publish_date=null → 前端显示"待详情页确认"。这不满足"至少要有发布日"。
# 兜底：import_candidate_detail 抓详情后，从详情 HTML/净化文本提取发布时间回填候选表。
#  统一来源（确定性，不含推断）：
#    ① 详情正文「发布时间/发布日期/公告日期：YYYY-MM-DD」
#    ② 详情 HTML 时间标签 <time>/<span class=…date…>
#    ③ URL date 段（lc_）回退
_DETAIL_RE_PLAIN = re.compile(
    r"(?:发布时间|发布日期|公告日期):?\s*(?:<span[^>]*>\s*)?(20\d\d[-/.]\d{1,2}[-/.]\d{1,2})",
    re.I)


def extract_detail_publish_date(html: str, text: str, url: str = "") -> date | None:
    """从详情页（HTML 净化前置）提取发布时间；无则纳 URL 日期段兜底。

    确定性：可确证才返回；无法判定 → None（保持待详情，不编造）。
    """
    m = _DETAIL_RE_PLAIN.search(text or "") or _DETAIL_RE_PLAIN.search(html or "")
    if m:
        d = _to_date(m.group(1))
        if d is not None:
            return d
    m = _RE_DATE_ISO.search(text or "")
    if m:
        d = _to_date(m.group(0))
        if d is not None and d <= date.today():
            return d
    m = _RE_DATE_URL.search(url or "")
    if m:
        return _to_date(m.group(1))
    return None


def _row_date(text: str) -> date | None:
    """「同一行内就近 YYYY-MM-DD」回退（B1，2026-09-10）：在条目行片段中找
    第一个日历日期（YYYY-MM-DD / YYYY.M.D / YYYY/M/D）。确定性子串匹配，
    非推断；调用方须先用 _RE_ROW_BOUNDARY 截断片段，避免跨条目错配。"""
    m = _RE_DATE_ISO.search(text or "")
    if m:
        return _to_date(m.group(0))
    return None


def _publish_date(href: str, tail: str, inner: str = "") -> date | None:
    """从条目行内文本 + 链接提取发布日期（B1 升级，2026-09-10）。

    优先级（可确证强度从高到低）：
      ① 行内「发布日期：」明文（页面显式标注）；
      ② 同一行内就近日期——tail（</a> 之后、下一个条目之前的片段）与锚内文本
         （邯郸 <span class="date"> 在 <a> 内）分别找首个日期，tail 优先；
      ③ URL 日期段（/20260822/ 与河北政采 t20260822_ 形态）。
    三者皆无 → None（详情页回源，不推断）。
    """
    m = _RE_DATE_PLAIN.search(tail or "") or _RE_DATE_PLAIN.search(inner or "")
    if m:
        d = _to_date(m.group(1))
        if d is not None:
            return d
    # 同一行就近回退：tail 先截断到下一个条目起点，再找行内日期
    row = tail or ""
    bound = _RE_ROW_BOUNDARY.search(row)
    if bound:
        row = row[:bound.start()]
    d = _row_date(row) or _row_date(inner or "")
    if d is not None:
        return d
    m = _RE_DATE_URL.search(href or "")
    if m:
        d = _to_date(m.group(1))
        if d is not None:
            return d
    m = _RE_DATE_URL_T.search(href or "")
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
# td/th 也按块级处理（2026-09-11）：表单式公告 <td>是否接受联合体投标</td><td>否</td> 否则会粘成
# 「是否接受联合体投标否」，标签与值失去边界，是极性误读的温床（docs/10 附录 A parsers #1）。
# 注：末尾 _WS 会把块边界折叠为单个空格（既有单行输出口径），边界仍保留。
_RE_BLOCK = re.compile(r"</?(?:p|div|tr|td|th|li|h[1-6]|br|table|ul|ol)[^>]*>", re.I)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_ANY_TAG = _RE_TAG  # 锚内嵌套标签剥离（与正文净化同口径）
_RE_BLANK = re.compile(r"[ \t\f\v]+")
_RE_NL = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    return _WS.sub(" ", _html.unescape(text)).strip()


def _strip_tags(fragment: str) -> str:
    """剥去锚内嵌套标签（邯郸 <span class="inf">标题</span> 等），保留纯文本。"""
    return _html.unescape(_RE_ANY_TAG.sub("", fragment or ""))


def _entry(title: str, href: str, category: str, publish_date: date | None) -> dict:
    return {"title": title, "url": href, "category": category,
            "publish_date": publish_date.isoformat() if publish_date else None}


def parse_announce_list(html: str, source: SourceSpec) -> list[dict]:
    """解析公告列表页 → [{title, url, category, publish_date}]（静态列表通用）。

    href 站内相对路径拼 base_url 绝对链接；正文兜底需含公告类关键词且长度 ≥ 8
    （与 fetch_ebidding_lists.py 口径一致，避免把导航/工具链接当公告）。
    publish_date（B1 升级）：「发布日期：」明文 > 同一行就近日期（tail 行内
    span/div 与锚内 date span）> URL 日期段 > None（可确证，不推断）。
    """
    out: list[dict] = []
    seen: set[str] = set()

    # 先剥 HTML 注释与 <script> 块：省平台把废弃锚点整段注释（无闭合 </a>）、
    # 又在脚本里拼 "<li><a href=…>" 模板串（+ ob.projectName +）——不剥会被
    # 当成条目（标题/日期均不可确证，2026-09-10 实测）。
    html = _RE_COMMENT.sub(" ", html or "")
    html = _RE_SCRIPT_BLOCK.sub(" ", html)

    # 列表页当前 URL，用于把站内相对路径（含 ../../../ 相对上级）解析为绝对 URL
    base_page = source.list_url

    def _add(title: str, href: str, tail: str = "", inner: str = "") -> None:
        title = _clean(title)
        # 标题尾部独立日期剥离（邯郸锚内 date span 混入兜底文本）
        tmatch = _RE_TITLE_TRAILING_DATE.search(title)
        if tmatch:
            title = title[:tmatch.start()].rstrip("　 \t-[]")
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
        out.append(_entry(title, abs_url, source.category, _publish_date(href, tail, inner)))

    for m in _RE_HREF_TITLE.finditer(html):
        href = m.group(1) or m.group(2) or ""
        title = m.group(3) or _clean(m.group(4))
        # tail：<a> 闭合后片段（列表行内「发布日期：…」span / 行内日期元素紧随其后）
        tail = html[m.end():m.end() + 200]
        _add(title, href, tail)
    if out:
        return out
    # 无 title 属性命中（邯郸/石家庄等）→ 文本兜底（含公告类关键词且长度 ≥ 8）；
    # inner 去标签后作为标题（防 <span> 结构污染），并参与就近日期回退
    for m in _RE_ANCHOR.finditer(html):
        href = m.group(1) or m.group(2) or ""
        inner = m.group(3)
        text = _clean(_strip_tags(inner))
        tmatch = _RE_TITLE_TRAILING_DATE.search(text)
        if tmatch:
            text = text[:tmatch.start()].rstrip("　 \t-[]")
        if "javascript" in href or "void" in href or len(text) < 8:
            continue
        if any(k in text for k in _ANNOUNCE_KEYWORDS):
            tail = html[m.end():m.end() + 200]
            _add(text, href, tail, inner)
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