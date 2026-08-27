#!/usr/bin/env python3
"""Fetch hbzdtdl.toobiao.com (河北省招标投标公共服务平台) via http."""
import requests, sys, io, re, html
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

urls = [
    ("http://hbzdtdl.toobiao.com/", "hbzdtdl_home.html"),
]
for url, fname in urls:
    try:
        r = _sess.get(url, timeout=20, verify=False, allow_redirects=True)
        body = r.text
        open(fname, "w", encoding="utf-8").write(body)
        txt = clean(re.sub(r"<[^>]+>", " ", body))
        print(f"{url} -> {r.status_code} len={len(body)} final={r.url}")
        # extract entries with dates
        for m in re.finditer(r"(20\d\d-\d\d-\d\d)", txt):
            pass
        # print lines containing 招标/公告/储能/风电 with dates
        for m in re.finditer(r"[^\s]{0,60}(储能|风电|火电|招标公告|采购公告|施工总承包)[^\s]{0,80}", txt):
            s = m.group(0)
            if "登录" in s or "注册" in s:
                continue
            print("  ·", s[:150])
    except Exception as e:
        print(f"{url} -> FAIL ({type(e).__name__}: {e})")
