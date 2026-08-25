# 招投标 Schema 生成审计日志（_schema_generation_log）

> 依据：domain-schema-generator v2（⓪.1→⓪.6 闭环）。本日志记录 schema 从骨架树到通过门禁的全过程，供后续质量追溯。
> 生成日期：2026-08-13 ｜ 生成方式：主 Agent 骨架填空 + 隔离子任务独立审计 + 机械占位符扫描

---

## 一、输入

| 项 | 路径/来源 |
|----|----------|
| 骨架树（唯一真相源） | 《投标智能体-智库底座复用方案_v1.md》§四（招投标 Schema 骨架树细化版 v1，8 大章）+ §五/§六 |
| 开发基线 | 《投标信息收集智能体-PRD_v1.md》§六 数据模型（字段 key/采集模式/门禁） |
| 辅助输入 | 《信息收集智能体建设方案_v1.md》（六环节闭环/合规红线） |
| 机制参考 | 智库《domain-schema-generator-v2.md》（⓪.1→⓪.6）、《心理咨询schema骨架树.md》（结构锚点方法论） |

## 二、生成过程（⓪.1 骨架填空）

- 产出：`schema/schema_draft_v0.md`（582 行，8 大章全叶子节点填充）
- 关键决策：
  1. **无分析层**：删除智库 7 维分析框架/SCQA/报告撰写/作战地图整章（信息搜集 Agent 无分析产出，符合复用方案 §二 边界）
  2. **主卡 20 字段**与 PRD §六 逐项对齐，`company_advantage` 为业务占位（待企业资料导入）
  3. **门禁 G0-G6′** 每道内嵌 `auto_action`（复用方案 §三 复用清单 #6/#10）
  4. **纯事实红线**独立成章（4.4）+ QA 8.3 双重检查
  5. 事实类型 9 类 = 智库 6 类 + 新增 requirement/checklist + 企业私有 advantage（复用方案 §四 4.1）

## 三、独立审计（⓪.2）

- 执行方式：delegate_task 隔离子任务（无生成上下文），主 Agent 模型
- 审计对象：`schema/schema_draft_v0.md`
- 审计基准：复用方案 §四 骨架树逐叶子节点（51 个检查点）

### 审计结果（iteration 0）

```json
{
  "audit_iteration": 0,
  "missing_nodes": [],
  "total_leaf_nodes_checked": 51,
  "total_missing": 0,
  "coverage_rate": "51/51"
}
```

- §一 域识别与路由（5 项）：1.1 六类公告路由+处理分支 / 1.2 属于-不属于+模糊降级 / 1.3 资质 11 行 / 1.4 平台注册表（国家级 2+京津冀 5+第三方 L0 候选）/ 1.5 三态匹配规则 ✅
- §二 招标事实维度（6 维 45 字段）：2.1 十二字段 / 2.2 六字段 / 2.3 九字段 / 2.4 六字段 / 2.5 六字段 / 2.6 六字段，全部带 field_key/类型/必填/提取规则 ✅
- §三 来源可信度（4 项）：3.1 L0-L3 四级 / 3.2 五维评分（25/25/25/15/10）/ 3.3 四档置信度映射 / 3.4 三态质量闸门 ✅
- §四 项目事实卡（4 项）：4.1 九类事实类型 / 4.2 主卡 20 字段与 §六 一致 / 4.3 挂接规则 4 条 / 4.4 红线禁止 4 例+允许 2 例 ✅
- §五 采集与合规（6 项）：5.1 触发式/定时式+切换门槛 / 5.2-5.5 / 5.6 R1-R6 六条 ✅
- §六 Pipeline 门禁（10 项）：G0-G6′ 七道各含 rule+auto_action+fallback / 6.2 状态文件定位 / 6.3 状态文件 schema / 6.5 自动执行算法 ✅
- §七 去重（6 项）：7.1 L1 Python 伪代码 / 7.2 L2 / 7.3 L3 合并表 / 7.4 关联 / 7.5 动作表 / 7.6 置信度+域内特殊规则 4 条（≥3）✅
- §八 QA（6 项）：8.1-8.6 齐全，8.2 含 P0 全检 10 条+月度 100 条，8.6 YAML 模板 ✅
- 额外（4 项）：术语表 12 条（≥10）/ 变更记录 / 活文档声明 / 无分析层混入 ✅

**非缺陷观察**：骨架树 2.2.1 `publish_time` 叶子在 schema 中落在 §2.1（语义一致，合理合并），未计入缺失。

## 四、机械占位符扫描（⓪.3）

- 扫描对象：`schema/schema_draft_v0.md`
- 检测模式：空字符串 / N 占位 / "# 例：" 空 / 待补充|待填写|TODO|TBD|xxx|XXX / 列表空项
- 结果：`{"placeholder_hits": [], "total": 0}`
- 业务占位判定（不计缺陷）："待企业资料导入""待补""待投标专员补清单""后续补入""待合同"——均为方案明确要求保留的业务占位。

## 五、定点修复（⓪.4）

- diff_report.missing_nodes 为空数组 → 无需修复，0 轮修复。

## 六、门禁通过

| 条件 | 结果 |
|------|:--:|
| 条件0：`.pipeline_state.yml` 已初始化且非空 | ✅（schema/.pipeline_state.yml，domain: zhaotoubiao） |
| 条件1：coverage_rate = 100%（51/51） | ✅ |
| 条件2：placeholder_hits.total = 0 | ✅ |

通过后动作：
1. ✅ `schema/schema_draft_v0.md` → `schema/schema.md`
2. ✅ frontmatter 更新：status=active, generated_by=domain-schema-generator-v2, audit_iterations=1
3. ✅ 本日志（_schema_generation_log.md）写入审计轨迹
4. ⚠️ 按复用方案 §七 完全分离原则：**不更新智库 `_index.md`**，招投标域独立演进；项目内索引见 `schema/README.md`
5. ✅ `.pipeline_state.yml` domain 字段（zhaotoubiao）与 schema 一致

## 七、版本记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-08-13 | v0.0.0 | 初版通过审计门禁（coverage 51/51，placeholder 0） |

> 后续版本化规则：schema 首次通过门禁记为 v1.0.0；因新证据触发的补充走 PATCH/MINOR 逻辑；结构性改版（新增章节）才触发全量审计。
