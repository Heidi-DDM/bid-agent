#!/usr/bin/env python3
"""Fetch remaining ebidding detail pages: 储能设备采购 + 京秦设计审查 + 康保监理搜索."""
import requests, re, html, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

pages = [
    ("围场德御297MW/1188MWh磷酸铁锂储能系统设备采购",
     "https://ebidding.hebtig.com/jyxx/001001/001001002/001001002001/20260812/64e780af-297b-4915-ba97-5cc87b6547f4.html"),
    ("晟德承德县300MW/1200MWh磷酸铁锂储能系统设备采购",
     "https://ebidding.hebtig.com/jyxx/001001/001001002/001001002001/20260811/5f594ecb-5463-44cd-b523-18debc1082ab.html"),
    ("京秦高速遵秦段设计审查采购(今日)",
     "https://ebidding.hebtig.com/jyxx/001002/001002003/001002003001/20260824/00768342-2d37-4848-9799-eb9e0d67518f.html"),
]
for title, url in pages:
    try:
        r = _sess.get(url, timeout=25, verify=False, allow_redirects=True)
        src = r.text
        txt = clean(re.sub(r"<script.*?</script>", " ", src, flags=re.S))
        txt = clean(re.sub(r"<[^>]+>", " ", txt))
        print(f"\n{'='*16} {title} ({r.status_code}) {'='*16}")
        for kw in ["项目名称", "招标编号", "采购人", "招标人", "代理", "获取", "报名", "截止", "开标", "递交", "资质", "地点", "规模", "交货"]:
            for m in re.finditer(kw + r"[^。；\n]{0,120}", txt):
                s = m.group(0).strip()
                if s:
                    print(f"  [{kw}] {s[:150]}")
        dates = sorted(set(re.findall(r"20\d\d-\d\d-\d\d", txt)))
        print(f"  [DATES] {dates}")
    except Exception as e:
        print(f"{title} -> FAIL ({type(e).__name__}: {e})")
