#!/usr/bin/env python3
"""Probe jtsww.com (建投商务网) common list/API paths."""
import requests, ssl, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

paths = [
    "/", "/index.html", "/notice/", "/notice", "/bidNotice/", "/zbgg/",
    "/announcement/", "/search", "/api/notice/list", "/api/zbgg/list",
    "/notice/list", "/jyxx/", "/zbxx/", "/tender/", "/info/",
]
for p in paths:
    url = "https://www.jtsww.com" + p
    try:
        r = _sess.get(url, timeout=12, verify=False, allow_redirects=True)
        body = r.text
        has_list = any(k in body for k in ["招标公告", "采购公告", "公示", "公告列表", "zbgg"])
        print(f"{url} -> {r.status_code} len={len(body)} final={r.url} has_list={has_list}")
    except Exception as e:
        print(f"{url} -> FAIL ({type(e).__name__})")
