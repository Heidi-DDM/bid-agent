# F021 §2 版面层（2026-09-24，解析优化四步之二）：表格单元格抽取。
#
# 方法来源（docs/10 §4 / 2026-09-24 调研）：MinerU / docling / marker 等高星解析器的共同
# 前提是「先还原版面结构、再抽取字段」——招标文件的投标人须知前附表正是典型：pdftotext
# 把多列表格拍平成文本流后，label 与值可能被邻列内容隔断/错行，正则在文本流里怎么写都会漏。
# 本模块用 pdfplumber（纯 Python，无 torch）做「表格行 → 单元格」还原，是这一思路的可落地
# 子集；MinerU/docling 全量接入属后续演进（docs/10 §5）。
#
# 接线哲学（与 P4 LLM 兜底一致）：
# - pdfplumber 为**可选依赖**：未安装 / 导入失败 / 任何异常 → 零副作用（返回空，主链照跑）；
# - 只服务「label → 同行右邻单元格取值」一类前附表字段（extractor 主卡兜底）；
# - **不把表格行并入 paragraphs**：文本流是锚点正则与逐字校验的基准，混入表格会引入
#   表头类误报（2026-09-24「项目经理 技术负责人 项目描述 备注」表头教训）；
# - 单元格文本 = 原文逐字（pdfplumber 按字符坐标拼装），不做拼接推断。
from __future__ import annotations

import logging

logger = logging.getLogger("runtime.parsing.layout")

_warned_unavailable = False


def tables_available() -> bool:
    """pdfplumber 是否可用（可选依赖；缺席只打一次 INFO，不打 ERROR——缺席是正常形态）。"""
    global _warned_unavailable
    try:
        import pdfplumber  # noqa: F401
        return True
    except ImportError:
        if not _warned_unavailable:
            logger.info("版面层未启用：未安装 pdfplumber（可选依赖，pip install pdfplumber；缺席零副作用）")
            _warned_unavailable = True
        return False


def extract_pdf_tables(path: str, *, max_pages: int = 20) -> dict[int, list[list[str]]]:
    """前 max_pages 页的表格 → {page_no: rows}；rows = 二维单元格文本（None→""、去换行）。

    前附表 / 公告 / 资格审查条款集中在文件前部，全文档逐页表格识别代价高收益低，
    max_pages 由 LAYOUT_TABLE_PAGES 控制（默认 20）。任何异常 → 空 dict（降级零副作用）。
    """
    if not tables_available():
        return {}
    out: dict[int, list[list[str]]] = {}
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages[:max_pages]:
                try:
                    tables = page.extract_tables() or []
                except Exception:  # 单页失败不影响其他页
                    continue
                rows = [[(c or "").replace("\n", "").strip() for c in row]
                        for t in tables for row in t]
                rows = [r for r in rows if any(r)]
                if rows:
                    out[page.page_number] = rows
    except Exception as exc:
        logger.warning("版面层表格抽取失败（降级零副作用）：%s", type(exc).__name__)
        return {}
    return out
