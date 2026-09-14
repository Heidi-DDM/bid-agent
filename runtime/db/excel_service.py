# F022 §5.1 v1.3（09-优化方案 §3.5）：Excel/CSV 台账受控导入服务
#
# 职责：对上传的台账文件做安全嗅探与预览（扩展名+魔数+大小+空文件+加密/损坏/
# 外部链接/宏拒绝），按字段映射读取结构化行，供企业资料导入；原文件作为不可变
# 企业私有材料（evidence）落库并回链 material_id，结构化行保留来源（sheet+行号）。
#
# 口径：本模块为纯逻辑 + openpyxl 只读解析（沙盒内可跑，不触网）。企业资料
# 不发送到外部模型；导入行默认 pending_verification，由数据管理员核验后 active。
# 无法自动识别的列不静默丢弃（unmapped_columns 返回待人工确认）；.xls 旧格式
# 仅做 OLE 嗅探与受控证据入库，不支持自动预览映射（如实说明，不推断）。
from __future__ import annotations

import csv
import io
import re
import zipfile

# 每类资料允许映射的字段键（对齐 enterprise_service.import_* 支持键）
ALLOWED_FIELD_KEYS: dict[str, frozenset[str]] = {
    "qualifications": frozenset({
        "category", "level", "specialty", "valid_from", "valid_until", "issuer",
        "evidence_refs", "data_owner",
    }),
    "performances": frozenset({
        "project_name", "project_type", "specialty", "contract_amount",
        "contract_amount_wan", "completed_at", "awarded_at", "owner_org",
        "scale", "evidence_refs", "data_owner",
    }),
    "personnel": frozenset({
        "name", "organization", "specialty", "cert_level", "cert_no",
        "valid_from", "valid_until", "issuer", "on_site", "on_site_project",
        "phone", "evidence_refs", "data_owner",
    }),
    "managers": frozenset({
        "display_name", "name", "organization", "specialty", "reg_cert_type",
        "reg_cert_no", "cert_level", "cert_valid_until", "b_cert_no",
        "edu_safety_status", "availability", "active_projects",
        "performance_refs", "evidence_refs", "data_owner",
    }),
}

SUPPORTED_SUFFIXES = (".xlsx", ".xls", ".csv")
# 台账预览/导入大小上限（50 MB）
LEDGER_MAX_BYTES = 50 * 1024 * 1024

# 常见列名别名（F006 §4/§6.7、F007 §6.4 实测源列名 + 英文键本身）→ 字段键。
# 仅用于 preview 的“未映射列”识别提示与默认建议；commit 始终以调用方显式 mapping 为准。
HEADER_ALIASES: dict[str, dict[str, str]] = {
    "qualifications": {
        "category": "category", "资质类别": "category", "资质": "category",
        "level": "level", "资质等级": "level", "等级": "level",
        "specialty": "specialty", "专业": "specialty",
        "valid_from": "valid_from", "发证日期": "valid_from",
        "valid_until": "valid_until", "有效期至": "valid_until", "有效期": "valid_until", "到期日": "valid_until",
        "issuer": "issuer", "发证机关": "issuer", "颁发机构": "issuer",
        "evidence_refs": "evidence_refs", "证据": "evidence_refs", "证据位置": "evidence_refs", "附件": "evidence_refs",
        "data_owner": "data_owner", "数据责任人": "data_owner", "责任人": "data_owner",
    },
    "performances": {
        "project_name": "project_name", "项目名称": "project_name", "工程名称": "project_name",
        "project_type": "project_type", "项目类型": "project_type", "工程类型": "project_type",
        "specialty": "specialty", "专业": "specialty",
        "contract_amount": "contract_amount", "合同金额": "contract_amount",
        "contract_amount_wan": "contract_amount_wan", "合同额(万元)": "contract_amount_wan",
        "合同额（万元）": "contract_amount_wan", "合同额": "contract_amount_wan", "合同金额(万元)": "contract_amount_wan",
        "completed_at": "completed_at", "竣工时间": "completed_at", "竣工日期": "completed_at",
        "awarded_at": "awarded_at", "中标时间": "awarded_at", "开工时间": "awarded_at",
        "owner_org": "owner_org", "业主单位": "owner_org", "建设单位": "owner_org", "业主": "owner_org",
        "scale": "scale", "规模": "scale",
        "evidence_refs": "evidence_refs", "证据": "evidence_refs", "合同扫描件": "evidence_refs", "附件": "evidence_refs",
        "data_owner": "data_owner", "数据责任人": "data_owner", "责任人": "data_owner",
    },
    "personnel": {
        "name": "name", "姓名": "name",
        "organization": "organization", "单位": "organization", "工作单位": "organization", "所在单位": "organization",
        "specialty": "specialty", "专业": "specialty", "注册专业": "specialty",
        "cert_level": "cert_level", "等级": "cert_level", "职称级别": "cert_level", "级别": "cert_level",
        "cert_no": "cert_no", "证书编号": "cert_no", "注册编号": "cert_no",
        "valid_from": "valid_from", "发证日期": "valid_from",
        "valid_until": "valid_until", "有效期至": "valid_until", "有效期": "valid_until",
        "issuer": "issuer", "发证机关": "issuer", "颁发机构": "issuer",
        "on_site": "on_site", "在施状态": "on_site", "是否在施": "on_site",
        "on_site_project": "on_site_project", "在施项目名称": "on_site_project",
        "phone": "phone", "联系电话": "phone", "电话": "phone",
        "evidence_refs": "evidence_refs", "证据": "evidence_refs", "附件": "evidence_refs",
        "data_owner": "data_owner", "数据责任人": "data_owner", "责任人": "data_owner",
    },
    "managers": {
        "display_name": "display_name", "name": "name", "姓名": "name", "项目经理": "display_name",
        "organization": "organization", "单位": "organization", "工作单位": "organization",
        "specialty": "specialty", "专业": "specialty", "注册专业": "specialty",
        "reg_cert_type": "reg_cert_type", "注册类型": "reg_cert_type",
        "reg_cert_no": "reg_cert_no", "注册编号": "reg_cert_no", "注册证书编号": "reg_cert_no",
        "cert_level": "cert_level", "等级": "cert_level",
        "cert_valid_until": "cert_valid_until", "证书有效期": "cert_valid_until", "有效期至": "cert_valid_until",
        "b_cert_no": "b_cert_no", "B证编号": "b_cert_no", "安全B证": "b_cert_no", "安全证书编号": "b_cert_no",
        "edu_safety_status": "edu_safety_status", "继续教育": "edu_safety_status", "安全考核": "edu_safety_status",
        "availability": "availability", "在施状态": "availability", "是否在施": "availability",
        "active_projects": "active_projects", "在施项目名称": "active_projects", "在建项目": "active_projects",
        "performance_refs": "performance_refs", "业绩": "performance_refs",
        "evidence_refs": "evidence_refs", "证据": "evidence_refs", "附件": "evidence_refs",
        "data_owner": "data_owner", "数据责任人": "data_owner", "责任人": "data_owner",
    },
}

