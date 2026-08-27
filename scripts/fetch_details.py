#!/usr/bin/env python3
"""Fetch dlzb detail page (某建投任丘热电 CEMS 招标) + hbzdtdl.toobiao.com list."""
import requests, sys, io, re, html
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

targets = [
    ("https://www.dlzb.com/d-zb-68938356.html", "dlzb_renqiu_cems.html"),
    ("https://www.dlzb.com/d-zb-68938358.html", "dlzb_renqiu_huanbao.html"),
    ("https://www.dlzb.com/d-zb-68857919.html", "dlzb_xibaipo_huashui.html"),
    ("https://hbzdtdl.toobiao.com/", "hbzdtdl_home.html"),
]
for url, fname in targets:
    try:
        r = _sess.get(url, timeout=20, verify=False, allow_redirects=True)
        body = r.text
        open(fname, "w", encoding="utf-8").write(body)
        txt = clean(re.sub(r"<[^>]+>", " ", body))
        print(f"{url} -> {r.status_code} len={len(body)} final={r.url}")
        # show snippet around 截止/报名/时间
        for kw in ["截止", "报名", "开标", "获取招标文件", "资质"]:
            m = re.search(kw + r".{0,120}", txt)
            if m:
                print(f"    [{kw}] {m.group(0)[:150]}")
    except Exception as e:
        print(f"{url} -> FAIL ({type(e).__name__}: {e})")
