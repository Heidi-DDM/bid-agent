# R021/F017：Document Router（runtime 版）
# 把 r017 演练（scripts/parse_lab/r017/r017_ocr_engine.py）升级为 runtime 模块：
#   - 逐页输出（pdftotext -f/-l 按页提取，页码真实保留）——原演练仅单页合并
#   - 保留原文 sha256 / kind 判定 / OCR 置信度 / needs_review
#   - 失败/低置信度不静默：进 manual_review（由调用方/worker 决策）
# 依赖：macOS/内网命令行工具（readyz 已检查）：
#   pdftotext / pdftoppm（poppler）、tesseract（chi_sim）、textutil（.doc）
# 合规：企业资料仅本地处理，不出内网；.gef/.etb 不绕过（F005 §4.4）。
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONF_THRESHOLD = 0.90          # 字段级置信度阈值
PAGE_CONF_THRESHOLD = 0.60     # 整页置信度阈值（低于 → needs_review）
TEXT_PDF_MIN_CHARS = 20        # pdftotext 输出少于该字符 → 判为扫描件（F017 §3）

_TESSERACT = "/opt/homebrew/bin/tesseract"
_PDFTOTEXT = "/opt/homebrew/bin/pdftotext"
_PDFTOPPM = "/opt/homebrew/bin/pdftoppm"
_TEXTUTIL = "/usr/bin/textutil"

# 允许测试/低配环境覆盖工具路径（env 注入，非 .env 配置项）
TOOL_ENV = {
    "tesseract": os.environ.get("RUNTIME_TESSERACT", _TESSERACT),
    "pdftotext": os.environ.get("RUNTIME_PDFTOTEXT", _PDFTOTEXT),
    "pdftoppm": os.environ.get("RUNTIME_PDFTOPPM", _PDFTOPPM),
    "textutil": os.environ.get("RUNTIME_TEXTUTIL", _TEXTUTIL),
}


@dataclass
class ParsedPage:
    """解析器产物：页码 + 段落文本（对齐 runtime/rag/chunker.ParsedPage）。

    tables = 版面层表格行（[[单元格,…],…]；可选能力，pdfplumber 缺席时恒为空）。
    只供「label→同行右邻单元格」类主卡兜底取值；不并入 paragraphs（文本流是锚点
    正则与逐字校验的基准，混入表格会引入表头类误报，2026-09-24）。"""

    page_no: int
    paragraphs: list[str] = field(default_factory=list)
    ocr_confidence: float | None = None
    tables: list[list[str]] = field(default_factory=list)


@dataclass
class RouteResult:
    """路由结果：kind + 页文本 + 置信度 + 原文引用。"""

    kind: str  # text_pdf / scanned_pdf / docx / doc / image / unsupported / error
    source: str
    sha256: str
    pages: list[ParsedPage] = field(default_factory=list)
    confidence: float | None = None
    needs_review: bool = False
    note: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "source": self.source,
            "sha256": self.sha256,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "note": self.note,
            "error": self.error,
            "pages": [
                {"page_no": p.page_no, "ocr_confidence": p.ocr_confidence,
                 "paragraph_count": len(p.paragraphs)}
                for p in self.pages
            ],
        }


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return r.returncode, r.stdout or "", r.stderr or ""
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"


def _tool(name: str) -> str:
    return TOOL_ENV[name]


def _split_paragraphs(text: str) -> list[str]:
    """按换行拆段，剔除空段与纯页码行；保留原文（F005 溯源需要逐字摘录）。"""
    out: list[str] = []
    for p in text.splitlines():
        p = p.strip()
        if not p:
            continue
        # 纯页码/页眉噪声行（如 "第 1 页 / 共 477 页" 或孤立数字）仍保留——原文摘录不裁内容；
        # 但全空白已剔除。
        out.append(p)
    return out


