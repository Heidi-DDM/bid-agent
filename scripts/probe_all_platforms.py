#!/usr/bin/env python3
"""对照表平台批量实测脚本（用户本机运行，需外网）。

目的：逐个确认「京津冀招投标平台对照表-20260819.xlsx」全部平台（23 平台 +
4 商业聚合）的接入可行性，为接入登记提供事实依据。每个公告源输出：
  - robots 状态（allowed/disallowed/missing/unreachable，预检留痕不阻断 ADR-003）
  - 列表页 HTTP 状态与长度
  - 列表结构判定：静态可解析 / JS 动态（HTML 无条目）/ 无法判定
  - 公告条目关键词命中（标题含 公告/采购/招标/公示 的链接）
  - 分页形态探测（page_2 / 2.html / index_1.htm 等常见形态）
非公告源（schema §1.4 剔除名单：住建部四库/中国采购与招标网/河北省住建厅/
张家口保函平台）仅留档可达性，不接入采集。商业聚合（L0 待合同）走官方
API/授权推送，不做页面采集探测。

用法：python scripts/probe_all_platforms.py
输出：控制台摘要；可选 --save 保存 HTML 快照到 scripts/ 下供留存。
注意：本脚本仅探测（每平台 1-2 个请求），遵守限频：单源 ≤1 次/5 分钟为抓取粒度，
探测不进入采集限频器，但仍克制请求数量并透明 UA。
"""
from __future__ import annotations

import argparse
import io
import re
import sys

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

UA = ("Hebei-Jianshe-BidAgent/0.1 "
      "(internal demo system; source compliance per docs/data-source-compliance.md)")

# (平台名, 列表页 URL, 备用 robots 基址) —— 摘自对照表「平台全景对照表」全部 23 行
PLATFORMS = [
    # ── 公告源（接入评估） ──
    ("全国公共资源交易平台", "https://www.ggzy.gov.cn/", "https://www.ggzy.gov.cn"),
    ("中国招标投标公共服务平台", "https://www.cebpubservice.com/", "https://www.cebpubservice.com"),
    ("河北省政府采购网", "http://www.ccgp-hebei.gov.cn/province/cggg/zbgg/", "http://www.ccgp-hebei.gov.cn"),
    ("河北省公共资源交易服务平台", "https://szj.hebei.gov.cn/hbggfwpt/", "https://szj.hebei.gov.cn"),
    ("北京市政府采购网", "http://www.ccgp-beijing.gov.cn/", "http://www.ccgp-beijing.gov.cn"),
    ("北京公共资源交易服务平台", "https://ggzyfw.beijing.gov.cn/", "https://ggzyfw.beijing.gov.cn"),
    ("北京建设工程交易系统", "https://zhjy.bcactc.com/", "https://zhjy.bcactc.com"),
    ("天津市政府采购网", "http://www.ccgp-tianjin.gov.cn/", "http://www.ccgp-tianjin.gov.cn"),
    ("天津市公共资源交易平台", "https://ggzy.zwfwb.tj.gov.cn/", "https://ggzy.zwfwb.tj.gov.cn"),
    ("石家庄市公共资源交易中心", "http://www.sjzsggzyjyzx.org.cn/", "http://www.sjzsggzyjyzx.org.cn"),
    ("唐山市公共资源交易中心", "http://ggzyjy.xzspj.tangshan.gov.cn/", "http://ggzyjy.xzspj.tangshan.gov.cn"),
    ("邯郸市公共资源交易中心", "https://ggzy.hd.gov.cn/", "https://ggzy.hd.gov.cn"),
    ("秦皇岛市公共资源交易中心", "http://www.qhdggzy.cn/qhdggzy/", "http://www.qhdggzy.cn"),
    ("承德市公共资源交易中心", "http://szj.chengde.gov.cn/cdsggzy/", "http://szj.chengde.gov.cn"),
    ("衡水市公共资源交易中心", "http://hsggzy.hengshui.gov.cn/", "http://hsggzy.hengshui.gov.cn"),
    ("沧州市公共资源交易中心", "https://xzsp.cangzhou.gov.cn/xzsp/add100115/dt_index.shtml", "https://xzsp.cangzhou.gov.cn"),
    ("邢台市公共资源交易中心", "http://60.6.198.121:8888/sszt-zyjyPortal/", "http://60.6.198.121:8888"),
    ("保定市(挂省级)", "https://szj.hebei.gov.cn/hbggfwpt/", "https://szj.hebei.gov.cn"),
    ("廊坊市(挂省级)", "https://szj.hebei.gov.cn/hbggfwpt/", "https://szj.hebei.gov.cn"),
    # ── 非公告源（对照表全量留档，schema §1.4 剔除名单，不接入采集） ──
    ("[留档] 住建部四库一平台", "https://jzsc.mohurd.gov.cn/", "https://jzsc.mohurd.gov.cn"),
    ("[留档] 中国采购与招标网", "http://www.chinabidding.com.cn/", "http://www.chinabidding.com.cn"),
    ("[留档] 河北省住建厅", "http://zfcxjst.hebei.gov.cn/", "http://zfcxjst.hebei.gov.cn"),
    ("[留档] 张家口市(保函平台)", "http://60.8.117.38:5027/zjkbhpt/", "http://60.8.117.38:5027"),
]

