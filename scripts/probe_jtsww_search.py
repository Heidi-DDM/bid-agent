#!/usr/bin/env python3
"""Try jtsww.com public search page for 任丘热电 announcements."""
import requests, re, html, sys, io, urllib.parse
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

for kw in ["任丘热电", "烟气CEMS", "西柏坡"]:
    url = "https://www.jtsww.com/search?keyword=" + urllib.parse.quote(kw)
    try:
        r = _sess.get(url, timeout=20, verify=False, allow_redirects=True)
        src = r.text
        txt = clean(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", src, flags=re.S)))
        print(f"\n===== {url} status={r.status_code} len={len(src)} =====")
        # find title/notice mentions
        for m in re.finditer(r"(任丘热电|西柏坡|烟气CEMS|招标公告|采购公告)[^|]{0,120}", txt):
            s = m.group(0).strip()
            if len(s) > 10:
                print("  ·", s[:160])
        # any date near 公告
        for m in re.finditer(r"20\d\d-\d\d-\d\d", txt):
            pass
        dates = re.findall(r"20\d\d-\d\d-\d\d", txt)
        if dates:
            print("  [dates]", sorted(set(dates))[-15:])
    except Exception as e:
        print(f"{url} -> FAIL ({type(e).__name__}: {e})")
