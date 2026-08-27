#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""招投标 Schema 骨架树：数据定义 + 校验 + 生成 + 冒烟测试。"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schema"


GATES = {
    "G0": "采集门禁",
    "G1": "清洗门禁",
    "G2": "事实卡门禁",
    "G3": "情报库门禁",
    "G3.5": "匹配门禁",
    "G4": "推送门禁",
    "G5": "归档门禁",
    "G6′": "校准门禁",
}


def node(node_id: str, name: str, kind: str = "section", children=None, **kw):
    item = {"id": node_id, "name": name, "kind": kind}
    item.update(kw)
    if children:
        item["children"] = children
    return item


def field(node_id, name, key, typ, required=False, source="公告原文", values=None, note="", gate=None):
    item = node(node_id, name, "field", field_key=key, type=typ, required=required, source=source)
    if values:
        item["values"] = values
    if note:
        item["note"] = note
    if gate:
        item["gate"] = gate
    return item


def ref_item(node_id, name, data_source):
    return node(node_id, name, "ref_item", data_source=data_source)


def rule(node_id, name, note="", gate=None):
    item = node(node_id, name, "rule", note=note)
    if gate:
        item["gate"] = gate
    return item


MAIN_CARD_FIELDS = [
    {"name": "卡片ID", "key": "card_id", "type": "string", "required": True, "source": "系统生成"},
    {"name": "项目ID", "key": "project_id", "type": "string", "required": True, "source": "系统生成/聚合"},
    {"name": "项目名称", "key": "project_name", "type": "string", "required": True, "source": "公告原文"},
    {"name": "招标编号", "key": "tender_no", "type": "string", "required": False, "source": "公告原文"},
    {"name": "招标人", "key": "tenderee", "type": "string", "required": True, "source": "公告原文"},
    {"name": "招标代理机构", "key": "agency", "type": "string", "required": False, "source": "公告原文"},
    {"name": "地区", "key": "region", "type": "string", "required": True, "source": "公告原文"},
    {"name": "项目类型", "key": "project_type", "type": "enum", "required": True, "source": "公告原文", "values": ["工程", "货物", "服务"]},
    {"name": "预算金额", "key": "budget_amount", "type": "decimal", "required": False, "source": "公告原文"},
    {"name": "最高投标限价", "key": "ceiling_price", "type": "decimal", "required": False, "source": "公告原文"},
    {"name": "投标保证金金额", "key": "bid_bond_amount", "type": "decimal", "required": False, "source": "公告原文"},
    {"name": "报名截止时间", "key": "deadline_signup", "type": "datetime", "required": False, "source": "公告原文"},
    {"name": "投标截止时间", "key": "deadline_bid", "type": "datetime", "required": True, "source": "公告原文"},
    {"name": "开标时间", "key": "open_date", "type": "datetime", "required": False, "source": "公告原文"},
    {"name": "资格要求", "key": "qualification_requirements", "type": "ref[]", "required": False, "source": "子卡引用"},
    {"name": "评分规则", "key": "scoring_rules", "type": "ref[]", "required": False, "source": "子卡引用"},
    {"name": "公司投标优势", "key": "company_advantage", "type": "ref[]", "required": False, "source": "占位=待企业资料导入"},
    {"name": "来源链接", "key": "source_links", "type": "url[]", "required": True, "source": "公告原文"},
    {"name": "置信度", "key": "confidence", "type": "enum", "required": True, "source": "来源映射", "values": ["confirmed", "high", "medium", "low"]},
    {"name": "状态", "key": "status", "type": "enum", "required": True, "source": "项目状态", "values": ["active", "closed", "awarded"]},
]


SUBCARD_TYPES = [
    {"key": "basic", "name": "基本信息", "note": "主卡摘要之外的补充信息"},
    {"key": "time", "name": "时间线", "note": "报名/答疑/投标/开标/保证金等时间点"},
    {"key": "qualification", "name": "资格要求", "note": "资质/人员/业绩/财务/信用等"},
    {"key": "scoring", "name": "评分规则", "note": "技术/商务/资信权重与废标条款"},
    {"key": "amount", "name": "金额限价", "note": "预算/限价/保证金/履约保证金"},
    {"key": "compliance", "name": "合规约束", "note": "暗标/CA/递交方式/原件备查等"},
    {"key": "requirement", "name": "清单式要求", "note": "资格/废标清单，逐条引用条款"},
    {"key": "checklist", "name": "投标动作清单", "note": "报名/CA/保证金/递交/开标动作"},
    {"key": "advantage", "name": "公司投标优势", "note": "企业私有库数据与招标要求匹配"},
]


