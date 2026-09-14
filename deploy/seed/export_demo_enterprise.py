#!/usr/bin/env python3
"""导出本地 enterprise_data 演示底座为脱敏 SQL（docs/11-部署方案-阿里云演示环境.md §6）。

用法（本机仓库根目录；DATABASE_URL 缺省读取 runtime/.env）：
    .venv/bin/python deploy/seed/export_demo_enterprise.py
    → 生成 deploy/seed/out/enterprise_demo.sql（目录已被 .gitignore 排除）

脱敏规则（只含通用模式，不含任何真实值；对所有文本与 JSON 内字符串递归生效）：
  1. 人名：括号内 2-3 字中文名 + "，" 且后接证件/职称关键词 → 保留姓氏 + "某"
  2. 注册执业证号：省份简称 + 16 位以上数字 → 保留前 4 位与后 4 位
  3. 安全生产考核证：X建安[A-C](年份)数字 → 数字段全部掩码
  4. 资质证书编号：字母+3 位数字+字母+5 位数字（如 D000A00000 形态）→ 末 5 位掩码
  5. 许可证字号：〔年份〕6 位数字 → 保留末 2 位
  6. 手机号 / 18 位身份证号：防御性掩码（F018 §5 本就禁止记录，若存在一律掩码）

导出为 INSERT ... ON CONFLICT DO NOTHING，可重复执行。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "runtime" / ".env"
OUT_DIR = Path(__file__).resolve().parent / "out"
OUT_FILE = OUT_DIR / "enterprise_demo.sql"

SCHEMA = "enterprise_data"
# 导出顺序：证据文件最后（可能被其他表引用）
TABLES = ("qualifications", "managers", "performances", "personnel", "evidence_files")

_MASK_RULES: list[tuple[str, re.Pattern[str], object]] = [
    # 括号紧跟 2-3 字中文 + 全角逗号 → 人名（"（施工业绩一，" 等 4 字以上描述不命中）
    ("person_name",
     re.compile(r"（([\u4e00-\u9fa5])[\u4e00-\u9fa5]{1,2}，"),
     lambda m: f"（{m.group(1)}某，"),
    ("registration_no",
     re.compile(r"([\u4e00-\u9fa5])(\d{4})(\d{8,14})(\d{4})"),
     lambda m: m.group(1) + m.group(2) + "*" * len(m.group(3)) + m.group(4)),
    # 安全生产考核证 A/B/C 类（含 C1/C2/C3 细分）
    ("safety_cert",
     re.compile(r"([\u4e00-\u9fa5]建安[A-C]\d?\(\d{4}\))(\d+)"),
     lambda m: m.group(1) + "*" * len(m.group(2))),
    ("qualification_cert",
     re.compile(r"\b([A-Z]\d{3}[A-Z])(\d{5})\b"),
     lambda m: m.group(1) + "*****"),
    ("license_no",
     re.compile(r"(〔\d{4}〕)(\d{4})(\d{2})"),
     lambda m: m.group(1) + "****" + m.group(3)),
    ("mobile",
     re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)"),
     lambda m: m.group(1) + "****" + m.group(3)),
    ("id_card",
     re.compile(r"(?<!\d)(\d{6})(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
     lambda m: m.group(1) + "********" + m.group(0)[-4:]),
]

_stats: dict[str, int] = {name: 0 for name, _, _ in _MASK_RULES}


def _dumps_cn(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def mask_text(value: str) -> str:
    for name, pattern, repl in _MASK_RULES:
        value, n = pattern.subn(repl, value)  # type: ignore[arg-type]
        _stats[name] += n
    return value


def mask_value(value):
    """递归处理 str / list / dict；其他类型原样返回。"""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in value.items()}
    return value


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        m = re.search(r"^DATABASE_URL=(.+)$", ENV_FILE.read_text(encoding="utf-8"), re.M)
        if not m:
            sys.exit("未找到 DATABASE_URL（env 或 runtime/.env）")
        url = m.group(1).strip().strip('"')
    return re.sub(r"^postgresql\+psycopg://", "postgresql://", url)


def main() -> int:
    try:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb
    except ImportError:
        sys.exit("需要 psycopg（项目 .venv 已装）")

    conn = psycopg.connect(database_url(), cursor_factory=psycopg.ClientCursor)
    cur = conn.cursor()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "-- 演示环境企业资料脱敏种子（deploy/seed/export_demo_enterprise.py 生成；docs/11 §6）",
        "-- 幂等：ON CONFLICT DO NOTHING",
        "BEGIN;",
    ]
    row_total = 0
    for table in TABLES:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (SCHEMA, table),
        )
        cols = cur.fetchall()
        if not cols:
            print(f"  跳过（表不存在）：{SCHEMA}.{table}")
            continue
        names = [c[0] for c in cols]
        json_cols = {c[0] for c in cols if c[1] in ("json", "jsonb")}

        cur.execute(sql.SQL("SELECT {} FROM {}.{} ORDER BY 1").format(
            sql.SQL(", ").join(sql.Identifier(n) for n in names),
            sql.Identifier(SCHEMA), sql.Identifier(table)))
        rows = cur.fetchall()
        lines.append(f"\n-- {SCHEMA}.{table}: {len(rows)} 行")
        for row in rows:
            params = []
            for name, value in zip(names, row):
                masked = mask_value(value)
                # 中文原样输出（不转 \\u 转义），便于人工复核脱敏结果
                params.append(Jsonb(masked, dumps=_dumps_cn) if name in json_cols and masked is not None else masked)
            stmt = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING;").format(
                sql.Identifier(SCHEMA), sql.Identifier(table),
                sql.SQL(", ").join(sql.Identifier(n) for n in names),
                sql.SQL(", ").join(sql.Placeholder() for _ in names),
            )
            lines.append(cur.mogrify(stmt, params))
        row_total += len(rows)
        print(f"  {SCHEMA}.{table}: {len(rows)} 行")
    lines.append("COMMIT;")
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n已写入 {OUT_FILE}（{row_total} 行）")
    print("脱敏命中统计：" + json.dumps(_stats, ensure_ascii=False))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
