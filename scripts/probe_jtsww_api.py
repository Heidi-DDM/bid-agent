#!/usr/bin/env python3
"""Probe jtsww.com bidding-portalsite API endpoints for announcement lists."""
import requests, sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

paths = [
    "/bidding-portalsite/notice/page",
    "/bidding-portalsite/notice/list",
    "/bidding-portalsite/portal/notice/list",
    "/bidding-portalsite/portal/notice/page",
    "/bidding-portalsite/api/notice/page",
    "/bidding-portalsite/zbgg/list",
    "/bidding-portalsite/portal/zbgg/page",
    "/bidding-portalsite/notice/queryList",
    "/bidding-portalsite/notice/queryNoticeList",
    "/bidding-portalsite/portal/index/noticeList",
    "/bidding-portalsite/portal/getNoticeList",
    "/bidding-portalsite/notice/getNoticeList",
]
for p in paths:
    url = "https://www.jtsww.com" + p
    try:
        r = _sess.get(url, timeout=10, verify=False, allow_redirects=True)
        body = r.text[:200].replace("\n", " ")
        print(f"{p} -> {r.status_code} len={len(r.text)} ct={r.headers.get('Content-Type','')[:40]} body={body[:150]}")
    except Exception as e:
        print(f"{p} -> FAIL ({type(e).__name__})")