ADVANTAGE_FIELDS = [
    {"name": "优势卡ID", "key": "advantage_id", "type": "string", "required": True, "source": "自动生成"},
    {"name": "项目ID", "key": "project_id", "type": "string", "required": True, "source": "回链主卡"},
    {"name": "优势名称", "key": "advantage_name", "type": "string", "required": False, "source": "占位"},
    {"name": "对应招标条款", "key": "tender_clause_ref", "type": "ref[]", "required": False, "source": "回链资格/评分子卡"},
    {"name": "匹配依据", "key": "match_basis", "type": "enum", "required": False, "source": "占位", "values": ["资质", "人员", "业绩", "历史标书", "成本"]},
    {"name": "匹配结果", "key": "match_result", "type": "enum", "required": False, "source": "占位", "values": ["满足", "部分满足", "待核实", "不满足"]},
    {"name": "支撑材料", "key": "evidence_files", "type": "file[]", "required": False, "source": "待提供"},
    {"name": "有效期", "key": "valid_until", "type": "date", "required": False, "source": "占位"},
    {"name": "来源库", "key": "source_library", "type": "enum", "required": False, "source": "占位", "values": ["资质库", "人员库", "业绩库", "历史标书库", "成本库"]},
    {"name": "状态", "key": "status", "type": "enum", "required": True, "source": "占位", "values": ["待企业资料导入", "已启用"]},
    {"name": "备注", "key": "remark", "type": "string", "required": False, "source": "人工补充"},
]


PROJECT_MANAGER_FIELDS = [
    {"name": "经理ID", "key": "manager_id", "type": "string", "required": True, "source": "系统生成/导入", "note": "如 PM-0001"},
    {"name": "姓名/脱敏展示名", "key": "display_name", "type": "string", "required": True, "source": "企业私有库", "note": "列表默认脱敏（如 张**），明细按权限"},
    {"name": "所属组织", "key": "organization", "type": "string", "required": True, "source": "企业私有库"},
    {"name": "专业", "key": "specialty", "type": "string", "required": True, "source": "企业私有库", "note": "建筑工程/市政/公路等"},
    {"name": "注册证书", "key": "reg_cert_type", "type": "string", "required": True, "source": "企业私有库", "note": "一级建造师/二级建造师等"},
    {"name": "注册编号", "key": "reg_cert_no", "type": "string", "required": True, "source": "企业私有库", "note": "唯一，校验格式"},
    {"name": "证书等级", "key": "cert_level", "type": "string", "required": True, "source": "企业私有库", "note": "一级/二级"},
    {"name": "证书有效期", "key": "cert_valid_until", "type": "date", "required": True, "source": "企业私有库", "note": "过期自动 expired"},
    {"name": "继续教育/安全证书状态", "key": "edu_safety_status", "type": "enum", "required": True, "source": "企业私有库", "values": ["valid", "expiring", "expired", "pending"]},
    {"name": "可担任项目类型", "key": "eligible_project_types", "type": "string[]", "required": True, "source": "企业私有库", "note": "与项目类型匹配"},
    {"name": "地区限制", "key": "region_restriction", "type": "string", "required": False, "source": "企业私有库", "note": "如仅限河北省"},
    {"name": "历史业绩", "key": "performance_refs", "type": "ref[]", "required": False, "source": "回链 F006 PerformanceRecord"},
    {"name": "当前在建项目", "key": "active_projects", "type": "ref[]", "required": False, "source": "企业私有库", "note": "项目ID + 预计结束"},
    {"name": "预计可用日期", "key": "expected_available_at", "type": "date", "required": False, "source": "企业私有库"},
    {"name": "可用状态", "key": "availability", "type": "enum", "required": True, "source": "企业私有库", "values": ["available", "occupied", "planning"]},
    {"name": "信用/处罚状态", "key": "credit_penalty_status", "type": "string", "required": False, "source": "企业私有库", "note": "仅限企业合法维护范围（失信/处罚记录，有证据）"},
    {"name": "证据文件引用", "key": "evidence_refs", "type": "ref[]", "required": True, "source": "回链 F003 evidence_file", "note": "注册证书、社保、继续教育证明、业绩证明"},
    {"name": "最后核验时间", "key": "verified_at", "type": "datetime", "required": False, "source": "企业私有库"},
    {"name": "资料责任人", "key": "data_owner", "type": "string", "required": True, "source": "企业私有库"},
    {"name": "经理状态", "key": "status", "type": "enum", "required": True, "source": "企业私有库", "values": ["active", "unavailable", "expired", "pending_verification", "archived"]},
    {"name": "推荐角色", "key": "recommendation_role", "type": "enum", "required": False, "source": "投标专员设置", "values": ["primary", "backup"], "note": "主推荐/备选，留存审计（F007 §6.2）"},
]


MATCH_MATRIX_FIELDS = [
    {"name": "矩阵ID", "key": "matrix_id", "type": "string", "required": True, "source": "系统生成", "note": "关联 project_id + 规则版本"},
    {"name": "要求引用", "key": "tender_clause_ref", "type": "ref[]", "required": True, "source": "回链招标条款（F005 子卡）"},
    {"name": "企业证据引用", "key": "evidence_refs", "type": "ref[]", "required": True, "source": "回链 F006/F007 证据"},
    {"name": "匹配结果", "key": "match_result", "type": "enum", "required": True, "source": "规则匹配", "values": ["satisfied", "partial", "not_satisfied", "unverifiable", "manual_review"], "note": "manual_review 不得视为满足；正式枚举替代旧优势卡占位"},
    {"name": "计分", "key": "score", "type": "decimal", "required": False, "source": "规则计分", "note": "计分项得分；不可计算时为 null"},
    {"name": "满分值", "key": "max_score", "type": "decimal", "required": True, "source": "规则配置", "note": "计分项满分值（F008 配置）"},
    {"name": "缺失项", "key": "missing_items", "type": "ref[]", "required": False, "source": "匹配结果", "note": "待补/待核实清单"},
    {"name": "判定时点", "key": "as_of", "type": "datetime", "required": True, "source": "招标条款/规则配置", "note": "按资格预审、投标截止或明确日期核验"},
    {"name": "判定依据", "key": "match_reason", "type": "object", "required": True, "source": "规则引擎", "note": "表达式、输入、证据、计算结果和人工复核原因"},
]


