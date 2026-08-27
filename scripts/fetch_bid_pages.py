#!/usr/bin/env python3
"""Fetch bid platform list pages (sandbox DNS resolves to simulated hosts).
trust_env=False per project note (local proxy 127.0.0.1:7890 dead)."""
import ssl, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    print("NO_REQUESTS")
    sys.exit(2)

# TLS 1.2-only context (some platforms reject TLS 1.3 handshake)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
try:
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
except Exception:
    pass

targets = [
    ("https://ebidding.hebtig.com/", "ebidding_home.html"),
    ("https://ebidding.hebtig.com/jyxx/001001/001001002/trade.html", "ebidding_huowu.html"),
    ("http://www.jtsww.com/", "jtsww_home.html"),
    ("http://hdxy.toobiao.com/zhaobiao", "toobiao_hebeijiantou.html"),
    ("https://ggzyfw.beijing.gov.cn/xtggxazbgg/", "ggzyfw_xiongan.html"),
]

_sess = requests.Session()
_sess.trust_env = False

for url, fname in targets:
    try:
        r = _sess.get(url, timeout=20, verify=False, allow_redirects=True)
        body = r.text
        with open(fname, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"{url} -> {fname} [{r.status_code}] len={len(body)} final={r.url}")
    except Exception as e:
        print(f"{url} -> FAIL ({type(e).__name__}: {e})")
