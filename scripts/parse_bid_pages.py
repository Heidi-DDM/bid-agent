#!/usr/bin/env python3
"""Parse fetched platform HTML pages: extract announcement entries (title/url/date)."""
import re, html, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

for path in ["ebidding_home.html", "ebidding_huowu.html", "jtsww_home.html", "toobiao_hebeijiantou.html"]:
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except FileNotFoundError:
        print(f"--- {path}: MISSING"); continue
    print(f"\n===== {path} (len={len(src)}) =====")
    # announcement links: <a href="...">title</a> where title contains 公告/公示/采购/招标
    items = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', src, re.S):
        href, txt = m.group(1), clean(m.group(2))
        if not txt or len(txt) < 8:
            continue
        if any(k in txt for k in ["公告", "公示", "采购", "招标", "中标", "询价", "谈判", "竞价"]):
            items.append((txt, href))
    seen = set()
    for txt, href in items:
        key = (txt, href)
        if key in seen: continue
        seen.add(key)
        print(f"  · {txt}\n    {href}")
