#!/usr/bin/env python3
"""G4 前置：招标文件入口链接可达性复检（通道① 稳定性保障）
- 输入：入口 URL 列表（JSON 或每行一个 URL）
- 输出：每条 URL 的 HTTP 状态 + 内容类型 + 是否可访问
- 重试：3 次指数退避（1s/2s/4s）
- 合规：仅 HEAD/GET 探测可达性，不下载文件内容、不登录、不过验证码
用法: python3 check_tender_doc_links.py [url1 url2 ...] 或 管道输入
"""
import sys, time, json, ssl, urllib.request, urllib.error

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

def check(url, retries=3):
    """返回 (status, content_type, error)"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0",
                "Accept": "text/html,application/pdf,application/msword,*/*",
            })
            with opener.open(req, timeout=15) as r:
                return r.status, r.headers.get("Content-Type", ""), None
        except urllib.error.HTTPError as e:
            # 403/405 常见于平台拒绝 HEAD——降级 GET 探测
            if e.code in (403, 405) and attempt == retries - 1:
                try:
                    req = urllib.request.Request(url, method="GET", headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0",
                        "Range": "bytes=0-1023",  # 只取头 1KB，不下载全文
                    })
                    with opener.open(req, timeout=15) as r:
                        ct = r.headers.get("Content-Type", "")
                        return r.status, ct, None
                except Exception as e2:
                    return e.code, "", f"HEAD {e.code} / GET fail: {e2}"
            return e.code, "", None
        except (urllib.error.URLError, OSError, Exception) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
    return 0, "", f"network error after {retries} attempts: {last_err}"

def main():
    urls = sys.argv[1:]
    if not urls:
        # 从 stdin 读
        urls = [l.strip() for l in sys.stdin if l.strip()]
    results = []
    for url in urls:
        st, ct, err = check(url)
        ok = (200 <= st < 400) if st else False
        results.append({"url": url, "status": st, "content_type": ct,
                        "reachable": ok, "error": err})
        print(f"{'✅' if ok else '❌'} {st} {ct[:40]:40s} {url}")
    # 汇总 JSON 存档（供 G4 记录）
    if results:
        print("\n--- json ---")
        print(json.dumps(results, ensure_ascii=False))

if __name__ == "__main__":
    main()