_XLSX_MAGIC = b"PK"
_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# 常见“待补”占位符（与 enterprise_service 归一化口径一致，仅用于提示列质量）
_PLACEHOLDER_PATTERN = re.compile(r"后续补充|后续解决|不用填|待补|待完善|^[\s~-]*$")

_MAX_PREVIEW_ROWS = 5


class LedgerError(Exception):
    """台账文件校验失败（用户可读原因，映射为 400 业务错误）。"""


def sniff_ledger(content: bytes, suffix: str) -> str:
    """台账扩展名 + 魔数嗅探：返回合法后缀；不合法抛 LedgerError（如实原因）。"""
    if suffix not in SUPPORTED_SUFFIXES:
        raise LedgerError(
            f"台账仅支持 {', '.join(SUPPORTED_SUFFIXES)}"
            "（xlsm/xlsb/doc 等含宏或二进制格式不在白名单）"
        )
    if len(content) > LEDGER_MAX_BYTES:
        raise LedgerError(f"台账文件超过上限 {LEDGER_MAX_BYTES // (1024 * 1024)} MB")
    if not content:
        raise LedgerError("空文件：未上传台账内容")
    magic = content[:8]
    if suffix == ".xlsx" and not magic.startswith(_XLSX_MAGIC):
        raise LedgerError("文件内容不是 XLSX（魔数校验失败），请确认文件完整未损坏")
    if suffix == ".xls" and not magic.startswith(_XLS_MAGIC):
        raise LedgerError("文件内容不是 XLS（魔数校验失败），请确认文件完整未损坏")
    if suffix == ".csv":
        # CSV 为文本格式：拒绝二进制魔数（含 NUL 控制字节视为非文本）
        if b"\x00" in content[:1024]:
            raise LedgerError("文件内容不是 CSV 文本（含二进制字节），请确认文件完整")
    return suffix