def _pages_from_pdftotext(path: str, *, ocr: bool = False,
                          conf: float | None = None) -> list[ParsedPage]:
    """按页调用 pdftotext -f N -l N 提取，保留真实页码（r017 演练只合并为 1 页，本次修正）。

    效率说明：单文件按页调用 N 次 pdftotext；477 页农大文件约 8-15s（演示可接受）。
    页数未知时先 pdftotext 全量输出数 \f 分页符得到页数，再逐页提取。
    """
    pages: list[ParsedPage] = []
    # 1) 总页数：pdftotext 全量输出中 form-feed 计数 + 1
    rc, full, _ = _run([_tool("pdftotext"), "-layout", path, "-"])
    if rc != 0:
        return pages
    ff_count = full.count("\f")
    total = ff_count + 1 if full.strip() else 0
    if total <= 0:
        return pages
    for page_no in range(1, total + 1):
        rc2, text, _ = _run([_tool("pdftotext"), "-layout", "-f", str(page_no), "-l", str(page_no), path, "-"])
        if rc2 == 0:
            paras = _split_paragraphs(text)
            if paras:
                pages.append(ParsedPage(page_no=page_no, paragraphs=paras,
                                        ocr_confidence=conf if ocr else None))
    return pages


def is_scanned_pdf(path: str) -> bool:
    """PDF 是否无文本层：pdftotext 输出字符 < TEXT_PDF_MIN_CHARS 判为扫描件（F017 §3）。"""
    rc, out, _ = _run([_tool("pdftotext"), path, "-"])
    if rc != 0:
        return True
    chars = re.sub(r"[\s\f]+", "", out)
    return len(chars) < TEXT_PDF_MIN_CHARS


def pdf_to_images(path: str, dpi: int = 300) -> list[Path]:
    """扫描 PDF 每页转 PNG（临时目录，调用方负责清理）。"""
    tmp = Path(tempfile.mkdtemp(prefix="bid_router_"))
    rc, _, _ = _run([_tool("pdftoppm"), "-png", "-r", str(dpi), path, str(tmp / "page")])
    if rc != 0:
        return []
    return sorted(tmp.glob("page-*.png"))


def _ocr_image(img_path: str, lang: str = "chi_sim") -> tuple[str, float, list[dict]]:
    """tesseract OCR 单图：返回 (文本, 整页置信度, 词级明细)。"""
    rc, out, _ = _run([_tool("tesseract"), img_path, "stdout", "-l", lang, "--psm", "3", "tsv"])
    if rc != 0:
        return "", 0.0, []
    lines = out.splitlines()
    if not lines:
        return "", 0.0, []
    header = lines[0].split("\t")
    try:
        ci_conf = header.index("conf")
        ci_text = header.index("text")
    except ValueError:
        return "", 0.0, []
    confs: list[float] = []
    text_parts: list[str] = []
    for ln in lines[1:]:
        f = ln.split("\t")
        if len(f) <= ci_conf:
            continue
        try:
            c = float(f[ci_conf])
        except ValueError:
            continue
        t = f[ci_text].strip() if len(f) > ci_text else ""
        if not t:
            continue
        text_parts.append(t)
        if c > 0:
            confs.append(c / 100.0)
    conf = sum(confs) / len(confs) if confs else 0.0
    return " ".join(text_parts), conf, []


def _route_text_pdf(path: str, digest: str) -> RouteResult:
    pages = _pages_from_pdftotext(path)
    if not pages:
        return RouteResult(kind="error", source=path, sha256=digest,
                           error="pdftotext 无输出（文本层为空？）",
                           needs_review=True, note="解析失败进人工复核（F005 §7）")
    _attach_page_tables(path, pages)
    return RouteResult(kind="text_pdf", source=path, sha256=digest,
                       pages=pages, confidence=1.0, needs_review=False)


def _attach_page_tables(path: str, pages: list[ParsedPage]) -> None:
    """版面层表格行挂到对应页（可选能力：pdfplumber 缺席/失败/无表格 → 零副作用）。

    只抽前 layout_table_pages() 页（前附表/公告区在文件前部），供 extractor 主卡
    label→单元格兜底；不影响 paragraphs 与既有锚点行为。"""
    try:
        from runtime.core.config import layout_table_pages
        from runtime.parsing import layout
        tables = layout.extract_pdf_tables(path, max_pages=layout_table_pages())
    except Exception:  # 版面层任何异常不得阻断主链
        return
    for p in pages:
        rows = tables.get(p.page_no)
        if rows:
            p.tables = rows