ADMISSION_FIELDS = [
    {"name": "资格/响应性核查", "key": "qualification_result", "type": "object", "required": True, "source": "硬性要求匹配", "note": "规则化核查，不是法律意见或最终资格审查"},
    {"name": "当前评分结果", "key": "scoring_result", "type": "object", "required": True, "source": "评分规则", "note": "可计算分数、不可计算项和内部质量评审状态；不是评标委员会得分"},
    {"name": "投标准备度", "key": "operational_readiness", "type": "object", "required": True, "source": "动作要求", "note": "按 approval_ready/submission_ready/submitted/opened 阶段判断"},
    {"name": "内部准入结论", "key": "internal_admission_result", "type": "object", "required": True, "source": "内部准入规则", "note": "综合四类结论的可解释结果"},
    {"name": "是否可送人工审批", "key": "internal_admission_eligible", "type": "boolean", "required": True, "source": "内部准入公式", "note": "不代表自动投标或评标满分"},
    {"name": "结果新鲜度", "key": "result_freshness", "type": "enum", "required": True, "source": "版本核验", "values": ["current", "stale"], "note": "澄清、证据变更或过期后必须 stale 并重算"},
    {"name": "阻断/待补/复核项", "key": "decision_items", "type": "object[]", "required": False, "source": "匹配结果", "note": "统一承载 blocked_missing_data、blocked_hard_requirement、not_qualified、manual_review 及责任人/截止时间"},
]


ADMISSION_STATE_FIELDS = [
    {"name": "准入状态", "key": "admission_status", "type": "enum", "required": True, "source": "准入状态机", "values": ["draft", "collecting", "parsed", "matching", "blocked_missing_data", "blocked_hard_requirement", "not_qualified", "qualified_full_score", "pending_bid_approval", "approved_for_bidding", "rejected_by_approver", "archived"], "note": "ADR-001 §2.2 状态机"},
]


WAIVER_FIELDS = [
    {"name": "豁免ID", "key": "waiver_id", "type": "string", "required": True, "source": "系统生成"},
    {"name": "授权人", "key": "authorizer", "type": "string", "required": True, "source": "审批人"},
    {"name": "原因", "key": "reason", "type": "string", "required": True, "source": "豁免申请", "note": "必填"},
    {"name": "证据", "key": "evidence_refs", "type": "ref[]", "required": True, "source": "回链 evidence_file", "note": "在途材料/受理回执等，必填"},
    {"name": "有效期", "key": "valid_until", "type": "date", "required": True, "source": "豁免配置", "note": "必填，过期自动失效"},
    {"name": "审批时间", "key": "approved_at", "type": "datetime", "required": True, "source": "审批记录"},
    {"name": "覆盖的阻断项", "key": "covered_items", "type": "ref[]", "required": True, "source": "豁免申请", "note": "具体豁免哪项"},
]


APPROVAL_FIELDS = [
    {"name": "审批ID", "key": "approval_id", "type": "string", "required": True, "source": "系统生成"},
    {"name": "项目ID", "key": "project_id", "type": "string", "required": True, "source": "回链主卡"},
    {"name": "审批人", "key": "approver", "type": "string", "required": True, "source": "审批人配置", "note": "经营负责人"},
    {"name": "决策", "key": "decision", "type": "enum", "required": True, "source": "人工审批", "values": ["approved", "rejected", "waived"]},
    {"name": "决策时间", "key": "decided_at", "type": "datetime", "required": True, "source": "审批记录"},
    {"name": "意见", "key": "comment", "type": "string", "required": False, "source": "审批记录", "note": "驳回时必填"},
    {"name": "关联准入结果", "key": "admission_result_ref", "type": "ref[]", "required": True, "source": "回链 F008 准入判定"},
]