def _reject_risky_xlsx(content: bytes) -> None:
    """XLSX 安全扫描：损坏/加密 ZIP、外部链接（外链公式风险）一律拒绝，无持久化残留。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise LedgerError(f"XLSX 损坏或加密（无法解压）：{exc}")
    names = zf.namelist()
    if "xl/workbook.xml" not in names:
        raise LedgerError("XLSX 缺少 workbook（疑似加密或结构损坏），拒绝导入")
    if any(n.startswith("xl/externalLinks/") for n in names):
        raise LedgerError("XLSX 含外部链接（外链公式风险），拒绝导入（F022 §5.1）")
    # 宏文件白名单外由扩展名拦截；xlsx 含 vbaProject 亦拒绝
    if any("vbaProject" in n for n in names):
        raise LedgerError("XLSX 内含 VBA 宏（vbaProject），拒绝导入（F022 §5.1）")


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _read_xlsx_rows(content: bytes) -> tuple[list[str], list[list[str]]]:
    """openpyxl 只读解析首个工作表 → (表头, 数据行)。损坏/加密抛 LedgerError。"""
    _reject_risky_xlsx(content)
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError:  # pragma: no cover - 依赖缺失时应由安装流程保证
        raise LedgerError("服务端缺少 openpyxl 依赖，无法解析 XLSX（联系运维安装 requirements-dev.txt）")
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError) as exc:
        raise LedgerError(f"XLSX 打开失败（加密/损坏/格式不支持）：{exc}")
    try:
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        headers_raw = next(rows_iter, None)
        headers = [_normalize_cell(h) for h in (headers_raw or [])]
        rows: list[list[str]] = []
        for values in rows_iter:
            rows.append([_normalize_cell(v) for v in values])
            if len(rows) >= 2000:
                break  # 预览与导入共用上限，防止畸形文件耗尽内存
        return headers, rows
    finally:
        wb.close()


def _read_csv_rows(content: bytes) -> tuple[list[str], list[list[str]]]:
    text = content.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise LedgerError("CSV 为空文件")
    reader = csv.reader(io.StringIO(text))
    rows_all = list(reader)
    if not rows_all:
        raise LedgerError("CSV 无任何行")
    headers = [_normalize_cell(h) for h in rows_all[0]]
    rows = [[_normalize_cell(v) for v in r] for r in rows_all[1:]][:2000]
    return headers, rows


def preview_ledger(content: bytes, suffix: str, *, kind: str | None = None) -> dict:
    """安全嗅探 + 预览（表头/行数/前 5 行/未映射列/质量警告）。无持久化副作用。

    kind ∈ qualifications/performances/personnel/managers 时，unmapped_columns
    按常见列名别名表（HEADER_ALIASES，F006 §4/§6.7）计算——无法自动识别的列
    返回待人工确认（映射阶段可补），不静默丢弃。
    """
    if kind is not None and kind not in ALLOWED_FIELD_KEYS:
        raise LedgerError(
            f"未知资料类型: {kind}（允许 qualifications/performances/personnel/managers）"
        )
    suffix = sniff_ledger(content, suffix)
    if suffix == ".xls":
        # 旧版二进制 XLS：OLE 嗅探已过；无开源只读解析承诺，如实提示转存
        return {
            "kind": kind,
            "sheet_name": None,
            "headers": [],
            "row_count": None,
            "preview_rows": [],
            "unmapped_columns": [],
            "warnings": [
                "旧版 .xls 无法自动预览表头与行内容：请转存为 .xlsx/.csv 后获得映射预览；"
                "如需直接留存，可作受控证据材料上传，由数据管理员人工录入结构化记录"
            ],
            "notes": ["xls-old-format"],
        }
    headers, rows = (
        _read_xlsx_rows(content) if suffix == ".xlsx" else _read_csv_rows(content)
    )
    alias = HEADER_ALIASES.get(kind or "", {})
    unmapped: list[str] = []
    for idx, header in enumerate(headers, start=1):
        if not header:
            unmapped.append(f"列{idx}（表头为空）")
        elif alias and header not in alias:
            unmapped.append(header)  # 别名表无法识别 → 待人工确认（映射阶段可补）
    warnings: list[str] = []
    for row in rows[: _MAX_PREVIEW_ROWS]:
        for cell in row:
            if cell and _PLACEHOLDER_PATTERN.match(cell):
                warnings.append("检测到「后续补充/待补」等占位符：导入时将按口径转『待补』，不推断")
                break
    return {
        "kind": kind,
        "sheet_name": None,
        "headers": [h for h in headers if h],
        "row_count": len(rows),
        "preview_rows": rows[: _MAX_PREVIEW_ROWS],
        "unmapped_columns": unmapped,
        "warnings": warnings,
        "notes": [],
    }


def read_ledger_rows(content: bytes, suffix: str, mapping: dict[str, str]) -> list[dict]:
    """按 {列名: 字段键} 映射读取台账行 → enterprise_service 行字典。

    未映射列跳过且不静默丢弃：调用方需先在 preview 阶段展示 unmapped 列供人工确认；
    行来源（sheet 名 + 行号）以 evidence_refs 前缀 ledger: 写入，保留来源回链。
    """
    suffix = sniff_ledger(content, suffix)
    if suffix == ".xls":
        raise LedgerError(
            "旧版 .xls 不支持自动映射导入：请转存 .xlsx/.csv 后重试，或作受控证据由数据管理员人工录入"
        )
    headers, rows = (
        _read_xlsx_rows(content) if suffix == ".xlsx" else _read_csv_rows(content)
    )
    col_to_key: dict[int, str] = {}
    for idx, header in enumerate(headers):
        key = mapping.get(header)
        if key:
            col_to_key[idx] = key
    out: list[dict] = []
    for row_idx, values in enumerate(rows, start=2):  # 表头行号=1
        if not any(values):
            continue
        row: dict = {}
        for col_idx, key in col_to_key.items():
            cell = values[col_idx] if col_idx < len(values) else ""
            if cell:
                row[key] = cell
        if not row:
            continue
        refs = row.pop("evidence_refs", None) or []
        if isinstance(refs, str):
            refs = [r for r in re.split(r"[;；,，]", refs) if r.strip()]
        row["evidence_refs"] = list(refs) + [f"ledger:{suffix.strip('.')}:row{row_idx}"]
        out.append(row)
    return out