def _route_scanned_pdf(path: str, digest: str) -> RouteResult:
    imgs = pdf_to_images(path)
    if not imgs:
        return RouteResult(kind="scanned_pdf", source=path, sha256=digest,
                           error="pdftoppm 转图失败", needs_review=True)
    pages: list[ParsedPage] = []
    confs: list[float] = []
    try:
        for i, img in enumerate(imgs, start=1):
            text, conf, _ = _ocr_image(str(img))
            confs.append(conf)
            paras = _split_paragraphs(text)
            pages.append(ParsedPage(page_no=i, paragraphs=paras, ocr_confidence=conf))
    finally:
        for img in imgs:
            img.unlink(missing_ok=True)
    overall = sum(confs) / len(confs) if confs else 0.0
    return RouteResult(
        kind="scanned_pdf", source=path, sha256=digest,
        pages=pages, confidence=overall,
        needs_review=overall < PAGE_CONF_THRESHOLD,
        note="OCR 整页置信度低于 0.60 → 人工复核（F017 §4）" if overall < PAGE_CONF_THRESHOLD else None,
    )


def _route_docx(path: str, digest: str) -> RouteResult:
    """DOCX = zip；解包 word/document.xml 提取段落文本（F005 §4.4，无第三方依赖）。"""
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paras = [
            " ".join("".join(t.text or "" for t in p.iter("{%s}t" % ns["w"])).split())
            for p in root.iter("{%s}p" % ns["w"])
        ]
        paras = [p for p in paras if p.strip()]
    except Exception as exc:  # zipfile.BadZipFile / ET.ParseError / KeyError
        return RouteResult(kind="docx", source=path, sha256=digest,
                           error=f"docx 解包失败: {type(exc).__name__}",
                           needs_review=True, note="解析失败进人工复核（F005 §7）")
    # DOCX 无真实分页信息：整档作 1 页处理（页码定位在 docx 中不可得，属已知限制）
    return RouteResult(kind="docx", source=path, sha256=digest,
                       pages=[ParsedPage(page_no=1, paragraphs=paras)],
                       confidence=1.0, needs_review=False)


def _route_doc(path: str, digest: str) -> RouteResult:
    tmp = Path(tempfile.mkdtemp(prefix="bid_doc_"))
    out = tmp / "out.txt"
    rc, _, _ = _run([_tool("textutil"), "-convert", "txt", "-output", str(out), path])
    text = out.read_text(encoding="utf-8", errors="ignore") if rc == 0 and out.exists() else ""
    paras = _split_paragraphs(text)
    if not paras:
        return RouteResult(kind="doc", source=path, sha256=digest,
                           error="textutil 转换失败或无文本", needs_review=True)
    return RouteResult(kind="doc", source=path, sha256=digest,
                       pages=[ParsedPage(page_no=1, paragraphs=paras)],
                       confidence=1.0, needs_review=False)


def _route_image(path: str, digest: str) -> RouteResult:
    text, conf, _ = _ocr_image(path)
    paras = _split_paragraphs(text)
    if not paras:
        return RouteResult(kind="image", source=path, sha256=digest,
                           error="OCR 无输出", needs_review=True)
    return RouteResult(kind="image", source=path, sha256=digest,
                       pages=[ParsedPage(page_no=1, paragraphs=paras, ocr_confidence=conf)],
                       confidence=conf, needs_review=conf < PAGE_CONF_THRESHOLD)


def _sniff_kind(path: Path) -> str | None:
    """按文件头魔数嗅探真实文档类型（扩展名缺失/不可信时回退用，返回 route 分支名）。"""
    try:
        head = path.open("rb").read(8)
    except OSError:
        return None
    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        return ".docx"  # zip 容器：docx（word/document.xml 校验在 _route_docx 内完成）
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".doc"   # OLE2 复合文档（旧版 .doc）
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"
    if head.startswith(b"BM"):
        return ".bmp"
    return None


