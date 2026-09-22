#!/usr/bin/env python
"""回填企业证据文件的文档路由结论与 OCR 置信度（2026-09-22 业主要求）。

背景：13 份证据（资质/安许/信用扫描件 + 典型项目中标合同验收）入库时未落
ocr_confidence，页面上该列为空。本脚本对每份证据原件执行既有文档路由
（runtime/parsing/router.py：文本层 pdftotext 优先，扫描件 tesseract chi_sim OCR），
把结论如实写入：
- 有文本层 → ocr_confidence 保持 NULL，review_note="文档路由：有文本层…无需 OCR"；
- 扫描件 OCR → ocr_confidence=平均置信度，review_note 注明引擎与均值；
  置信度 < 0.9 → review_status="pending_review"（F006 §6.6 低置信度进人工复核）；
- 路由失败 → review_note 记录原因，不改状态（不静默）。
每份文件一条审计，可回溯。

用法：
  DATABASE_URL=... OBJECT_STORE_ROOT=... python scripts/backfill_evidence_ocr.py [--dry-run]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from runtime.core.config import object_store_root  # noqa: E402
from runtime.db import api_service  # noqa: E402
from runtime.db.models import EvidenceFile  # noqa: E402
from runtime.parsing.router import route_document  # noqa: E402

LOW_CONF = 0.9  # F006 §6.6 低置信度阈值


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("缺少 DATABASE_URL")
        return 2
    dry_run = "--dry-run" in sys.argv
    store = Path(object_store_root())
    engine = create_engine(database_url)
    routed_text = routed_ocr = failed = 0
    with Session(engine) as session:
        rows = session.scalars(select(EvidenceFile).order_by(EvidenceFile.evidence_id)).all()
        for e in rows:
            path = store / e.object_uri
            print(f"== {e.evidence_id}（{e.file_type}）")
            if not path.exists():
                e.review_note = f"文档路由失败：对象文件不存在（{e.object_uri}）"
                failed += 1
                print(f"   {e.review_note}")
                continue
            try:
                result = route_document(str(path))
            except Exception as exc:  # noqa: BLE001 —— 路由异常如实记录不中断
                e.review_note = f"文档路由失败：{exc}"
                failed += 1
                print(f"   {e.review_note}")
                continue
            conf = result.confidence
            if result.kind in ("text_pdf", "docx", "doc", "text"):
                e.ocr_confidence = None
                e.review_note = (f"文档路由：有文本层（{result.kind}，pdftotext/内嵌文本"
                                 " 提取，无需 OCR）")
                routed_text += 1
                print("   有文本层，无需 OCR")
            else:
                e.ocr_confidence = round(conf, 2) if conf is not None else None
                e.review_note = (f"文档路由：OCR（tesseract chi_sim，"
                                 f"{len(result.pages)} 页，平均置信度 "
                                 f"{conf:.2f}）" if conf is not None else
                                 "文档路由：OCR 完成，但未返回置信度")
                if conf is not None and conf < LOW_CONF:
                    e.review_status = "pending_review"
                    e.review_note += f"（低于 {LOW_CONF} 阈值 → 待人工复核）"
                routed_ocr += 1
                print(f"   OCR 平均置信度 {conf}")
            if not dry_run:
                api_service.audit(
                    session, actor="ocr-backfill", action="enterprise.evidence.route",
                    basis=f"evidence={e.evidence_id} kind={result.kind}",
                    outcome=(e.review_note or "")[:120], object_ref=e.evidence_id,
                )
        if not dry_run:
            session.commit()
    print(f"\n完成：文本层 {routed_text} 份、OCR {routed_ocr} 份、失败 {failed} 份"
          f"{'（dry-run，未写库）' if dry_run else ''}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
