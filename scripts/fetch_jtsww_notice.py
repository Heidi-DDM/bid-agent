#!/usr/bin/env python3
"""Fetch jtsww.com /notice/ and extract announcement entries (title/link/date)."""
import requests, re, html, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False
url = "https://www.jtsww.com/notice/"
r = _sess.get(url, timeout=20, verify=False, allow_redirects=True)
src = r.text
open("jtsww_notice.html", "w", encoding="utf-8").write(src)
print(f"status={r.status_code} len={len(src)} final={r.url}")

# 1) anchors
def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

items = []
for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', src, re.S):
    href, txt = m.group(1), clean(m.group(2))
    if not txt or len(txt) < 8:
        continue
    if any(k in txt for k in ["公告", "公示", "采购", "招标", "中标", "询价"]):
        items.append((txt, href))

print(f"--- anchors ({len(items)}) ---")
seen = set()
for txt, href in items:
    if (txt, href) in seen: continue
    seen.add((txt, href))
    print(f"  · {txt}\n    {href}")

# 2) dates in page
dates = re.findall(r"20\d\d-\d\d-\d\d", src)
print(f"--- dates found: {sorted(set(dates))[-30:]} ---")