def _route_text_file(p: Path, digest: str) -> RouteResult:
    """纯文本 → 单页（公告原文以净化 txt 固化，对象库禁 html，F019 §4；L1 索引用）。"""
    for enc in ("utf-8", "gb18030"):
        try:
            text = p.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return RouteResult(kind="error", source=str(p), sha256=digest,
                           error="文本编码无法识别（utf-8/gb18030 均失败）", needs_review=True)
    if not text.strip():
        return RouteResult(kind="error", source=str(p), sha256=digest,
                           error="空文本文件", needs_review=True)
    return RouteResult(kind="text", source=str(p), sha256=digest,
                       pages=[ParsedPage(page_no=1, paragraphs=[ln for ln in text.splitlines() if ln.strip()])])


def _looks_like_text(p: Path) -> bool:
    """魔数嗅探失败后的纯文本探测：前 4KB 可解码且控制字符占比 <5%（排除 \n\r\t）。"""
    head = p.read_bytes()[:4096]
    if not head:
        return False
    for enc in ("utf-8", "gb18030"):
        try:
            s = head.decode(enc)
        except (UnicodeDecodeError, ValueError, LookupError):
            continue
        ctrl = sum(1 for ch in s if ord(ch) < 32 and ch not in "\n\r\t")
        return ctrl / max(len(s), 1) < 0.05
    # 截断的多字节序列会解码失败：errors=ignore 只做可打印率估计（嗅探用途足够）
    s = head.decode("utf-8", errors="ignore") + head.decode("gb18030", errors="ignore")
    ctrl = sum(1 for ch in s if ord(ch) < 32 and ch not in "\n\r\t")
    return ctrl / max(len(s), 1) < 0.05


def route_document(path: str) -> RouteResult:
    """Document Router 入口：文件 → kind 判定 → 逐页文本（F017 §3 路由表）。

    不支持格式（.gef/.etb/.bde）→ unsupported（不绕过保护，F005 §4.4）；
    损坏/空文件/工具缺失 → error + needs_review（不静默跳过）。
    扩展名缺失或不可信（如对象存储的 sha256 文件名）→ 按文件头魔数嗅探回退；
    嗅探到 zip 但非 docx（缺 word/document.xml）→ unsupported（不误解析任意 zip）。
    """
    p = Path(path)
    ext = p.suffix.lower()
    digest = sha256_file(path)
    if ext == ".pdf":
        try:
            scanned = is_scanned_pdf(path)
        except OSError as exc:
            return RouteResult(kind="error", source=path, sha256=digest,
                               error=f"PDF 读取失败: {exc}", needs_review=True)
        if scanned:
            return _route_scanned_pdf(path, digest)
        return _route_text_pdf(path, digest)
    if ext == ".docx":
        return _route_docx(path, digest)
    if ext in (".txt", ".text", ".md"):
        return _route_text_file(p, digest)
    if ext == ".doc":
        return _route_doc(path, digest)
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        return _route_image(path, digest)
    if ext in (".gef", ".etb", ".bde"):
        return RouteResult(kind="unsupported", source=path, sha256=digest,
                           needs_review=True, note=f"加密固化格式 {ext}：不解析、不绕过（F005 §4.4）")
    if ext in ("", ".bin", ".dat", ".blob", ".sha256", ".digest"):
        sniffed = _sniff_kind(p)
        if sniffed is None:
            # 嗅探失败再探纯文本：对象库里的公告原文是无扩展名的 sha256 文件名 txt
            # （2026-09-15：此前 unsupported → L1 索引任务失败）
            if _looks_like_text(p):
                return _route_text_file(p, digest)
            return RouteResult(kind="unsupported", source=path, sha256=digest,
                               needs_review=True, note=f"无法识别文件类型（魔数嗅探失败）{ext}")
        if sniffed == ".pdf":
            try:
                scanned = is_scanned_pdf(path)
            except OSError as exc:
                return RouteResult(kind="error", source=path, sha256=digest,
                                   error=f"PDF 读取失败: {exc}", needs_review=True)
            if scanned:
                return _route_scanned_pdf(path, digest)
            return _route_text_pdf(path, digest)
        if sniffed == ".docx":
            return _route_docx(path, digest)
        if sniffed == ".doc":
            return _route_doc(path, digest)
        return _route_image(path, digest)
    return RouteResult(kind="unsupported", source=path, sha256=digest,
                       needs_review=True, note=f"不支持格式 {ext}（F005 §4.4）")
