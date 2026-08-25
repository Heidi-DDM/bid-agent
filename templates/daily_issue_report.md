---
type: daily-issue-report
issue_date: $issue_date
status: $status
item_count: $item_count
urgent_count: $urgent_count
version: $version
generated_at: $generated_at
last_updated_at: $last_updated_at
stable_path: $stable_path
---

# $title

> 日报日期：$issue_date ｜ 可参与公告：**$item_count** 条 ｜ 紧急事项：**$urgent_count** 条
> 最后更新时间：$last_updated_at
> 数据口径：收录**截止时间未过、当天仍可报名/下载/投递**的招标公告（北京时间，2026-08-18 起不限定当天发布）；缺失字段不编造，汇总见文末「待补充与后台提醒」。

$empty_note

$groups

---

## 待补充与后台提醒

### 一、待补充字段（公告原文未载明，需回源补采或人工确认）

$pending_fields

### 二、后台提醒（仅统计数字，不含事件明细）

- 后台待复核事件：$pending_review_count 条（截止时间缺失或待核验，未进本日报）
- 后台迟到事件：$late_count 条（截止时间已过，已按口径保留在后台）
