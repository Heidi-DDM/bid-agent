#!/usr/bin/env python3
"""PoC: 调用围场平台公开全文检索接口，定位围场德御 EPC 公告的招标文件下载入口
合规：使用平台前端自身调用的公开检索接口（等同页面搜索功能），不绕过技术保护
"""
import json, ssl, urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

BASE = "http://weichangjyzx.cn"

def search(wd, cnum="", pn=0, rn=15):
    param = {
        "token": "",
        "pn": pn, "rn": rn,
        "sdt": "", "edt": "",
        "wd": wd, "inc_wd": "", "exc_wd": "",
        "fields": "", "cnum": cnum,
        "sort": '{"webdate":"0","id":"0"}',
        "ssort": "", "cl": 200, "terminal": "",
        "condition": None, "time": None,
        "highlights": "", "statistics": None,
        "unionCondition": None, "accuracy": "",
        "noParticiple": "1", "searchRange": None, "noWd": True,
    }
    req = urllib.request.Request(
        BASE + "/inteligentsearch/rest/esinteligentsearch/getFullTextDataNew",
        data=json.dumps(param).encode(), method="POST",
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"},
    )
    with opener.open(req, timeout=25) as r:
        return json.loads(r.read())

# 1) 关键词搜索 EPC
print("=== 搜索: 围场德御 储能 EPC ===")
try:
    res = search("围场德御 储能 EPC")
    if res.get("code") == 200:
        data = json.loads(res["content"])
        total = (data.get("result") or {}).get("totalcount", 0)
        print("total:", total)
        for rec in (data.get("result") or {}).get("records", []):
            print("-", rec.get("title", "")[:60], "|", rec.get("linkurl", ""), "|", (rec.get("webdate") or "")[:10])
    else:
        print("code:", res.get("code"), "msg:", res.get("msg"))
except Exception as e:
    print("ERR:", e)
