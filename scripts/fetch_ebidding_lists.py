#!/usr/bin/env python3
"""Fetch ebidding.hebtig.com trade list pages (工程类/服务类/非招标专区) and parse entries with dates."""
import requests, re, html, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

urls = [
    ("https://ebidding.hebtig.com/jyxx/001001/001001001/trade.html", "ebidding_gongcheng.html"),   # 招标专区-工程类
    ("https://ebidding.hebtig.com/jyxx/001001/001001003/trade.html", "ebidding_fuwu.html"),       # 招标专区-服务类
    ("https://ebidding.hebtig.com/jyxx/001001/001001004/trade.html", "ebidding_fgzc.html"),       # 非招标专区
]
for url, fname in urls:
    try:
        r = _sess.get(url, timeout=20, verify=False, allow_redirects=True)
        src = r.text
        open(fname, "w", encoding="utf-8").write(src)
        print(f"\n===== {fname} ({url}) status={r.status_code} len={len(src)} =====")
        # entries: anchor + date
        for m in re.finditer(r'<a[^>]+href="(/jyxx/[^"]+)"[^>]*title="([^"]+)"[^>]*>(.*?)</a>', src, re.S):
            href, title, inner = m.group(1), m.group(2), m.group(3)
            if not title:
                title = clean(inner)
            if len(title) < 8:
                continue
            print(f"  · {title}\n    https://ebidding.hebtig.com{href}")
        # fallback: any anchor with 公告/采购 in text
        for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', src, re.S):
            href, txt = m.group(1), clean(m.group(2))
            if len(txt) >= 8 and any(k in txt for k in ["公告", "采购", "公示", "招标", "中标"]) and "javascript" not in href:
                print(f"  · {txt}\n    https://ebidding.hebtig.com{href}")
    except Exception as e:
        print(f"{fname} -> FAIL ({type(e).__name__}: {e})")