MATERIAL_FIELDS = [
    {"name": "材料ID", "key": "material_id", "type": "string", "required": True, "source": "系统生成/导入", "note": "主键，如 MAT-PUB-xxx / MAT-PRV-xxx"},
    {"name": "材料类型", "key": "material_type", "type": "enum", "required": True, "source": "F003 契约", "values": ["announcement", "tender_document", "qualification_cert", "performance_record", "personnel_cert", "evidence_file"]},
    {"name": "来源类型", "key": "source_type", "type": "enum", "required": True, "source": "F003 契约", "values": ["official_platform", "agency", "uploaded", "internal", "manual_entry"]},
    {"name": "归属类型", "key": "owner_type", "type": "enum", "required": True, "source": "F003 契约", "values": ["public", "enterprise"], "note": "公开/私有严格分层隔离"},
    {"name": "密级", "key": "classification", "type": "enum", "required": True, "source": "F003 契约", "values": ["public", "internal", "confidential"]},
    {"name": "权限范围", "key": "permission_scope", "type": "enum", "required": True, "source": "F003 契约", "values": ["public_read", "enterprise_read", "restricted", "approver_only"]},
    {"name": "原文哈希", "key": "content_hash", "type": "string", "required": True, "source": "SHA-256 计算", "note": "防篡改；不一致 = 篡改风险，阻断使用"},
    {"name": "版本", "key": "version", "type": "int", "required": True, "source": "系统生成", "note": "从 1 递增，不覆盖历史"},
    {"name": "导入时间", "key": "imported_at", "type": "datetime", "required": True, "source": "系统生成"},
    {"name": "有效期", "key": "valid_until", "type": "date", "required": False, "source": "F003 契约", "note": "证书/证照必填；过期标 expired，不计入满分"},
    {"name": "解析状态", "key": "parse_status", "type": "enum", "required": True, "source": "解析流程", "values": ["pending", "parsed", "partial", "failed", "manual_review"], "note": "failed/partial 不进入匹配，人工复核"},
    {"name": "证据文件引用", "key": "evidence_refs", "type": "ref[]", "required": False, "source": "回链 evidence_file"},
    {"name": "数据责任人", "key": "data_owner", "type": "string", "required": True, "source": "F003 契约", "note": "谁负责维护/核验"},
    {"name": "最后核验时间", "key": "verified_at", "type": "datetime", "required": False, "source": "F003 契约"},
    {"name": "状态", "key": "status", "type": "enum", "required": True, "source": "F003 契约", "values": ["active", "expired", "archived", "invalid"]},
]


