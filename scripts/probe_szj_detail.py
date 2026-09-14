#!/usr/bin/env python3
"""省公共资源交易服务平台（szj.hebei.gov.cn）详情直抓专项探测（用户本机执行，需外网）。

背景：2026-09-04 探测确认 szj.hbjyzx 公告**列表页为 JS 动态加载**（HTML 无条目，
当前抓取器无 JS 渲染）；**详情页为静态可解析**（净化文本可用）。本脚本回答
「详情直抓怎么做」的三个未知项：
  1. robots.txt 是否可达/是否有 Disallow（预检留痕不阻断，ADR-003）
  2. 列表数据来源：直接抓列表页 HTML 是否出条目；若否，页面里出现的
     XHR 接口/数据关键字（fetch/ajax/api），为后续浏览器 F12 人工确认提供锚点
  3. 详情页结构：对给定详情 URL 验证「净化文本」产出（html_to_text 口径），
     证明现有 import_detail 链路可直接复用

用法：
  python scripts/probe_szj_detail.py                      # 只探测列表层
  python scripts/probe_szj_detail.py --detail "<详情URL>"  # 附验证单条详情净化
注意：
  - 仅探测（≤5 个请求），不进采集限频器，但仍克制并透明 UA；
  - 不执行 JS、不绕过验证码/登录态/反爬（schema §5.6 R1 不变）；
  - 沙盒无外网时此脚本在用户本机运行，结果回贴登记。
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
import ssl
import warnings

warnings.filterwarnings("ignore")

sys.stdout = sys.stdout

UA = ("Hebei-Jianshe-BidAgent/0.1 "
      "(internal demo system; source compliance per docs/data-source-compliance.md)")
BASE = "https://szj.hebei.gov.cn"

# 探测候选路径（门户/公告入口常见形态；命中即打印，不跟抓详情）
CANDIDATE_PATHS = [
    "/robots.txt",
    "/hbggfwpt/",
    "/hbggfwpt/jyxx/",
    "/hbjyzx/",
    "/hbjyzx/jyxx/",
    "/jyxx/",
    "/portal/jyxx/",
]

_ANNOUNCE_KW = ("公告", "采购", "公示", "招标", "中标", "磋商", "比选", "询价")
_JS_HINTS = ("暂无数据", "加载中", "ajax", "getData", "v-for", "vue", "react",
             "fetch(", "axios", "/api/")


def _ctx() -> ssl.SSLContext:
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),  # 对照表纠偏⑦：本机代理失效，绕过
        urllib.request.HTTPSHandler(context=_ctx()),
    )


def _get(opener, url: str, timeout: int = 12) -> tuple[int | str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(1_500_000)
            return resp.status, data.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"ERR:{type(exc).__name__}", str(exc)


def _report_static_or_dynamic(src: str) -> str:
    """列表页结构判定：静态（有公告链接）/ 动态（无条目、含 JS 数据提示）。"""
    anchors = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', src, re.S)
    hits = []
    for href, text in anchors:
        t = re.sub(r"<[^>]+>", "", text).strip()
        if len(t) >= 8 and any(k in t for k in _ANNOUNCE_KW):
            hits.append((t[:40], href))
    if hits:
        return f"static（示例 {len(hits)} 条：{hits[:3]}）"
    js = [k for k in _JS_HINTS if k in src]
    return f"js-dynamic（HTML 无公告条目；JS/数据接口提示: {js[:6] or '未见'}）"


def main() -> None:
    ap = argparse.ArgumentParser(description="szj 详情直抓专项探测")
    ap.add_argument("--detail", help="单条公告详情 URL，验证净化文本产出")
    args = ap.parse_args()

    opener = _opener()
    print(f"== szj.hebei.gov.cn 详情直抓探测 ==\n")
    for path in CANDIDATE_PATHS:
        st, txt = _get(opener, BASE + path)
        if isinstance(st, str):
            print(f"[{path}] {st}")
            continue
        print(f"[{path}] HTTP {st} len={len(txt)}")
        if path == "/robots.txt":
            dis = [ln.strip() for ln in txt.splitlines()
                   if ln.lower().startswith("disallow")]
            print(f"  robots Disallow: {dis or '无（默认允许）'}")
            continue
        if st == 200 and len(txt) > 500:
            print(f"  结构判定: {_report_static_or_dynamic(txt)}")
            for kw in ("hbjyzx", "hbggfwpt", "/jyxx"):
                if kw in txt:
                    print(f"  页面含路径关键字: {kw}")
        else:
            print(f"  非 200 或空页：跳过")

    if args.detail:
        print(f"\n== 详情净化验证: {args.detail} ==")
        st, txt = _get(opener, args.detail)
        print(f"  HTTP {st} len={len(txt)}")
        if isinstance(st, int) and st == 200:
            # 与 runtime/collecting/parsers.html_to_text 口径一致的轻量净化
            body = re.sub(r"<script\b.*?</script>", " ", txt, flags=re.S | re.I)
            body = re.sub(r"<style\b.*?</style>", " ", body, flags=re.S | re.I)
            body = re.sub(r"</?(?:p|div|tr|li|h[1-6]|br|table|ul|ol)[^>]*>", "\n", body)
            body = re.sub(r"<[^>]+>", "", body)
            body = re.sub(r"\s+", " ", body).strip()
            print(f"  净化后可见文本: {len(body)} 字（前 120 字: {body[:120]}）")
            print(f"  → import_detail 链路（robots 预检 + 限频 + 净化固化）可复用" if len(body) > 200
                  else "  → 净化为空/过短，疑似登录墙或非正文页，需人工确认")
    print("\n== 完成 ==")


if __name__ == "__main__":
    main()