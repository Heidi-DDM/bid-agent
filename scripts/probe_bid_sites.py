#!/usr/bin/env python3
"""Probe reachability of bid platform domains via their DNS-resolved IPs."""
import socket, ssl, sys

targets = [
    ("ebidding.hebtig.com", 443, "https"),
    ("www.jtsww.com", 80, "http"),
    ("hdxy.toobiao.com", 80, "http"),
    ("ggzyfw.beijing.gov.cn", 443, "https"),
    ("www.ggzy.gov.cn", 443, "https"),
]

for host, port, scheme in targets:
    try:
        ip = socket.gethostbyname(host)
        s = socket.create_connection((ip, port), timeout=6)
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=host)
        s.sendall(b"GET / HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
        data = s.recv(300)
        print(f"{host}: CONNECTED -> {data[:120]!r}")
        s.close()
    except Exception as e:
        print(f"{host}: FAIL ({type(e).__name__}: {e})")