TREE = [
    node("1", "域识别与路由", children=[
        node("1.1", "公告类型路由", children=[
            field("1.1.1", "公告类型", "announcement_type", "enum", True, values=["招标公告", "资格预审公告", "变更/澄清公告", "中标候选人公示", "中标结果公示", "流标/终止公告"], note="公告类型决定后续流水线分支", gate="G0"),
        ]),
        node("1.2", "域边界", children=[
            field("1.2.1", "项目类型", "project_type", "enum", True, values=["工程", "货物", "服务"], note="非招标采购（询价/比选/竞争性谈判/单一来源）剔除", gate="G0"),
            node("1.2.2", "企业资质边界", children=[
                ref_item("1.2.2.1", "建筑工程施工总承包特级", "企业资质清单"),
                ref_item("1.2.2.2", "建筑工程设计甲级", "企业资质清单"),
                ref_item("1.2.2.3", "人防工程设计甲级", "企业资质清单"),
                ref_item("1.2.2.4", "质量管理体系认证证书（ISO 9001）", "企业资质清单"),
                ref_item("1.2.2.5", "环境管理体系认证证书（ISO 14001）", "企业资质清单"),
                ref_item("1.2.2.6", "职业健康安全管理体系认证证书", "企业资质清单"),
                ref_item("1.2.2.7", "信息安全管理体系认证证书（ISO 27001）", "企业资质清单"),
                ref_item("1.2.2.8", "知识产权管理体系认证证书", "企业资质清单"),
                ref_item("1.2.2.9", "社会责任管理体系认证证书（2024-08-29~2027-08-29）", "企业资质清单"),
                ref_item("1.2.2.10", "安全生产许可证", "企业资质清单"),
            ]),
            rule("1.2.3", "资质匹配规则", gate="G0", note="公告资格要求与集团资质逐项匹配；满足/部分满足/剔除"),
        ]),
        node("1.3", "平台注册表", children=[
            node("1.3.1", "国家级 L1", children=[
                ref_item("1.3.1.1", "全国公共资源交易平台 ggzy.gov.cn", "平台注册表"),
                ref_item("1.3.1.2", "中国招标投标公共服务平台 cebpubservice.com", "平台注册表"),
                ref_item("1.3.1.3", "中国政府采购网 ccgp.gov.cn（京津冀走分站）", "平台注册表"),
            ]),
            node("1.3.2", "北京 L1", children=[
                ref_item("1.3.2.1", "北京公共资源交易服务平台 ggzyfw.beijing.gov.cn ★", "平台注册表"),
                ref_item("1.3.2.2", "北京市政府采购网 ccgp-beijing.gov.cn", "平台注册表"),
                ref_item("1.3.2.3", "北京建设工程交易系统 zhjy.bcactc.com", "平台注册表"),
            ]),
            node("1.3.3", "天津 L1", children=[
                ref_item("1.3.3.1", "天津市公共资源交易平台 ggzy.zwfwb.tj.gov.cn ★", "平台注册表"),
                ref_item("1.3.3.2", "天津市政府采购网 ccgp-tianjin.gov.cn", "平台注册表"),
            ]),
            node("1.3.4", "河北 L1", children=[
                ref_item("1.3.4.1", "河北省公共资源交易服务平台 szj.hebei.gov.cn/hbggfwpt/ ★（旧域名已注销）", "平台注册表"),
                ref_item("1.3.4.2", "河北省政府采购网 ccgp-hebei.gov.cn ★", "平台注册表"),
                ref_item("1.3.4.3", "惠招标（河北交投 ebidding.hebtig.com）", "平台注册表"),
                ref_item("1.3.4.4", "雄安新区公共资源交易服务平台", "平台注册表"),
            ]),
            node("1.3.5", "河北 11 市 L2（逐个合规评估）", children=[
                ref_item("1.3.5.1", "石家庄 sjzsggzyjyzx.org.cn", "平台注册表"),
                ref_item("1.3.5.2", "唐山 ggzyjy.xzspj.tangshan.gov.cn", "平台注册表"),
                ref_item("1.3.5.3", "邯郸 ggzy.hd.gov.cn", "平台注册表"),
                ref_item("1.3.5.4", "秦皇岛 qhdggzy.cn", "平台注册表"),
                ref_item("1.3.5.5", "承德 szj.chengde.gov.cn/cdsggzy/", "平台注册表"),
                ref_item("1.3.5.6", "衡水 hsggzy.hengshui.gov.cn", "平台注册表"),
                ref_item("1.3.5.7", "沧州 xzsp.cangzhou.gov.cn", "平台注册表"),
                ref_item("1.3.5.8", "邢台 60.6.198.121:8888（IP直连）", "平台注册表"),
                ref_item("1.3.5.9", "保定/廊坊（挂省级 inforc=1306/1310）", "平台注册表"),
            ]),
            node("1.3.6", "第三方 L0（待合同）", children=[
                ref_item("1.3.6.1", "千里马 qianlima.com", "平台注册表"),
                ref_item("1.3.6.2", "剑鱼标讯 jianyu360.com", "平台注册表"),
                ref_item("1.3.6.3", "采招网 bidcenter.com.cn（≠比地）", "平台注册表"),
                ref_item("1.3.6.4", "比地招标 bidizhaobiao.cn", "平台注册表"),
            ]),
            rule("1.3.7", "圈定标准与非公告源", note="属地半径+战略导向；住建厅/四库一平台/采购与招标网/张家口保函平台/政采地市子站 非公告源剔除；公告采集免费免登录（招标投标法16条），CA 锁仅下载文件/投标环节需要；L2 各地市全部纳入采集清单（2026-08-18 用户决策，逐个过合规评估）；千里马/剑鱼免费资源纳入日常巡检，付费 API 待合同不阻塞"),
        ]),
    ]),
    node("2", "招标事实维度", children=[
        node("2.1", "项目基本信息", children=[
            field("2.1.1", "项目名称", "project_name", "string", True),
            field("2.1.2", "招标编号", "tender_no", "string", False),
            field("2.1.3", "招标人", "tenderee", "string", True),
            field("2.1.4", "招标代理机构", "agency", "string", False),
            field("2.1.5", "地区", "region", "string", True),
            field("2.1.6", "行业专业", "industry", "string", False),
            field("2.1.7", "招标方式", "procurement_method", "enum", False, values=["公开招标", "邀请招标"]),
            field("2.1.8", "来源平台", "source_platform", "string", True),
            field("2.1.9", "来源链接", "source_url", "url", True),
            field("2.1.10", "发布时间", "publish_time", "datetime", False),
        ]),
        node("2.2", "时间线", children=[
            field("2.2.1", "报名/文件获取截止时间", "deadline_signup", "datetime", False),
            field("2.2.2", "答疑/澄清截止时间", "deadline_clarify", "datetime", False),
            field("2.2.3", "投标截止时间", "deadline_bid", "datetime", True),
            field("2.2.4", "开标时间与地点", "open_date", "datetime", False),
            field("2.2.5", "保证金到账截止时间", "deadline_bond", "datetime", False),
            field("2.2.6", "中标公示时间", "award_time", "datetime", False),
        ]),
        node("2.3", "资格要求", children=[
            field("2.3.1", "资质要求", "qualification", "string", False),
            field("2.3.2", "人员要求", "personnel", "string", False),
            field("2.3.3", "业绩要求", "performance", "string", False),
            field("2.3.4", "财务要求", "finance", "string", False),
            field("2.3.5", "信用要求", "credit", "string", False),
            field("2.3.6", "安全生产许可证", "safety_license", "string", False),
            field("2.3.7", "联合体要求", "joint_venture", "string", False),
            field("2.3.8", "其他资格", "other_qualification", "string", False),
            field("2.3.9", "审查方式", "review_method", "enum", False, values=["资格预审", "资格后审"]),
        ]),
        node("2.4", "评分规则", children=[
            field("2.4.1", "评审办法", "review_standard", "enum", False, values=["综合评估法", "经评审最低价法", "其他"]),
            field("2.4.2", "商务评分", "business_scoring", "string", False),
            field("2.4.3", "技术评分", "technical_scoring", "string", False),
            field("2.4.4", "资信评分", "credit_scoring", "string", False),
            field("2.4.5", "废标条款", "disqualification_clauses", "string[]", False),
            field("2.4.6", "澄清/补正规则", "clarification_rules", "string", False),
        ]),
        node("2.5", "金额与限价", children=[
            field("2.5.1", "预算金额", "budget_amount", "decimal", False),
            field("2.5.2", "最高投标限价", "ceiling_price", "decimal", False),
            field("2.5.3", "投标保证金", "bid_bond_amount", "decimal", False),
            field("2.5.4", "履约/质量保证金", "performance_bond", "decimal", False),
            field("2.5.5", "币种与金额单位", "currency", "string", False),
            field("2.5.6", "计价方式", "pricing_method", "string", False),
        ]),
        node("2.6", "合规约束", children=[
            field("2.6.1", "暗标要求", "dark_bid", "string", False),
            field("2.6.2", "CA/电子签章", "ca_requirement", "string", False),
            field("2.6.3", "递交方式", "submission_method", "string", False),
            field("2.6.4", "授权委托书", "authorization_requirement", "string", False),
            field("2.6.5", "踏勘/标前会", "site_visit", "string", False),
            field("2.6.6", "其他合规条款", "other_compliance", "string", False),
        ]),
    ]),
    node("3", "来源可信度与质量闸门", children=[
        field("3.1", "数据源分级", "source_level", "enum", True, values=["L0", "L1", "L2", "L3"], note="L3禁止", gate="G0"),
        node("3.2", "来源评分五维", children=[
            field("3.2.1", "官方性", "officialness", "enum", False, values=["官方发布", "转载"]),
            field("3.2.2", "时效性", "timeliness", "string", False),
            field("3.2.3", "完整性", "completeness", "string", False),
            field("3.2.4", "一致性", "consistency", "string", False),
            field("3.2.5", "可追溯性", "traceability", "string", False),
        ]),
        field("3.3", "置信度", "confidence", "enum", True, values=["confirmed", "high", "medium", "low"], note="官方=confirmed;第三方=high;转载=medium;low不采用"),
        field("3.4", "质量闸门", "quality_gate", "enum", True, values=["通过", "人工复核", "驳回"], gate="G2"),
    ]),
    node("4", "项目事实卡规范", children=[
        node("4.1", "事实类型", children=[node(f"4.1.{i+1}", s["name"], "ref_item", data_source=s["note"]) for i, s in enumerate(SUBCARD_TYPES)]),
        rule("4.2", "项目主卡字段", note="见 MAIN_CARD_FIELDS 生成表"),
        rule("4.3", "子卡挂接规则", gate="G3", note="一项目一主卡；子卡回链卡片ID；advantage 仅引用企业私有库"),
        rule("4.4", "纯事实红线", gate="G2", note="只记录公告原文，禁止分析性断语"),
    ]),
    node("5", "采集与合规", children=[
        rule("5.1", "采集模式", note="测试期触发式（手动/单条URL）；正式期定时式；限频均生效"),
        rule("5.2", "robots", note="遵守 robots.txt，禁止路径不采集"),
        rule("5.3", "UA透明", note="标识性 UA，不伪装"),
        rule("5.4", "个人信息", note="最小化、内部使用、30 天后脱敏"),
        rule("5.5", "数据源分级", note="L0-L3，L3 禁止"),
        rule("5.6", "合规红线", note="R1-R6：不绕过技术保护/不采非公开/不干扰平台/个人信息最小化/仅内部用途/可溯源"),
    ]),
    node("6", "Pipeline 状态机 + 门禁", children=[
        rule(f"6.{i+1}", f"{g} {name}", note=g, gate=g) for i, (g, name) in enumerate([(g, n) for g, n in GATES.items() if g != "G3.5"])
    ] + [
        rule("6.8", "G3.5 匹配门禁", note="准入链路新增门禁（F008 §6.4 定稿命名）：G3 情报库通过后，对资格/资源逐项匹配与满分判定；通过 → qualified_full_score → pending_bid_approval；缺失/不满足 → 阻断；不阻断公开情报推送（G4 独立运行）", gate="G3.5"),
    ]),
    node("7", "去重规则", children=[
        rule("7.1", "L1 精确去重", note="URL/公告ID/招标编号一致"),
        rule("7.2", "L2 近似去重", note="项目名+招标人+时间+地点相似度"),
        rule("7.3", "L3 合并去重", note="多平台合并主卡+多源引用"),
        rule("7.4", "变更/澄清关联", note="按项目ID挂接，不覆盖历史版本"),
    ]),
    node("8", "QA 标准", children=[
        rule("8.1", "逐字段核对", note="事实卡 vs 原文段落"),
        rule("8.2", "抽样规则", note="P0 全检 10 条；常态化月度 ≥100 条"),
        rule("8.3", "红线检查", note="无分析断语"),
        rule("8.4", "溯源检查", note="每条含可访问原文链接"),
        rule("8.5", "缺失检查", note="缺失字段标待补，不硬造"),
    ]),
    node("9", "资格与资源核查域", children=[
        node("9.1", "企业资料匹配", children=[
            field(f"9.1.{i+1}", f["name"], f["key"], f["type"], f["required"], source=f["source"], note=f.get("note", "")) for i, f in enumerate(MATCH_MATRIX_FIELDS)
        ]),
        node("9.2", "三类要求", children=[
            rule("9.2.1", "硬性要求 hard_requirement", note="资格/资质/人员/业绩/信用/联合体/否决条款；须配置 failure_effect，无法核验进入待补而非明确不满足（F008 §4.1）"),
            rule("9.2.2", "计分要求 scored_requirement", note="商务/技术/资信/报价；每项配置公式、输入、满分、证据、去重与主观内部质量评审边界（F008 §4.2）"),
            rule("9.2.3", "动作要求 action_requirement", note="报名/CA/保证金/递交/开标按 approval_ready/submission_ready/submitted/opened 阶段核查（F008 §4.3）"),
        ]),
        node("9.3", "项目经理匹配", children=[
            field(f"9.3.{i+1}", f["name"], f["key"], f["type"], f["required"], source=f["source"], values=f.get("values"), note=f.get("note", "")) for i, f in enumerate(PROJECT_MANAGER_FIELDS)
        ] + [
            rule("9.3.22", "硬条件验证", note="逐项验证：专业匹配/注册证书类型与等级/有效期（含继续教育与安全证书）/社保在岗/业绩满足/在建冲突/可用状态/信用处罚；任一不满足或无法核验 → 不计入满分可投标（F007 §6.1）"),
        ]),
        node("9.4", "规则版本", children=[
            field("9.4.1", "规则集ID", "rule_set_id", "string", True, source="系统生成"),
            field("9.4.2", "规则版本", "rule_version", "string", True, source="规则配置"),
            field("9.4.3", "生效日期", "effective_from", "date", True, source="规则配置"),
            field("9.4.4", "创建人", "created_by", "string", True, source="规则配置"),
        ]),
        node("9.5", "四类结论与内部满分准入", children=[
            field(f"9.5.{i+1}", f["name"], f["key"], f["type"], f["required"], source=f["source"], values=f.get("values"), note=f.get("note", "")) for i, f in enumerate(ADMISSION_FIELDS)
        ]),
    ]),
    node("10", "准入状态机域", children=[
        node("10.1", "准入状态", children=[
            field(f"10.1.{i+1}", f["name"], f["key"], f["type"], f["required"], source=f["source"], values=f["values"], note=f.get("note", "")) for i, f in enumerate(ADMISSION_STATE_FIELDS)
        ]),
        rule("10.2", "状态迁移规则", note="draft→collecting→parsed→matching→blocked_missing_data/blocked_hard_requirement/not_qualified→qualified_full_score→pending_bid_approval→approved_for_bidding/rejected_by_approver→archived；qualified_full_score 仅指内部准入政策满足，非评标委员会得分；仅进入 pending_bid_approval，不自动投标"),
        node("10.3", "人工豁免", children=[
            field(f"10.3.{i+1}", f["name"], f["key"], f["type"], f["required"], source=f["source"], note=f.get("note", "")) for i, f in enumerate(WAIVER_FIELDS)
        ]),
        node("10.4", "审批记录", children=[
            field(f"10.4.{i+1}", f["name"], f["key"], f["type"], f["required"], source=f["source"], values=f.get("values"), note=f.get("note", "")) for i, f in enumerate(APPROVAL_FIELDS)
        ] + [
            rule("10.4.8", "审计要求", note="全部动作留痕：谁、何时、依据什么、结论；审计记录不可删改（F009 §6.4/§9）"),
        ]),
    ]),
    node("11", "数据治理域", children=[
        node("11.1", "Material 契约字段", children=[
            field(f"11.1.{i+1}", f["name"], f["key"], f["type"], f["required"], source=f["source"], values=f.get("values"), note=f.get("note", "")) for i, f in enumerate(MATERIAL_FIELDS)
        ]),
        rule("11.2", "权限矩阵", note="公开原文/事实卡：public_read+enterprise_read+restricted+approver_only；企业资质/业绩：enterprise_read 起；项目经理个人信息：脱敏展示，明细仅审批人；审批/豁免/审计：仅 approver_only（F003 §6.2，Iteration 3 冻结目标态）"),
        rule("11.3", "隔离与不可变约束", note="公开/私有严格分层存储，禁止混存；raw 原文不可覆盖，每次导入 version+1；公告变更/澄清按 project_id 挂接；原文哈希不一致 = 篡改风险，阻断使用"),
        rule("11.4", "缺失/过期处置", note="企业证书/人员资质/业绩必须有：有效期、证据文件、数据责任人；缺失字段一律 pending_verification/待补/待核实，禁止编造；过期数据标 expired，不得用于满分判定"),
    ]),
]


