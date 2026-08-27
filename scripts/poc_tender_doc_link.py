#!/usr/bin/env python3
"""G3.5 招标文件链接发现 PoC
探测公告页 → 提取「招标文件获取」指引 → 分类链接类型（direct/platform/none）→ 验证可访问性
合规：不做登录、不破解验证码（R1）；仅探测公开可达信息（R2）
"""
import re, sys, ssl, json, urllib.request, urllib.error
from urllib.parse import urljoin

# 走系统代理 + 容忍 MITM 证书（本机 Clash TUN 模式实测）
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36')]

ANNOUNCEMENTS = [
    {
        "name": "康保400MW储能EPC",
        "url": "https://ebidding.hebtig.com/jyxx/001001/001001001/001001001001/20260810/1ddbb063-bc5f-4e5e-9964-8a0c736e588b.html",
    },
    {
        "name": "围场德御300MW储能EPC",
        "url": "https://ebidding.hebtig.com/jyxx/001001/001001001/001001001001/20260812/a1cfb9c0-f6d3-4bbb-b686-3d9efb467bdd.html",
    },
]

FILE_EXT = re.compile(r'\.(pdf|doc|docx|zip|rar|7z)(\?|$)', re.I)

def fetch(url, timeout=20):
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status, r.headers.get('Content-Type', ''), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Content-Type', ''), b''
    except Exception as e:
        return 0, str(e), b''

def classify(url):
    """链接分类：direct=文件直链 / platform=交易平台入口 / none=无"""
    if FILE_EXT.search(url):
        return 'direct'
    return 'platform'

def main():
    results = []
    for ann in ANNOUNCEMENTS:
        entry = {"name": ann["name"], "url": ann["url"], "links": [], "get_guide": []}
        status, ctype, html = fetch(ann["url"])
        entry["announce_status"] = status
        entry["announce_content_type"] = ctype
        if status != 200 or not html:
            results.append(entry)
            continue
        text = html.decode('utf-8', errors='ignore')

        # 1) 提取「招标文件的获取」章节指引文字
        for m in re.finditer(r'招标文件[^<]{0,4}获取', text):
            seg = text[m.start():m.start()+800]
            plain = re.sub(r'<[^>]+>', ' ', seg)
            plain = re.sub(r'\s+', ' ', plain).strip()
            if plain and plain not in entry["get_guide"]:
                entry["get_guide"].append(plain[:300])

        # 2) 提取附件/下载链接（a[href] + 常见模式）
        seen = set()
        for href in re.findall(r'href=["\']([^"\']+)["\']', text, re.I):
            if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                continue
            if any(k in href.lower() for k in ('.css', '.js', '.png', '.jpg', '.ico', '.gif')):
                continue
            full = urljoin(ann["url"], href)
            if full in seen:
                continue
            seen.add(full)
            # 只保留可能与招标文件相关的链接
            if ('download' in href.lower() or 'file' in href.lower() or 'attach' in href.lower()
                    or FILE_EXT.search(href) or 'zbwj' in href.lower() or 'tender' in href.lower()):
                entry["links"].append({"url": full, "type": classify(full), "found_in": "html"})

        # 3) 全文搜索 http 链接中疑似文件获取入口
        for url in re.findall(r'(https?://[^\s"\'<>]+)', text):
            if url in seen:
                continue
            if any(k in url.lower() for k in ('ggzy', 'jyzx', 'hbjtt', 'ebidding', 'zjiaoyi', 'zhangjiakou', 'hebei')):
                if 'download' in url.lower() or FILE_EXT.search(url) or 'zbwj' in url.lower():
                    entry["links"].append({"url": url, "type": classify(url), "found_in": "text"})

        results.append(entry)

    # 3) 对发现的链接做可达性验证（仅 HEAD/GET 状态，不下载内容）
    for entry in results:
        for link in entry["links"]:
            st, ct, _ = fetch(link["url"], timeout=15)
            link["check_status"] = st
            link["check_content_type"] = ct
            # 平台入口：取标题/是否需要登录的关键词提示
            if st == 200 and link["type"] == "platform":
                link["login_hint"] = "疑似需登录" if ('登录' in ct or 'login' in ct.lower()) else "可访问"

    print(json.dumps(results, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
