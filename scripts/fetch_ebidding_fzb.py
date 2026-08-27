#!/usr/bin/env python3
"""Fetch ebidding 非招标专区 list pages and print entries."""
import requests, re, html, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

urls = [
    ("https://ebidding.hebtig.com/jyxx/001002/001002001/trade.html", "ebidding_fzb_gc.html"),
    ("https://ebidding.hebtig.com/jyxx/001002/001002002/trade.html", "ebidding_fzb_hw.html"),
    ("https://ebidding.hebtig.com/jyxx/001002/001002003/trade.html", "ebidding_fzb_fw.html"),
]
for url, fname in urls:
    try:
        r = _sess.get(url, timeout=20, verify=False, allow_redirects=True)
        src = r.text
        open(fname, "w", encoding="utf-8").write(src)
        print(f"\n===== {fname} ({url}) status={r.status_code} len={len(src)} =====")
        for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*title="([^"]+)"', src):
            href, title = m.group(1), m.group(2)
            if len(title) >= 8:
                print(f"  · {title}\n    https://ebidding.hebtig.com{href}")
    except Exception as e:
        print(f"{fname} -> FAIL ({type(e).__name__}: {e})")