REQUIRED_MAIN_CARD_KEYS = {
    "card_id", "project_id", "project_name", "tenderee", "region",
    "project_type", "deadline_bid", "source_links", "confidence", "status",
}


def validate_tree(tree) -> list[str]:
    errors: list[str] = []
    ids: dict[str, str] = {}

    def walk(nodes):
        for n in nodes:
            nid = n.get("id")
            if not nid:
                errors.append("节点缺少 id")
            elif nid in ids:
                errors.append(f"重复 id：{nid}")
            else:
                ids[nid] = n.get("name", "")

            if not n.get("name"):
                errors.append(f"{nid} 缺少 name")

            kind = n.get("kind")
            if kind == "field":
                for k in ("field_key", "type", "source"):
                    if not n.get(k):
                        errors.append(f"{nid} 缺少 {k}")
            if "gate" in n and n["gate"] not in GATES:
                errors.append(f"{nid} 引用了未知门禁 {n.get('gate')}")

            walk(n.get("children", []))

    walk(tree)

    keys = [f["key"] for f in MAIN_CARD_FIELDS]
    if len(keys) != len(set(keys)):
        errors.append("项目主卡字段 key 重复")
    if not REQUIRED_MAIN_CARD_KEYS.issubset(set(keys)):
        errors.append("项目主卡缺少必填字段")
    if not any(f["key"] == "company_advantage" for f in MAIN_CARD_FIELDS):
        errors.append("项目主卡缺少 公司投标优势 占位字段")

    sub_keys = [s["key"] for s in SUBCARD_TYPES]
    if len(sub_keys) != len(set(sub_keys)):
        errors.append("子卡类型 key 重复")
    if "advantage" not in sub_keys:
        errors.append("子卡类型缺少 advantage")

    return errors


