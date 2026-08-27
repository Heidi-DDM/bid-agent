#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G4 前置：浏览器级链接可达性复检（2026-08-17 修复"curl 200 但浏览器打不开"）

背景：ggzy.gov.cn / ebidding.hebtig.com / eps.ctg.com.cn / chnenergybidding.com.cn
等平台服务器 TLS 配置老旧，拒绝现代浏览器默认的 TLS 1.3 握手（ERR_CONNECTION_CLOSED），
但 curl（LibreSSL 协商 TLS 1.2）与 urllib 探测均返回 200 —— 旧复检脚本误判"可访问"。

本脚本双通道检测：
  通道① curl：内容层面存在性（HEAD→GET 降级，3 次指数退避）—— 沿用旧逻辑
  通道② Chrome headless：真实浏览器 TLS 握手 —— 判定用户能否点击打开

输出三态：
  ✅ browser_ok        浏览器可访问（stdout 有 HTML 且无握手错误）
  ⚠️ browser_blocked   浏览器握手被拒（ERR_CONNECTION_CLOSED / handshake failed 等），
                        但 curl 200（内容存在）→ 推送需标注"链接可能打不开"+备选入口
  ❌ unreachable       内容层面不可达（curl 失败）

用法: python3 check_links_browser.py [url1 url2 ...] 或 管道输入（每行一个 URL）
"""
import sys, time, json, ssl, subprocess, urllib.request, urllib.error
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 已知 TLS 不兼容平台 → 备选入口（推送卡片标注用）
FALLBACK_ENTRIES = {
    "ggzy.gov.cn": "https://www.ggzy.gov.cn/（全国公共资源交易平台，首页搜索公告标题）",
    "ebidding.hebtig.com": "https://ebidding.hebtig.com/（河北交投招标与采购服务平台）",
    "eps.ctg.com.cn": "https://eps.ctg.com.cn/（中国长江三峡集团电子采购平台）",
    "chnenergybidding.com.cn": "https://www.chnenergybidding.com.cn/（国能e招）",
}

# Chrome stderr 中表示"真实浏览器打不开"的错误特征
_BROWSER_FAIL_PATTERNS = [
    "handshake failed", "ERR_CONNECTION_CLOSED", "ERR_SSL_PROTOCOL_ERROR",
    "ERR_CONNECTION_RESET", "ERR_SSL_VERSION_OR_CIPHER_MISMATCH",
    "ERR_CERT_AUTHORITY_INVALID", "ERR_CERT_COMMON_NAME_INVALID",
    "net_error", "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_TIMED_OUT",
    "ERR_EMPTY_RESPONSE", "ERR_INVALID_HTTP_RESPONSE",
]

_CTX = None
def _ctx():
    global _CTX
    if _CTX is None:
        _CTX = ssl.create_default_context()
        _CTX.check_hostname = False
        _CTX.verify_mode = ssl.CERT_NONE
    return _CTX


def curl_check(url, retries=3):
    """通道①：内容层面可达性（HEAD→GET 降级）。返回 (status, content_type, error)。"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 Chrome/126.0",
                "Accept": "text/html,application/pdf,application/msword,*/*",
            })
            with _ctx_wrap(req).open(req, timeout=15) as r:
                return r.status, r.headers.get("Content-Type", ""), None
        except urllib.error.HTTPError as e:
            # 403/405 常见于平台拒绝 HEAD → 立即降级 GET（Range 只取头 1KB）
            if e.code in (403, 405):
                try:
                    req = urllib.request.Request(url, method="GET", headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                      "AppleWebKit/537.36 Chrome/126.0",
                        "Range": "bytes=0-1023",
                    })
                    with _ctx_wrap(req).open(req, timeout=15) as r:
                        return r.status, r.headers.get("Content-Type", ""), None
                except Exception as e2:
                    return e.code, "", f"HEAD {e.code} / GET fail: {e2}"
            return e.code, "", None
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return 0, "", f"network error after {retries} attempts: {last_err}"


def _ctx_wrap(req):
    """构造带自定义 ssl context 的 opener（复用 _ctx）。"""
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx()))


