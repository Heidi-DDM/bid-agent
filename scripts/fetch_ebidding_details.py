#!/usr/bin/env python3
"""Fetch ebidding.hebtig.com detail pages for energy announcements & extract deadlines."""
import requests, re, html, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_sess = requests.Session()
_sess.trust_env = False

def clean(s):
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

pages = [
    ("围场德御电网侧独立储能综合示范项目EPC总承包招标公告",
     "https://ebidding.hebtig.com/jyxx/001001/001001001/001001001001/20260812/a1cfb9c0-f6d3-4bbb-b686-3d9efb467bdd.html"),
    ("河北交投晟德承德县300MW/1200MWh储能电站项目EPC总承包招标公告",
     "https://ebidding.hebtig.com/jyxx/001001/001001001/001001001001/20260811/ab00e772-bba2-4c3b-9539-f3ecfa7c283e.html"),
    ("河北交投康保县400MW构网型独立储能电站项目EPC总承包招标公告",
     "https://ebidding.hebtig.com/jyxx/001001/001001001/001001001001/20260810/1ddbb063-bc5f-4e5e-9964-8a0c736e588b.html"),
]
for title, url in pages:
    try:
        r = _sess.get(url, timeout=25, verify=False, allow_redirects=True)
        src = r.text
        txt = clean(re.sub(r"<script.*?</script>", " ", src, flags=re.S))
        txt = clean(re.sub(r"<[^>]+>", " ", txt))
        open("ebidding_detail_tmp.html", "w", encoding="utf-8").write(src)
        print(f"\n{'='*20} {title} ({r.status_code}) {'='*20}")
        # extract key fields
        for kw in ["项目名称", "招标编号", "招标人", "代理机构", "招标文件的获取", "获取时间", "报名", "投标截止", "开标", "投标文件递交", "最高限价", "预算", "资质", "资格要求", "建设地点", "规模", "工期", "保证金"]:
            for m in re.finditer(kw + r"[^。；\n]{0,150}", txt):
                s = m.group(0).strip()
                if s:
                    print(f"  [{kw}] {s[:170]}")
        # dates
        dates = sorted(set(re.findall(r"20\d\d-\d\d-\d\d[ T]?\d{0,2}:?\d{0,2}", txt)))
        print(f"  [DATES] {dates}")
    except Exception as e:
        print(f"{title} -> FAIL ({type(e).__name__}: {e})")
