# F017-OCR 文档路由与解析（Document Router）

- **需求来源**：R017 ｜ **状态**：定稿 v1.0（Iteration 2 冻结）
- **关联**：F005（解析链路）、F006（资料库导入）、F003（Material 契约）、`scripts/parse_lab/r017/r017_ocr_engine.py`（演练实现，不入 Git）

## 1. 背景与目标

- **背景**：企业私有资料（资质证书、安许证、岗位证书、合同/验收扫描件）为**图片型 PDF（无文本层）**，无法用 F005 文本解析链路处理；F005 §4.4 原处置为"人工复核"。实测 6 份资质扫描件 + 2 份安许证均为扫描件，需要自动化结构化。
- **目标**：建立 **Document Router**——按文档类型分流（文本/扫描/DOCX），统一输出 Structured JSON（对接 F005 主卡/子卡与 F006 资料库导入）；**OCR 产物必须保留原文 hash 引用 + OCR 置信度 + 页码定位；低置信度 → 人工复核（不静默入库）**；使用**本地开源 OCR**，企业私有资料不出内网。

## 2. 非目标

- 不解析加密固化格式（.gef/.etb），不绕过任何技术保护；
- 不做多模态 LLM 整份喂入（避免 token/算力浪费与数据出境）；
- 不自动判定"资质满足/不满足"（匹配属 F008）；
- 不覆盖原文 raw（OCR 产物与原文 hash 分离）。

## 3. 架构（Document Router）

```text
         Document Router
                │
   ┌────────────┼────────────┐
   ▼            ▼            ▼
Text PDF     Scan PDF      DOCX/DOC
   │            │            │
 Parser        OCR         Parser
(pdftotext)  (tesseract)  (textutil/解包)
   │            │            │
   └────────────┼────────────┘
                ▼
       Structured JSON（hash+置信度+页码）
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
 F005 主卡/子卡  F006 资料库  人工复核队列（低置信度）
```

**路由判定**：

| 输入 | 判定 | 处理 |
|---|---|---|
| PDF | `pdftotext` 输出文本字符数 < 20 → 扫描件 | 扫描 → OCR（tesseract chi_sim）；文本 → Parser |
| DOCX | 解包 `word/document.xml` 提取 | Parser |
| DOC | `textutil` 转文本 | Parser（版式兼容有限，失败走人工复核） |
| 图片（png/jpg/tif） | 直接 | OCR |
| .gef/.etb/.bde | 不支持 | 人工复核（不绕过保护） |

## 4. 数据对象与证据契约

### 4.1 OCR 输出（Structured JSON）

| 字段 | key | 说明 |
|---|---|---|
| 来源 | `source` | 文件路径 |
| 原文哈希 | `sha256` | 原文不可变引用（F003 Material hash） |
| 类型 | `kind` | text_pdf / scanned_pdf / docx / doc / image / unsupported |
| 整页置信度 | `confidence` | 0-1，OCR 页词级平均（过滤 conf=0 噪声），有文本时下限 0.6 |
| 页码 | `pages[].page_no` | 每页页码定位 |
| 页面置信度 | `pages[].confidence` | 每页独立置信度 |
| 是否 OCR | `pages[].ocr` | true/false |
| 文本 | `text` | 结构化文本（OCR 或 Parser 产物） |
| 待复核 | `needs_review` | 整页置信度 < 阈值（默认 0.6）→ true |
| 字段 | `fields[]` | 字段级提取结果（见 4.2） |
| 复核队列 | `review_queue[]` | 低置信度字段清单（不静默入库） |

### 4.2 字段级提取（fields[]）

| 字段 | key | 说明 |
|---|---|---|
| 字段名 | `field` | 如 company_name / cert_no / valid_until |
| 值 | `value` | 提取值；未命中 = `__待补__` |
| 置信度 | `confidence` | 整页置信度 × 字段规则置信度（保守估计） |
| 页码 | `page_no` | 字段所在页 |
| 待复核 | `needs_review` | 字段置信度 < 阈值（默认 0.9）→ true |

**低置信度 → 人工复核**：任何字段 `needs_review=true` 即进入人工复核队列，**不静默入库**；人工确认后回填（留痕，F003/F009 审计）。

### 4.3 阈值（可配置）

- 整页置信度阈值：`PAGE_CONF_THRESHOLD = 0.60`（低于 → 整页人工复核）
- 字段置信度阈值：`CONF_THRESHOLD = 0.90`（低于 → 字段人工复核）

## 5. 输入/输出与证据要求

- 输入：企业私有资料文件（图片型 PDF/文本 PDF/DOCX/图片）。
- 输出：Structured JSON（含 sha256/confidence/page_no/fields/review_queue）。
- 证据要求：每条输出必须可回溯到原文（sha256）与页码；低置信度字段必须有复核记录；OCR 不覆盖原文 raw。

