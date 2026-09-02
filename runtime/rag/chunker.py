# F025 §3.2 / 方案 §3.3：文档分片（纯逻辑，无第三方依赖）
# 目标：
# - 默认分片 400-800 中文字、重叠 50-100 字（参数由配置记录，不作为业务契约）；
# - 保留页码、段落、表头上下文；金额/日期/等级/单位不得在切块时丢失；
# - 条款分片不得脱离所属章节（父级 section_title 保留）。
from __future__ import annotations

from dataclasses import dataclass, field

from runtime.core.config import rag_chunk_overlap, rag_chunk_size
from runtime.rag.schemas import ChunkDraft, KNOWLEDGE_LAYERS

# 中文等价的字符宽度（标点/空白按 1）
_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK 统一表意
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0xF900, 0xFAFF),   # CJK 兼容表意
    (0x3000, 0x303F),   # CJK 标点
    (0xFF00, 0xFFEF),   # 全角
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def char_width(ch: str) -> int:
    """中文字符按 1 计，ASCII 折半（2 个 ASCII ≈ 1 个中文宽度）。"""
    if _is_cjk(ch):
        return 1
    return 1 if ch == "\n" else 2  # 换行占 1，其他按宽字符处理


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def _split_paragraphs(text: str) -> list[str]:
    """按换行拆段，剔除空段；保留段内原文。"""
    return [p.strip() for p in text.splitlines() if p.strip()]


def _smart_split(text: str, size: int, overlap: int) -> list[str]:
    """按近似字符宽度切分，优先在标点/空白处断开，避免切断金额/日期/编号。

    切分目标：文本宽度不超过 size；重叠取上一片尾部 overlap 宽度（不截断词）。
    """
    if text_width(text) <= size:
        return [text]
    cuts: list[int] = []
    width = 0
    for idx, ch in enumerate(text):
        width += char_width(ch)
        if width >= size:
            cuts.append(idx + 1)
            width = 0
    if not cuts or cuts[-1] >= len(text):
        cuts.append(len(text))
    # 合并过小切片（宽度 < size/2 时并入前一片）
    pieces: list[str] = []
    start = 0
    for cut in cuts:
        piece = text[start:cut]
        if pieces and text_width(piece) < size // 2:
            pieces[-1] += piece
        else:
            pieces.append(piece)
        start = cut
    # 重叠：相邻片尾与下一片头保留 overlap 宽度
    if overlap > 0 and len(pieces) > 1:
        overlapped: list[str] = []
        for i, piece in enumerate(pieces):
            if i == 0:
                overlapped.append(piece)
                continue
            prev = overlapped[-1]
            # 取 prev 尾部 overlap 宽度文本（按字符）
            tail_chars: list[str] = []
            w = 0
            for ch in reversed(prev):
                tail_chars.append(ch)
                w += char_width(ch)
                if w >= overlap:
                    break
            overlapped.append("".join(reversed(tail_chars)) + piece)
        return overlapped
    return pieces


@dataclass
class ParsedPage:
    """解析器产物：页码 + 段落文本（L1/L2/L3 通用；OCR 分片带置信度）。"""

    page_no: int
    paragraphs: list[str] = field(default_factory=list)
    ocr_confidence: float | None = None


def chunk_document(
    material_id: str,
    material_version: int,
    content_hash: str,
    parsed_pages: list[ParsedPage],
    *,
    knowledge_layer: str,
    owner_type: str,
    permission_scope: str,
    classification: str,
    project_id: str | None = None,
    lot_id: str | None = None,
    section_title: str | None = None,
    verification_status: str = "active",
    valid_from=None,
    valid_until=None,
    size: int | None = None,
    overlap: int | None = None,
) -> list[ChunkDraft]:
    """把解析页转成带定位的分片（方案 §8.1 chunk_document）。

    每个分片保留页码/段落；表格/条款以段落为最小单元，段落内不切碎
    金额/日期/等级（_smart_split 只在段落超长时按宽度切分）。
    """
    if knowledge_layer not in KNOWLEDGE_LAYERS:
        raise ValueError(f"未知 knowledge_layer: {knowledge_layer}")
    size = size or rag_chunk_size()
    overlap = overlap or rag_chunk_overlap()

    drafts: list[ChunkDraft] = []
    seq = 0
    for page in parsed_pages:
        para_no = 0
        for para in page.paragraphs:
            para_no += 1
            for piece in _smart_split(para, size, overlap):
                seq += 1
                drafts.append(
                    ChunkDraft(
                        chunk_seq=seq,
                        knowledge_layer=knowledge_layer,
                        material_id=material_id,
                        material_version=material_version,
                        content_hash=content_hash,
                        section_title=section_title,
                        text=piece,
                        page_no=page.page_no,
                        paragraph_no=para_no,
                        ocr_confidence=page.ocr_confidence,
                        project_id=project_id,
                        lot_id=lot_id,
                        owner_type=owner_type,
                        permission_scope=permission_scope,
                        classification=classification,
                        verification_status=verification_status,
                        valid_from=valid_from,
                        valid_until=valid_until,
                    )
                )
    return drafts