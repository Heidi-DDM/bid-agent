#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把客户端 HTML 文件转成保留可点击链接的 PDF（2026-09-23 苹果手机适配）。

用法（仓库根运行，转换结果与 HTML 同目录同名 .pdf）：
  .venv/bin/python scripts/html_to_pdf.py 推送日志/xxx.html [--variant push|plain]

- push：推送列表专用——打印版自动展开「公告解析」（覆盖内联 display:none）、
  隐藏展开按钮、每卡不跨页、追加可复制的原文链接行；
- plain：普通文档——仅分节不跨页 + 打印颜色保留。
- 引擎：Chrome 无头打印（新版保留链接注释，点标题/按钮仍可直达网页）。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

BASE_PRINT_CSS = """
<style>
  @media print {
    * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    section, .card { break-inside: avoid; page-break-inside: avoid; }
  }
</style>
</head>"""

PUSH_PRINT_CSS = """
<style>
  @media print {
    * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    button { display: none !important; }
    .detail { display: block !important; }
    .detail::before { content: "公告解析"; display: block; font-weight: 700;
                      font-size: 13px; color: #14335c; margin-bottom: 4px; }
    .card { break-inside: avoid; page-break-inside: avoid; }
    .srcurl { font-size: 11px; color: #7a8aa0; word-break: break-all; margin-top: 6px; }
  }
  .srcurl { font-size: 11px; color: #7a8aa0; word-break: break-all; margin-top: 6px; }
</style>
</head>"""


def make_push_variant(src: str) -> str:
    src = src.replace("</head>", PUSH_PRINT_CSS, 1)

    def add_url(m: re.Match) -> str:
        block = m.group(0)
        href = re.search(r'href="([^"]+)"', block)
        url = href.group(1) if href else ""
        return block.replace(
            '<div class="detail" style="display:none">',
            f'<div class="detail" style="display:none">'
            f'<div class="srcurl">原文链接：{url}</div>', 1)

    src = re.sub(r'<div class="card">.*?<div class="detail" style="display:none">',
                 add_url, src, flags=re.S)
    src = src.replace(
        "每条均可：① 点「查看公告原文」进入交易平台公告页（用 CA 锁登录即可下载招标文件）；\n  ② 点「公告解析」查看系统解析出的要点（资质、金额、截止时间等）。",
        "每条均可：① 点标题或「查看公告原文」直达交易平台公告页（用 CA 锁登录即可下载招标文件）；② 「公告解析」要点已直接展开（资质、金额、截止时间等）。")
    return src


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    variant = "plain"
    for a in sys.argv[1:]:
        if a.startswith("--variant"):
            variant = a.split("=", 1)[1] if "=" in a else "push"
    if not args:
        print(__doc__)
        return 2
    src_path = Path(args[0])
    out_path = src_path.with_suffix(".pdf")
    src = src_path.read_text(encoding="utf-8")
    if variant == "push":
        src = make_push_variant(src)
    else:
        src = src.replace("</head>", BASE_PRINT_CSS, 1)

    tmp = Path(tempfile.gettempdir()) / f"pdf_print_{src_path.stem}.html"
    tmp.write_text(src, encoding="utf-8")

    subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out_path}", tmp.as_uri()], check=True)
    print(f"已生成 {out_path.resolve()}（variant={variant}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
