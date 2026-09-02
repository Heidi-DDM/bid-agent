#!/usr/bin/env python3
"""农大（ND-2025）RAG 三层验收——阶段 1：L2/L3 材料入库 + 向量索引（prepare + index，幂等可重跑）

材料形态（全部真实数据、全部只进本地 runtime 数据层，不入 Git）：
- L2 招标条款：`验证受限材料/农大/tender_full.txt`（pdftotext 477 页，源=企业资料台账20260821/…/施工招标文件.pdf）
  → Material MAT-ND-TENDER（tender_document / public / public_read / project ND-2025）
- L3 企业证据：`验证受限材料/农大/real_evidence_nongda.json`（脱敏结构化证据，商务标/技术标/保函/企业资料库人工核验产物）
  → 每 evidence kind 一个 Material MAT-ND-L3-<KIND>（evidence_file / enterprise / enterprise_read / project ND-2025）
  渲染文本=结构化记录的中文描述（金额/日期/等级/主体/项目名保留，供 BM25+向量检索命中）

索引：index_material(parsed_pages=...) → chunker 分片 → bge-m3 真实 embedding（8001 服务）→ pgvector。
幂等：knowledge_chunks 唯一键 (material,version,hash,model,index_version,seq) 跳过既有；材料行已存在跳过。

用法（真库环境，先 unset PYTHONPATH，服务三件套已 start）：
    DATABASE_URL=$(grep '^DATABASE_URL=' runtime/.env | cut -d= -f2-) \
        .venv/bin/python scripts/rag_acceptance/prepare_index.py [--skip-index]
输出：验证受限材料/农大/acceptance_materials.json（受限，不入 Git）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PROJECT_ID = "ND-2025"
L2_MATERIAL_ID = "MAT-ND-TENDER"
RESTRICTED = ROOT / "验证受限材料" / "农大"
TENDER_TXT = RESTRICTED / "tender_full.txt"
EVIDENCE_JSON = RESTRICTED / "real_evidence_nongda.json"
OBJECTS_ROOT = ROOT / "runtime" / "objects"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("缺少 DATABASE_URL")
    return Session(create_engine(url))


def _render_record(kind: str, rec: dict) -> str:
    """结构化证据记录 → 中文检索文本（只渲染记录内明确存在的字段，不推断）。"""
    parts = []
    order = ["subject", "category", "level", "cert_type", "manager_id", "display_name",
             "project_name", "project_type", "specialty", "declares", "checks", "negative",
             "amount", "area", "days", "continuous_months", "year", "status",
             "completed_at", "period_start", "period_end", "verified_at", "valid_until",
             "evidence_ref"]
    for key in order:
        if key in rec and rec[key] is not None:
            val = rec[key]
            if isinstance(val, bool):
                val = "有" if val else "无"
            parts.append(f"{key}={val}")
    for key in sorted(rec):
        if key not in order and rec[key] is not None:
            val = rec[key]
            if isinstance(val, (dict, list)):
                continue
            parts.append(f"{key}={val}")
    return f"[{kind}] " + "；".join(parts)


def _material_rows(material_id: str, material_type: str, owner_type: str, permission_scope: str,
                   classification: str, content: bytes, *, project_id: str | None = PROJECT_ID,
                   source_type: str = "uploaded") -> tuple[dict, dict, str]:
    """构造 Material/MaterialVersion 行字典 + 相对 object_uri（不入库，由调用方落）。"""
    content_hash = _sha256(content)
    version = 1
    obj_rel = f"{owner_type}/{material_id}/1/{content_hash[:16]}.txt"
    material = {
        "material_id": material_id, "version": version, "material_type": material_type,
        "source_type": source_type, "owner_type": owner_type, "classification": classification,
        "permission_scope": permission_scope, "content_hash": content_hash,
        "parse_status": "parsed", "status": "active", "evidence_refs": [],
        "data_owner": "rag_acceptance_seed", "verified_at": utcnow(),
        "project_id": project_id, "imported_at": utcnow(), "created_at": utcnow(), "updated_at": utcnow(),
    }
    ver = {"material_id": material_id, "version": version, "object_uri": obj_rel,
           "content_hash": content_hash, "imported_at": utcnow()}
    return material, ver, obj_rel


def ensure_material(sess, material_id: str) -> bool:
    from runtime.db.models import Material

    return sess.get(Material, (material_id, 1)) is not None


def _store_object(obj_rel: str, content: bytes) -> None:
    target = OBJECTS_ROOT / obj_rel
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def prepare(sess) -> list[dict]:
    """材料入库（幂等）：返回材料清单。"""
    manifest = []
    from runtime.db.models import Material, MaterialVersion

    # ---- L2 招标条款 ----
    if not TENDER_TXT.exists():
        raise SystemExit(f"缺少 L2 文本：{TENDER_TXT}（先 pdftotext 生成）")
    l2_bytes = TENDER_TXT.read_bytes()
    if not ensure_material(sess, L2_MATERIAL_ID):
        m, v, uri = _material_rows(L2_MATERIAL_ID, "tender_document", "public",
                                    "public_read", "public", l2_bytes)
        sess.add(Material(**m))
        sess.add(MaterialVersion(**v))
        sess.commit()
        _store_object(uri, l2_bytes)
        print(f"[L2] 材料入库 {L2_MATERIAL_ID}:v1 hash={m['content_hash'][:12]}")
    manifest.append({
        "material_id": L2_MATERIAL_ID, "version": 1, "knowledge_layer": "L2_tender",
        "owner_type": "public", "permission_scope": "public_read", "source": "tender_full.txt",
        "note": "招标文件 477 页条款全文（pdftotext -layout）",
    })

    # ---- L3 企业证据（按 evidence kind 一个材料） ----
    evidence = json.loads(EVIDENCE_JSON.read_text(encoding="utf-8"))
    for kind, records in evidence.items():
        if kind == "as_of_note" or not records:
            continue
        mid = f"MAT-ND-L3-{kind.upper()}"
        text = "\n".join(_render_record(kind, r) for r in records)
        content = text.encode("utf-8")
        if not ensure_material(sess, mid):
            m, v, uri = _material_rows(mid, "evidence_file", "enterprise",
                                       "enterprise_read", "internal", content)
            sess.add(Material(**m))
            sess.add(MaterialVersion(**v))
            sess.commit()
            _store_object(uri, content)
            print(f"[L3] 材料入库 {mid}:v1 records={len(records)}")
        manifest.append({
            "material_id": mid, "version": 1, "knowledge_layer": "L3_enterprise",
            "owner_type": "enterprise", "permission_scope": "enterprise_read",
            "source": "real_evidence_nongda.json", "kind": kind, "records": len(records),
        })
    sess.close()
    (RESTRICTED / "acceptance_materials.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"材料清单 {len(manifest)} 项 → {RESTRICTED / 'acceptance_materials.json'}")
    return manifest


def _l2_pages() -> list:
    """tender_full.txt（\f 分页）→ ParsedPage 列表。"""
    from runtime.rag.chunker import ParsedPage

    full = TENDER_TXT.read_text(encoding="utf-8")
    raw_pages = full.split("\f")
    pages = []
    for i, page in enumerate(raw_pages, start=1):
        paras = [ln.strip() for ln in page.splitlines() if ln.strip()]
        if not paras:
            continue
        # 长页合并为单段（chunker 内部按 600 字符再切，保留页码定位）
        pages.append(ParsedPage(page_no=i, paragraphs=["\n".join(paras)]))
    return pages


def _l3_pages(material: dict) -> list:
    from runtime.rag.chunker import ParsedPage

    obj_rel = f"enterprise/{material['material_id']}/1/"
    # 直接读 objects 内的渲染文件
    obj_dir = OBJECTS_ROOT / "enterprise" / material["material_id"] / "1"
    files = sorted(obj_dir.glob("*.txt")) if obj_dir.exists() else []
    if not files:
        raise SystemExit(f"缺少 L3 渲染文件：{obj_dir}")
    text = files[0].read_text(encoding="utf-8")
    return [ParsedPage(page_no=1, paragraphs=[text])]


def index_all(sess, manifest: list[dict], *, skip_embedding: bool = False) -> None:
    from runtime.rag.indexer import index_material
    from sqlalchemy import select

    from runtime.db.models import KnowledgeChunk, Material

    for item in manifest:
        mid, version = item["material_id"], item["version"]
        material = sess.get(Material, (mid, version))
        if material is None:
            print(f"[index] 材料缺失 {mid}，跳过")
            continue
        idx_version = f"{os.environ.get('EMBEDDING_MODEL', 'embedding')}:{material.content_hash[:12]}:{version}"
        existing = sess.scalar(select(KnowledgeChunk).where(
            KnowledgeChunk.material_id == mid,
            KnowledgeChunk.index_version == idx_version,
            KnowledgeChunk.index_status == "current",
        ))
        if existing is not None:
            print(f"[index] 已索引 {mid} idx={idx_version[:30]}…（跳过）")
            continue
        pages = _l2_pages() if mid == L2_MATERIAL_ID else _l3_pages(item)
        print(f"[index] 索引 {mid} pages={len(pages)} …")
        result = index_material(sess, mid, version, parsed_pages=pages)
        print(f"[index] {mid} created={result.created} skipped={result.skipped} "
              f"total={result.total_chunks} stale={result.stale_marked} idx={result.index_version[:28]}…")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-index", action="store_true", help="只做材料入库，不索引")
    args = parser.parse_args()

    sess = session()
    manifest = prepare(sess)
    if not args.skip_index:
        index_all(sess, manifest)
    sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