def to_markdown(errors: list[str]) -> str:
    lines = [
        "# 招投标 Schema 骨架树（生成稿）",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 树节点校验：{'通过' if not errors else '存在错误'}",
        "",
        "## 一、项目主卡字段清单",
        "",
        "| 中文字段 | 内部字段 key | 类型 | 必填 | 来源 |",
        "|---|---|:--:|:--:|---|",
    ]
    for f in MAIN_CARD_FIELDS:
        lines.append(f"| {f['name']} | `{f['key']}` | {f['type']} | {'是' if f['required'] else '否'} | {f['source']} |")

    lines += ["", "## 二、子卡类型清单", "", "| 类型 | key | 说明 |", "|---|---|---|"]
    for s in SUBCARD_TYPES:
        lines.append(f"| {s['name']} | `{s['key']}` | {s['note']} |")

    lines += ["", "## 三、公司投标优势子卡字段（占位）", "", "| 中文字段 | key | 类型 | 必填 | 来源 |", "|---|---|:--:|:--:|---|"]
    for f in ADVANTAGE_FIELDS:
        lines.append(f"| {f['name']} | `{f['key']}` | {f['type']} | {'是' if f['required'] else '否'} | {f['source']} |")
    lines.append("")
    lines.append("> 占位规则：企业私有库资料导入前，全部置为“待企业资料导入”，禁止用公开公告编造内部优势。")

    lines += ["", "## 四、骨架树结构", ""]

    def walk(nodes, depth=0):
        for n in nodes:
            prefix = "  " * depth + "- "
            if n["kind"] == "field":
                lines.append(f"{prefix}{n['id']} {n['name']}（`{n['field_key']}` / {n['type']} / {'必填' if n.get('required') else '选填'}）")
            else:
                lines.append(f"{prefix}{n['id']} {n['name']}")
            walk(n.get("children", []), depth + 1)

    walk(TREE)
    lines.append("")
    return "\n".join(lines)


