#!/usr/bin/env python3
"""Probe ports for remaining domains (http/80 vs https/443)."""
import socket, ssl, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

targets = [
    ("www.dlzb.com", 80, "http"),
    ("www.dlzb.com", 443, "https"),
    ("hbzdtdl.toobiao.com", 80, "http"),
    ("hbzdtdl.toobiao.com", 443, "https"),
    ("hdxy.toobiao.com", 80, "http"),
    ("hdxy.toobiao.com", 443, "https"),
]
for host, port, scheme in targets:
    try:
        ip = socket.gethostbyname(host)
        s = socket.create_connection((ip, port), timeout=6)
        if scheme == "https":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                ctx.maximum_version = ssl.TLSVersion.TLSv1_2
            except Exception:
                pass
            s = ctx.wrap_socket(s, server_hostname=host)
        s.sendall(b"GET / HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
        data = s.recv(200)
        print(f"{host}:{port} {scheme} -> {data[:120]!r}")
        s.close()
    except Exception as e:
        print(f"{host}:{port} {scheme} -> FAIL ({type(e).__name__}: {str(e)[:100]})")
