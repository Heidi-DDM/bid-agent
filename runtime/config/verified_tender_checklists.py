"""人工复核的招标文件材料清单（只读受控配置）。

这不是企业台账，也不是匹配引擎规则。它用于补足历史样本中解析规则集漏掉的
「投标文件组成 / 附件资料」展示，并以 project、材料 ID、材料版本三元组绑定。

维护规则：
- 每项必须能回链到招标文件正文或附件页；
- 不能由此推导企业“有/没有”某项材料；
- 不能改变 Requirement、MatchRun 或内部准入结论；
- 新文件/澄清应先走解析确认和版本化，不能复制本样本条目。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


# 页码是 PDF 实际页号，供 /materials/.../file?version=...#page=N 定位。
# 条目由 2026-09-30 对该历史样本招标文件的人工通读复核建立。
_CHECKLISTS: dict[tuple[str, str, int], dict[str, Any]] = {
    ("PJ-e22ce139ac", "MAT-PJe22ce139ac-TENDER", 1): {
        "review_note": (
            "这是对当前历史样本招标文件正文、资格后审表和第八章附件逐项复核得到的“文件明确材料清单”。"
            "仅展示文件要求和应处阶段；不以尚未递交的材料反推企业不具备资格。"
        ),
        "items": [
            {
                "item_id": "DOC-001", "category": "投标文件组成", "page_no": 13,
                "clause_ref": "投标人须知前附表 §3.1.1",
                "assertion": "商务标主要包括：投标函及附录、法定代表人身份证明或授权委托书、联合体协议书、投标保证金、已报价工程量清单、项目管理机构、拟分包项目情况表、资格审查资料、其他资料。",
                "materials": ["上述商务标组成文件（按第八章格式编制）"],
                "stage": "决定投标后至投标文件递交前",
                "condition": "联合体协议书仅在文件接受且实际组成联合体的情形适用；本项目不接受联合体。",
                "current_judgement": "文件组成要求；不以企业台账判断满足/不满足。",
                "requirement_refs": [],
            },
            {
                "item_id": "DOC-002", "category": "投标文件组成", "page_no": 13,
                "clause_ref": "投标人须知前附表 §3.1.2",
                "assertion": "技术标主要包括施工组织设计及其他资料（附表）。",
                "materials": ["施工组织设计", "文件要求的其他技术附表"],
                "stage": "决定投标后至投标文件递交前",
                "condition": "按第八章格式和技术暗标要求编制。",
                "current_judgement": "投标文件编制事项；不属于企业资格台账核验。",
                "requirement_refs": [],
            },
            {
                "item_id": "DOC-003", "category": "签署与授权", "page_no": 29,
                "clause_ref": "投标人须知 §3.5.5；前附表 §3.1.1",
                "assertion": "投标文件由委托代理人签字时，提供授权委托书；商务标组成列明法定代表人身份证明或授权委托书。",
                "materials": ["法定代表人身份证明或授权委托书（按实际签署方式和第八章格式）"],
                "stage": "投标文件递交时（条件性）",
                "condition": "仅委托代理人签字/盖章时提供授权委托书；法定代表人直接签署时按文件提供身份证明。",
                "current_judgement": "文件明确的条件性材料；不是“不接受联合体”的证明，也不要求虚构“独立投标声明”。",
                "requirement_refs": [],
            },
            {
                "item_id": "DOC-004", "category": "联合体", "page_no": 28,
                "clause_ref": "投标人须知 §3.1.3；招标公告 §3.1.3",
                "assertion": "招标公告规定不接受联合体投标的，或投标人没有组成联合体的，投标文件不包括联合体协议书。",
                "materials": [],
                "stage": "投标文件编制时",
                "condition": "本项目不接受联合体投标。",
                "current_judgement": "不提交联合体协议书；招标文件未要求“独立投标声明”。",
                "requirement_refs": ["MAT-PJe22ce139ac-TENDER-H-011-draft-r2"],
            },
            {
                "item_id": "DOC-005", "category": "资格审查材料", "page_no": 29,
                "clause_ref": "投标人须知 §3.5.1—§3.5.3",
                "assertion": "投标人基本情况表附营业执照、资质证书、安全生产许可证；企业主要负责人安全生产考核合格证书及分管安全生产副经理任命书；拟派项目经理建造师注册证书和安全生产考核合格证书，均按原件扫描件加盖电子印章。",
                "materials": ["营业执照", "资质证书", "安全生产许可证", "企业主要负责人安全生产考核合格证书（含文件指定的任命书）", "拟派项目经理注册证书及安全生产考核合格证书"],
                "stage": "资格审查/投标文件递交时",
                "condition": "按文件指定表格和电子印章要求提交。",
                "current_judgement": "企业资格事实另行按证据匹配；本行只列明确的随标材料。",
                "requirement_refs": ["MAT-PJe22ce139ac-TENDER-H-022-draft-r2", "MAT-PJe22ce139ac-TENDER-H-001-draft-r2", "MAT-PJe22ce139ac-TENDER-H-002-draft-r2", "MAT-PJe22ce139ac-TENDER-H-003-draft-r2", "MAT-PJe22ce139ac-TENDER-H-004-draft-r2"],
            },
            {
                "item_id": "DOC-006", "category": "实质性响应材料", "page_no": 15,
                "clause_ref": "投标人须知前附表 §3.6",
                "assertion": "实质性响应资料明确列有：营业执照、（代理人签字时的）授权委托书、建设行政部门核发的资质证书及资质核查结果证明、安全生产许可证和企业主要负责人 A 证、项目经理注册执业资格/职称证、保证金缴纳证明、专职安全生产管理人员 C 证；类似业绩材料未勾选。",
                "materials": ["企业法人营业执照", "条件性授权委托书", "资质证书及文件要求的资质核查结果证明", "安全生产许可证与企业主要负责人 A 证", "项目经理注册执业资格证书或专业技术职称证书", "保证金缴纳证明资料", "专职安全生产管理人员 C 证"],
                "stage": "资格审查/投标文件递交时",
                "condition": "类似业绩证明材料在本表为未勾选项，不得在本项目中虚构为必交材料。",
                "current_judgement": "文件明确材料清单；其中企业资格、人员事实仍须以独立企业证据匹配。",
                "requirement_refs": ["MAT-PJe22ce139ac-TENDER-H-007-draft-r2"],
            },
            {
                "item_id": "DOC-007", "category": "投标保证金", "page_no": 14,
                "clause_ref": "投标人须知前附表 §3.4.1；投标人须知 §3.4.1",
                "assertion": "要求提交人民币 50 万元投标保证金；可选银行保函、保证保险、电子投标保函或投标保证金承诺书。投标人在递交投标文件的同时递交，作为投标文件组成部分。",
                "materials": ["按选定方式提交的保证金材料：保函/保单/电子保函及文件要求的基本账户凭证或投标保证金承诺书"],
                "stage": "投标文件递交前/递交同时",
                "condition": "尚未决定投标或尚未到递交阶段时，不要求已有到账凭证；选定方式后再按该方式留存相应材料。",
                "current_judgement": "递交执行事项，不作为当前企业资格缺口或“不投标”判定依据。",
                "requirement_refs": ["MAT-PJe22ce139ac-TENDER-H-013-draft-r2"],
            },
            {
                "item_id": "DOC-008", "category": "信用记录", "page_no": 49,
                "clause_ref": "资格后审必要合格条件标准（信用记录）",
                "assertion": "信用记录由评标专家在评标时对投标企业是否有不良记录上网查询。",
                "materials": [],
                "stage": "评标/资格后审时",
                "condition": "由评标专家网上查询。",
                "current_judgement": "未定位到要求投标人另行上传信用截图、查询报告或证明材料；因此不创建企业补证任务。",
                "requirement_refs": ["MAT-PJe22ce139ac-TENDER-H-010-draft-r2", "MAT-PJe22ce139ac-TENDER-H-025-draft-r2"],
            },
            {
                "item_id": "DOC-009", "category": "文件形式", "page_no": 16,
                "clause_ref": "投标人须知前附表 §3.7.3—§3.7.4；投标人须知 §3.7.5",
                "assertion": "证书证件、业绩证明、投标保证金等证明材料采用原件扫描件并加盖投标单位电子印章；电子加密投标文件一份。中标公告发布五日内，中标人提交 5 份与上传电子文件一致的纸质投标文件。",
                "materials": ["加盖电子印章的原件扫描件", "电子加密投标文件", "中标后纸质投标文件 5 份（中标人）"],
                "stage": "递交时；纸质文件为中标公告发布后五日内",
                "condition": "纸质文件仅中标人后续提交。",
                "current_judgement": "文件制作/递交要求，不是企业资质或人员资格结论。",
                "requirement_refs": [],
            },
            {
                "item_id": "DOC-010", "category": "工程量清单", "page_no": 16,
                "clause_ref": "投标人须知前附表 §3.7.3；第八章“已标价工程量清单”",
                "assertion": "工程量清单扉页由注册造价工程师签字盖专用章；非本单位注册造价师提供造价委托合同；同一造价单位不得同时为同一标段两家及以上投标单位编制报价清单。",
                "materials": ["已标价工程量清单", "造价工程师签章；非本单位人员时的造价委托合同"],
                "stage": "投标文件递交前",
                "condition": "非本单位注册造价师时才提供造价委托合同。",
                "current_judgement": "投标文件制作/报价合规要求；系统不生成报价。",
                "requirement_refs": [],
            },
            {
                "item_id": "DOC-011", "category": "项目经理附件", "page_no": 101,
                "clause_ref": "第八章 投标文件格式·项目管理机构·附 1",
                "assertion": "项目经理简历表应附建造师执业资格证书、注册证书、安全生产考核合格证书、身份证、职称证、养老保险原件扫描件及未担任其他在施建设工程项目项目经理的承诺书；使用项目经理业绩时附合同及竣工验收报告。",
                "materials": ["项目经理资格/注册/B 证", "身份证、职称证、养老保险", "无在施项目经理承诺书", "如使用项目经理业绩：合同及竣工验收报告"],
                "stage": "投标文件递交时",
                "condition": "业绩附件仅在表中填报/使用项目经理业绩时提供。",
                "current_judgement": "同一项目经理的综合资料包；不得从不同人员拼接资格。",
                "requirement_refs": ["MAT-PJe22ce139ac-TENDER-H-003-draft-r2", "MAT-PJe22ce139ac-TENDER-H-004-draft-r2", "MAT-PJe22ce139ac-TENDER-H-005-draft-r2"],
            },
            {
                "item_id": "DOC-012", "category": "项目管理机构附件", "page_no": 102,
                "clause_ref": "第八章 投标文件格式·项目管理机构·附 2",
                "assertion": "项目副经理、技术负责人、合同商务负责人、专职安全生产管理人员等，应附注册资格证书、身份证、职称证、养老保险原件扫描件；专职安全生产管理人员附安全生产考核合格证书；主要业绩附合同协议书。",
                "materials": ["各岗位注册资格证书、身份证、职称证、养老保险", "专职安全生产管理人员 C 证", "如填报主要业绩：合同协议书"],
                "stage": "投标文件递交时",
                "condition": "附件资料包，不据此新增技术负责人/合同商务负责人等独立资格准入条件。",
                "current_judgement": "递交材料清单；企业台账缺记录只能待补/待核实，不推断企业不存在人员。",
                "requirement_refs": ["MAT-PJe22ce139ac-TENDER-H-028-draft-r2", "MAT-PJe22ce139ac-TENDER-H-007-draft-r2"],
            },
            {
                "item_id": "DOC-013", "category": "资格审查表附件", "page_no": 104,
                "clause_ref": "第八章 投标文件格式·资格审查资料·投标人基本情况表",
                "assertion": "投标人基本情况表后附企业法人营业执照、企业资质证书、安全生产许可证等材料的原件扫描件并加盖电子印章。",
                "materials": ["营业执照", "企业资质证书", "安全生产许可证等文件要求材料的原件扫描件及电子印章"],
                "stage": "资格审查/投标文件递交时",
                "condition": "按资格审查资料表和文件形式要求编制。",
                "current_judgement": "与 §3.5.1 相互印证；不因企业台账尚未录入就直接判断企业没有。",
                "requirement_refs": ["MAT-PJe22ce139ac-TENDER-H-022-draft-r2", "MAT-PJe22ce139ac-TENDER-H-001-draft-r2", "MAT-PJe22ce139ac-TENDER-H-002-draft-r2"],
            },
            {
                "item_id": "DOC-014", "category": "递交与回执", "page_no": 30,
                "clause_ref": "投标人须知 §4.1.1—§4.1.4",
                "assertion": "投标人应在截止时间前递交电子投标文件；完成上传后，电子交易平台发出递交回执，递交时间以回执载明的传输完成时间为准。",
                "materials": ["电子投标文件上传回执"],
                "stage": "实际递交完成后",
                "condition": "仅在决定投标并完成上传后产生。",
                "current_judgement": "递交后回执，不是当前“是否投标”阶段应有的企业证据。",
                "requirement_refs": [],
            },
        ],
    },
}


def verified_submission_checklist(project_id: str, material_id: str | None, version: int | None) -> dict[str, Any] | None:
    """返回与当前原始材料版本严格绑定的只读人工复核清单。"""
    if material_id is None or version is None:
        return None
    data = _CHECKLISTS.get((project_id, material_id, version))
    return deepcopy(data) if data else None