def chrome_check(url, timeout=30):
    """通道②：Chrome headless 真实握手。返回 (status, errors, has_html)。

    status: "ok" | "blocked" | "fail" | "no_chrome"
    """
    if not Path(CHROME).exists():
        return "no_chrome", ["Chrome 不存在"], False
    try:
        proc = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--dump-dom",
             "--virtual-time-budget=6000", "--timeout=15000", url],
            capture_output=True, text=True, timeout=timeout,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())},
        )
    except subprocess.TimeoutExpired:
        # 握手挂起（服务器对 TLS 1.3 半开连接不响应）与握手失败等价：浏览器打不开
        return "blocked", ["chrome timeout (handshake hang)"], False
    stderr = proc.stderr or ""
    stdout = proc.stdout or ""
    has_html = bool(stdout.strip()) and ("<!DOCTYPE" in stdout or "<html" in stdout or "<title" in stdout)
    errors = [p for p in _BROWSER_FAIL_PATTERNS if p in stderr]
    # Chrome 错误页特征（握手失败/连接被拒时 Chrome 渲染的错误页 HTML）
    error_page_markers = ["无法访问此网站", "ERR_CONNECTION", "ERR_SSL", "ERR_EMPTY_RESPONSE",
                          "ERR_TIMED_OUT", "ERR_NAME_NOT_RESOLVED", "This site can't be reached",
                          "isn't currently available"]
    is_error_page = any(m in stdout for m in error_page_markers)
    if has_html and not is_error_page:
        return "ok", [], True
    if is_error_page or (errors and not has_html):
        return "blocked", errors or ["chrome error page"], has_html
    if has_html:  # 有 HTML 但含错误页标记
        return "blocked", errors or ["error page marker"], has_html
    if proc.returncode != 0:
        return "fail", [f"exit {proc.returncode}"], has_html
    return "fail", ["no html output"], has_html


def classify(url):
    """双通道综合判定。返回 dict。"""
    st, ct, err = curl_check(url)
    curl_ok = (200 <= st < 400) if st else False
    bstatus, berrors, has_html = chrome_check(url)

    # 已知 TLS 不稳定平台名单（即使单次检测通过，也提示附备选入口——服务器时好时坏）
    fallback = next((v for k, v in FALLBACK_ENTRIES.items() if k in url), None)

    if not curl_ok:
        verdict, note = "unreachable", f"内容不可达 (curl {st} {err or ''})"
    elif bstatus == "ok":
        verdict, note = "browser_ok", "浏览器可访问"
    elif bstatus == "blocked":
        verdict, note = "browser_blocked", "浏览器握手被服务器拒绝（curl 200，内容存在）"
    elif bstatus == "no_chrome":
        verdict, note = "curl_only", "仅 curl 验证（本机无 Chrome，浏览器兼容性未验证）"
    else:
        verdict, note = "browser_fail", f"浏览器异常（{';'.join(berrors) or '无输出'}，curl 200）"

    # 名单内平台：即使本次 browser_ok，也提示"建议附备选入口"（TLS 行为不稳定）
    unstable_hint = None
    if fallback and verdict in ("browser_ok", "curl_only"):
        unstable_hint = f"该平台 TLS 行为不稳定（曾现浏览器打不开），建议附备选入口：{fallback}"

    return {
        "url": url, "curl_status": st, "content_type": ct,
        "verdict": verdict, "note": note,
        "browser_errors": berrors, "fallback_entry": fallback,
        "unstable_hint": unstable_hint,
    }


def render_line(r):
    if r["verdict"] == "browser_ok":
        line = f"✅ {r['note']}  curl {r['curl_status']}  {r['url']}"
        if r.get("unstable_hint"):
            line += f"\n   {r['unstable_hint']}"
        return line
    if r["verdict"] == "browser_blocked":
        return (f"⚠️ {r['note']}  curl {r['curl_status']}  {r['url']}\n"
                f"   建议附备选入口：{r['fallback_entry'] or '平台首页'}")
    if r["verdict"] == "curl_only":
        return f"🟡 {r['note']}  curl {r['curl_status']}  {r['url']}"
    return f"❌ {r['note']}  {r['url']}"


def main():
    urls = sys.argv[1:]
    if not urls:
        urls = [l.strip() for l in sys.stdin if l.strip()]
    results = []
    for u in urls:
        r = classify(u)
        results.append(r)
        print(render_line(r))
    if results:
        print("\n--- json ---")
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