## 6. 业务规则

1. **本地开源优先**：OCR 引擎用 tesseract（Apache-2.0，本机 5.5.2 + chi_sim 中文包）；数据不出内网。
2. **原文 hash 不可变**：OCR 产物挂 sha256 引用，原文 raw 不覆盖、不修改。
3. **置信度门槛**：整页 < 0.6 或字段 < 0.9 → 人工复核，不静默入库。
4. **页码定位**：每个 OCR 词/字段必须带 page_no（多页 PDF 逐页处理）。
5. **空格归一化**：OCR 中文词间空格是 tesseract 噪声（"统一 社会 信用 代码"），字段匹配前做空格归一化，命中后回链原始文本页码。
6. **不绕过保护**：.gef/.etb 加密固化不属 OCR 范围。
7. **中文语言**：默认 `chi_sim`；实测 `chi_sim+eng` 组合会丢失中文（LSTM 语言干扰），不默认加 eng。
8. **TSV 解析**：tesseract 5.5 TSV 的 conf 是浮点字符串（如 `86.608261`），按 float 解析；空白词与 conf=0 噪声不参与置信度计算（但 conf=0 的中文词保留用于文本组装）。

## 7. 失败/空态/异常状态

| 状态 | 处置 |
|---|---|
| 损坏 PDF（XRef 无效，渲染全白） | 整页置信度 0 → 人工复核（引擎已正确识别） |
| OCR 无文本（空白页/图片损坏） | conf=0 → 人工复核 |
| 不支持格式 | unsupported → 人工复核 |
| 字段未命中 | `__待补__` → 人工复核 |
| 低置信度字段 | 进 review_queue，不静默入库 |
| 原文 hash 校验失败 | 阻断，人工检查 |

## 8. API 目标态契约

```text
POST /api/v1/ocr/route           # 提交文件 → 路由 → OCR/Parser → Structured JSON
GET  /api/v1/ocr/jobs/{id}       # 任务状态与结果
GET  /api/v1/ocr/review-queue    # 人工复核队列
POST /api/v1/ocr/review/{id}     # 人工复核确认/修正（留痕）
```

## 9. 权限、数据隔离、审计和合规

- OCR 对象为企业私有资料，permission_scope = enterprise_read 起步；明细 restricted。
- 本地处理优先（数据不出内网）；云 OCR/多模态 LLM 需法务确认后方可启用。
- 所有 OCR/复核动作审计（谁/何时/改了啥/依据）。

## 10. 验收标准与测试计划

- 验收：①路由正确分流 文本/扫描/DOCX；②OCR 输出含 sha256 + 置信度 + 页码；③低置信度字段进人工复核队列（不静默入库）；④.gef/.etb 不解析；⑤本地处理不出内网；⑥字段级提取可回链页码。
- 测试计划（已用真实素材验证，2026-08-26）：资质证书 2.pdf（扫描件，OCR 提取企业名称/信用代码/证书编号/有效期/资质等级，低置信字段进复核队列）；公路招标文件 pdf（文本 → Parser）；投标文件 docx（Parser）；损坏 1.pdf（全白 → 人工复核）。

## 11. 影响范围

- F005（解析链路扩展）、F006（资料库导入）、F003（Material/evidence 扩展 ocr_confidence/page_no）、F008（匹配输入）。

## 12. 回链

- ADR-001（四层架构，OCR 属事实结构化层）；F005 §4.4；F006 §4.4/§6.6。

## 13. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-08-26 | v1.0 | R017 定稿：Document Router 架构、OCR 证据契约（sha256+置信度+页码）、低置信度人工复核、本地开源 tesseract、字段级提取与阈值、实测验证记录（资质扫描件 OCR 成功/损坏 PDF 进复核） |

## 14. 实测验证（2026-08-26，样例脚本不入 Git）

引擎实现：`scripts/parse_lab/r017/r017_ocr_engine.py`（本地，tesseract CLI + pdftotext/pdftoppm/textutil，Python 标准库）。

| 输入 | 路由 | 结果 |
|---|---|---|
| 资质证书 2.pdf（扫描） | OCR | 成功识别：企业名称/统一社会信用代码 911306007006711044/证书编号 0213035404/有效期至 2029-09-11/资质等级（地基基础壹级等）；OCR 误识（"晶"=日、"党级"=壹级）→ 低置信 → 人工复核队列 ✅ |
| 资质证书 1.pdf（XRef 损坏） | OCR | 渲染全白 → conf=0 → 人工复核 ✅ |
| 公路招标文件.pdf（文本） | Parser | text_pdf conf=1.0 ✅ |
| 投标文件.docx | Parser | docx conf=1.0 ✅ |