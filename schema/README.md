# 招投标信息收集智能体 · Schema 索引

> 本目录是招投标智能体的 **L2 操作规范层**（独立于智库，不读写智库目录，见《投标智能体-智库底座复用方案_v1.md》§七）。

## 目录内容

| 文件 | 角色 | 说明 |
|------|------|------|
| `schema.md` | 域操作规范（宪法） | 采集→清洗→事实卡→情报库→**匹配与准入**→推送→校准全规则 + **准入状态机** + **数据治理域（Material 契约/权限矩阵）**，status=active（v0.5.0） |
| `.pipeline_state.yml` | Pipeline 状态机 | G0-G6′ 门禁状态，读取时机=任务开始，写入时机=门禁通过 |
| `tender_skeleton_tree.json` | 骨架树（机器可读） | 由 `scripts/build_skeleton_tree.py` 生成；11 章（含资格与资源核查域、准入状态机域、数据治理域） |
| `schema_draft.md` | 骨架树生成稿 | 主卡/子卡/优势卡/匹配矩阵/项目经理字段/Material 契约字段清单 + 树结构（生成器输出） |
| `_schema_generation_log.md` | 审计轨迹 | generator v2 全流程记录（⓪.1→⓪.6 + v0.4.0 二次全量审计 + v0.5.0 三次全量审计） |

## 状态

- Schema 版本：v0.5.0（2026-08-25 三次全量审计通过，coverage 125/125，placeholder 0；新增数据治理域 Material 契约与权限矩阵，依据 F003/R003）
- Pipeline 状态：`step_01_collect` 起全 MISSING（等待 P0 采集验收）

## 上下游衔接

- 上游：`scripts/build_skeleton_tree.py`（骨架树维护）→ `schema.md`
- 下游：采集执行器 / 事实卡提取 / 情报库 / 推送（按 schema §六 门禁 G0-G6′ 执行）/ 资格资源匹配与准入（§十一、§十二，Iteration 1+ 实现）/ 数据治理（§十三 Material 契约与权限矩阵，Iteration 3 冻结实现）
- 基线：字段 key 以《投标信息收集智能体-PRD_v1.md》§六 为准，变更须同步 PRD §一 锚点；范围变化以 `docs/06-产品决策记录/ADR-001-投标准入边界.md` 为准