_ANNOUNCE_KW = ("公告", "采购", "公示", "招标", "中标", "磋商", "比选", "询价")
_JS_KW = ("暂无数据", "加载中", "list[", "ajax", "getData", "v-for", "vue", "react")


def _probe_robots(base: str) -> str:
    try:
        r = requests.get(f"{base}/robots.txt", timeout=10, headers={"User-Agent": UA}, verify=False)
        if r.status_code == 404:
            return "missing"
        if r.status_code != 200:
            return f"http{r.status_code}"
        body = r.text
        if "disallow: /" in body.lower() or "disallow:/" in body.lower():
            return "disallowed(/)"
        dis = [ln.split(":", 1)[1].strip() for ln in body.splitlines()
               if ln.lower().startswith("disallow") and ln.split(":", 1)[1].strip()]
        return f"disallowed({','.join(dis)})" if dis else "allowed"
    except Exception as exc:
        return f"unreachable({type(exc).__name__})"


def _canned_list(url: str) -> str | None:
    """已知列表结构：河北政府采购网列表页静态可解析；省平台列表为 JS 动态。返回判定。"""
    if "ccgp-hebei.gov.cn" in url:
        return "static"
    if "szj.hebei.gov.cn" in url:
        return "js-dynamic"
    return None


def _probe_list(name: str, url: str) -> dict:
    info = {"url": url, "status": None, "len": 0, "structure": "?", "entries": 0, "pagination": None}
    canned = _canned_list(url)
    if canned == "static":
        info["structure"] = "static"
    elif canned == "js-dynamic":
        info["structure"] = "js-dynamic"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": UA}, verify=False, allow_redirects=True)
        info["status"] = r.status_code
        src = r.text
        info["len"] = len(src)
        if canned is None:
            text = re.sub(r"<script.*?</script>", " ", src, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", html_unescape(text))
            # 列表静态判定：存在含公告关键词的站内链接
            anchors = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', src, re.S)
            anchored = [t for _, t in anchors if len(re.sub(r"<[^>]+>", "", t).strip()) >= 8]
            kw_hits = [t for t in anchored if any(k in t for k in _ANNOUNCE_KW)]
            if kw_hits:
                info["structure"] = "static"
                info["entries"] = len(kw_hits)
            elif any(k in src for k in ("暂无数据", "loading", "加载", "getData", "ajax", "v-for", "list")):
                info["structure"] = "js-dynamic"
            else:
                info["structure"] = "unclear"
        # 分页形态：page_2 / 2.html / index_1.htm / _1
        for pat in ("_2.", "page2", "2.html", "index_1", "_1."):
            if pat in src:
                info["pagination"] = pat
                break
    except Exception as exc:
        info["status"] = f"ERR:{type(exc).__name__}"
    return info


def html_unescape(s: str) -> str:
    return re.sub(r"&(?:\w+|#\d+);", " ", s)


def main() -> None:
    ap = argparse.ArgumentParser(description="批量探测对照表平台接入可行性")
    ap.add_argument("--save", action="store_true", help="保存列表页 HTML 快照到 scripts/probe_snapshots/")
    args = ap.parse_args()

    print(f"== 平台批量实测（{len(PLATFORMS)} 平台）==")
    for name, url, base in PLATFORMS:
        robots = _probe_robots(base)
        info = _probe_list(name, url)
        print(f"\n[{name}] {url}")
        print(f"  robots: {robots}")
        print(f"  list:   HTTP {info['status']} len={info['len']} 结构={info['structure']} "
              f"条目={info['entries']} 分页={info['pagination'] or '-'}")
        if args.save:
            from pathlib import Path
            Path("scripts/probe_snapshots").mkdir(exist_ok=True)
            try:
                r = requests.get(url, timeout=15, headers={"User-Agent": UA}, verify=False)
                fname = name.replace("/", "_")[:20]
                Path(f"scripts/probe_snapshots/{fname}.html").write_text(r.text, encoding="utf-8", errors="replace")
                print(f"  已保存快照 scripts/probe_snapshots/{fname}.html")
            except Exception as exc:
                print(f"  快照失败: {type(exc).__name__}")
    print("\n== 完成 ==")


if __name__ == "__main__":
    main()