def fill_main_card(sample: dict) -> dict:
    card: dict = {}
    for f in MAIN_CARD_FIELDS:
        key = f["key"]
        if key in sample:
            card[key] = sample[key]
        elif key == "card_id":
            card[key] = "T-CARD-0001"
        elif key == "project_id":
            card[key] = "T-PROJ-0001"
        elif key == "company_advantage":
            card[key] = [{"status": "待企业资料导入"}]
        else:
            card[key] = "待补"
    return card


def smoke_test() -> tuple[bool, list[str]]:
    sample = {
        "source_level": "L1",
        "project_name": "某市综合业务楼工程施工招标公告（测试样本）",
        "tender_no": "TEST-2026-001",
        "tenderee": "某市城市建设投资有限公司（测试样本）",
        "agency": "某招标代理有限公司（测试样本）",
        "region": "河北省·石家庄市",
        "project_type": "工程",
        "budget_amount": "1200万元",
        "ceiling_price": "1150万元",
        "bid_bond_amount": "20万元",
        "deadline_signup": "2026-08-20 17:00",
        "deadline_bid": "2026-08-25 09:30",
        "open_date": "2026-08-25 09:30",
        "qualification_requirements": ["建筑工程施工总承包三级及以上"],
        "scoring_rules": ["综合评估法"],
        "source_links": ["https://example.test/tender/TEST-2026-001"],
        "confidence": "confirmed",
        "status": "active",
    }

    card = fill_main_card(sample)
    missing = [
        f["name"]
        for f in MAIN_CARD_FIELDS
        if f["required"] and (card.get(f["key"]) in (None, "", "待补"))
    ]

    gates = {
        "G0": sample.get("source_level") in ("L0", "L1", "L2"),
        "G1": True,  # 样例无重复公告
        "G2": not missing and bool(card.get("source_links")),
        "G3": bool(card.get("qualification_requirements")) and bool(card.get("company_advantage")),
        "G4": card.get("confidence") in ("confirmed", "high") and card.get("status") == "active",
    }

    lines = ["", "== 样例公告冒烟测试 =="]
    for f in MAIN_CARD_FIELDS:
        lines.append(f"- {f['name']}: {card.get(f['key'])}")
    lines.append("")
    for gate, ok in gates.items():
        lines.append(f"- {gate} {GATES[gate]}: {'通过' if ok else '未通过'}")

    return all(gates.values()) and not missing, lines


def main() -> int:
    errors = validate_tree(TREE)
    if errors:
        print("骨架树校验失败：")
        for e in errors:
            print(" -", e)
        return 1

    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    (SCHEMA_DIR / "tender_skeleton_tree.json").write_text(
        json.dumps(TREE, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (SCHEMA_DIR / "schema_draft.md").write_text(to_markdown(errors), encoding="utf-8")

    print("骨架树校验通过。")
    print(f"已生成：{SCHEMA_DIR / 'tender_skeleton_tree.json'}")
    print(f"已生成：{SCHEMA_DIR / 'schema_draft.md'}")

    ok, lines = smoke_test()
    print("\n".join(lines))
    if not ok:
        print("冒烟测试未通过。")
        return 1

    print("\n冒烟测试通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
