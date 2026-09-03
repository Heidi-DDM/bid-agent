# R021/F004：URL 抓取器（manual_trigger 单条 URL 入库通道）
# 合规：透明 UA + robots.txt 预检 + 限频由调用方（worker/路由）用 compliance 模块执行；
# 本模块只负责"带超时抓取文本/robots.txt"，不绕过任何技术保护、不处理登录/付费内容。
# 所有抓取函数支持注入（fetch_fn/get_robots_fn），便于无网络单测。
from __future__ import annotations

import logging
import urllib.request
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from runtime.core.compliance import TRANSPARENT_UA

logger = logging.getLogger("runtime.core.fetcher")

FETCH_TIMEOUT_SECONDS = 15
MAX_CONTENT_BYTES = 20 * 1024 * 1024  # 单条公告/正文上限 20MB（防止内存放大）

ROBOTS_PATH = "/robots.txt"


class FetchError(Exception):
    """抓取失败（网络/HTTP 错误/超时/超限）。"""


class RobotsUnavailable(FetchError):
    """robots.txt 不可达：保守处置=人工复核，不静默抓取（schema §5.2）。"""


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": TRANSPARENT_UA})


def fetch_robots(url: str, *, timeout: int = FETCH_TIMEOUT_SECONDS) -> str | None:
    """获取站点 robots.txt 文本。返回 None 表示站点无 robots.txt（允许抓取）。

    HTTP 404/空文件视为"站点无 robots.txt"（RFC 9309：无规则 = 默认允许，
    由调用方记录 fetched=False 供审计复核）；网络错误/超时/5xx 抛
    RobotsUnavailable（保守：不可判定时交人工复核，不让抓取动作在无 robots
    保护下静默发生）。
    """
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}{ROBOTS_PATH}"
    try:
        with urllib.request.urlopen(_request(robots_url), timeout=timeout) as resp:
            data = resp.read(MAX_CONTENT_BYTES + 1)
            if len(data) > MAX_CONTENT_BYTES:
                raise RobotsUnavailable("robots.txt 超过大小上限")
            text = data.decode("utf-8", errors="replace")
            return text if text.strip() else None
    except HTTPError as exc:
        if exc.code == 404:
            return None  # 站点无 robots.txt 文件（区别于网络故障，RFC 9309 允许抓取）
        logger.warning("robots.txt 获取失败 url=%s err=%s", robots_url, type(exc).__name__)
        raise RobotsUnavailable(f"robots.txt 不可达: {robots_url}（{type(exc).__name__}）") from exc
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("robots.txt 获取失败 url=%s err=%s", robots_url, type(exc).__name__)
        raise RobotsUnavailable(f"robots.txt 不可达: {robots_url}（{type(exc).__name__}）") from exc


def fetch_text(url: str, *, timeout: int = FETCH_TIMEOUT_SECONDS) -> str:
    """抓取单条 URL 的正文文本（仅文本内容，不解析登录/付费页）。

    失败抛 FetchError；调用方捕获后转 manual_review/retryable，不伪造成功。
    """
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read(MAX_CONTENT_BYTES + 1)
            if len(data) > MAX_CONTENT_BYTES:
                raise FetchError("正文超过大小上限（20MB）")
            charset = resp.headers.get_content_charset() or _guess_charset(ctype)
            return data.decode(charset, errors="replace")
    except FetchError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        logger.warning("抓取失败 url=%s err=%s", url, type(exc).__name__)
        raise FetchError(f"抓取失败: {type(exc).__name__}") from exc


def _guess_charset(content_type: str) -> str:
    ct = content_type.lower()
    if "gbk" in ct or "gb2312" in ct or "gb18030" in ct:
        return "gb18030"
    if "big5" in ct:
        return "big5"
    return "utf-8"


# ── 可注入别名（测试/降级用） ─────────────────────────────────────────

FetchFn = Callable[[str], str]
GetRobotsFn = Callable[[str], str | None]


def noop_fetch(url: str) -> str:  # pragma: no cover - 测试替身占位
    raise FetchError("noop fetch（测试环境未配置网络抓取）")
