# R004/F004：真实公告采集运行时（manual_trigger）
# 模块职责：
#   registry.py  —— 源注册表（合规状态单一入口，引用 docs/合规数据源清单.md）
#   parsers.py   —— 各源列表页解析器（纯函数，无网络/DB 依赖，可单测）
#   service.py   —— 执行编排：robots 预检 → 限频 → 抓取 → 解析 → 候选落库 / 详情入库
# 红线由 runtime/core/fetcher.py + runtime/core/compliance.py 兜底执行，本模块不绕过。