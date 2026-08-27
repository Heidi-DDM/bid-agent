#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台去重（任务书 §4.5）与主来源优先级。

指纹组合：
  normalized_project_name + announcement_type + publisher_or_tenderer + publish_date + normalized_content_hash
规则：
- 去重只发生在"进入日报"的筛选层；原始来源记录一律保留（source_links[] 只增不删）
- 同 dedup_key 的事件组内选一个主来源 primary_source，其余为备用来源
- 主来源优先级（配置化）：法定官方平台 > 招标人/代理官方平台 > 政府公共资源平台 > 权威聚合平台 > 其他转载平台
- 各平台发布时间差异保留记录
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from . import timeutil as tu

_FULL_TO_HALF = str.maketrans(
    "０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ：；，。！？（）【】《》",
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ:;,。!?()[]<>",
)


def normalize_name(name: str | None) -> str:
    """项目名归一化：全角→半角、去空白、统一括号、去标点。用于指纹比较。"""
    if not name:
        return ""
    s = str(name).translate(_FULL_TO_HALF)
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[（(]\s*[)）]", "", s)
    s = re.sub(r"[，。、；：！？\-—_·|/\\]", "", s)
    return s


def normalize_publisher(publisher: str | None) -> str:
    """招标人/发布主体归一化：去公司后缀差异（有限公司/股份有限公司/（集团））。"""
    if not publisher:
        return ""
    s = normalize_name(publisher)
    s = re.sub(r"(股份有限公司|有限责任公司|有限公司|公司|集团)$", "", s)
    return s


def content_hash(content: str | None, length: int = 16) -> str:
    """正文归一化哈希：归一化（去空白）+ sha1 前 N 位。"""
    if not content:
        return ""
    norm = re.sub(r"[\s\u3000]+", "", str(content))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:length]


def dedup_key(project_name: str | None, announcement_type: str | None,
              publisher: str | None, publish_dt, content: str | None,
              content_hash_length: int = 16) -> str:
    """公告指纹（§4.5）。publish_dt 取北京时间日期 YYYY-MM-DD。"""
    pd = ""
    if publish_dt is not None:
        pd = tu.to_shanghai(publish_dt).strftime("%Y-%m-%d")
    parts = [
        normalize_name(project_name),
        (announcement_type or "").strip(),
        normalize_publisher(publisher),
        pd,
        content_hash(content, content_hash_length),
    ]
    return "|".join(parts)


def source_priority_level(platform_name: str, url: str = "", priority_cfg: list | None = None) -> str:
    """判定来源属于哪一级优先级（配置化关键词匹配）。未命中 → 其他转载平台。"""
    hay = f"{platform_name or ''} {url or ''}".lower()
    for lvl in priority_cfg or []:
        for kw in lvl.get("keywords", []):
            if kw.lower() in hay:
                return lvl["level"]
    return "其他转载平台"


def _lvl_index(level: str, priority_cfg: list) -> int:
    for i, lvl in enumerate(priority_cfg):
        if lvl["level"] == level:
            return i
    return len(priority_cfg)


def pick_primary_source(events: list, priority_cfg: list) -> dict:
    """同 dedup_key 事件组内选主来源。返回 {primary_event, backup_links[]}。

    主来源选择：先比优先级级别，再比官方性（source_level L0<L1<L2），再比发布时间最早（一手发布）。
    备用来源记录每个平台的名称、原文地址、发布时间（差异保留）。
    """
    if not events:
        return {"primary": None, "backups": []}
    ordered = sorted(
        events,
        key=lambda e: (
            _lvl_index(source_priority_level(e.source_platform, e.primary_source_url, priority_cfg), priority_cfg),
            _lvl_rank(e),                                   # source_level 越高越优先
            tu.parse_dt(e.publish_time) is None,            # 有发布时间的优先
            tu.parse_dt(e.publish_time) or tu.parse_dt("1970-01-01"),  # 一手（更早）优先
            e.id,
        ),
    )
    primary = ordered[0]
    backups = []
    for e in ordered[1:]:
        backups.append({
            "source_id": e.id,
            "source_name": e.source_platform or e.primary_source_id,
            "url": e.primary_source_url,
            "publish_time": e.publish_time,
        })
    return {"primary": primary, "backups": backups}


def _lvl_rank(e) -> int:
    """source_level 官方性排序：L0=3（授权 API 视为准官方）、L1=2、L2=1、无=0。"""
    lv = getattr(e, "source_level", "") or ""
    return {"L0": 3, "L1": 2, "L2": 1}.get(lv, 0)


def dedupe_for_daily(events: list, priority_cfg: list) -> list[dict]:
    """日报维度去重：按 dedup_key 分组，每组保留主来源事件，返回 [{primary, backups}]。

    注意：
    - dedup_key 含 announcement_type——同项目当天"招标公告"与"澄清公告"指纹不同，
      不会被合并（§4.4 旧项目新事件要求，与 schema §7.6 域内规则 #2 一致）。
    - content_hash 为空的事件**保守不合并**（附事件 id 唯一后缀）：宁可日报多显示，
      不可误合并内容无法确认相同的公告（历史数据缺正文快照时的兜底，§10.5）。
    """
    groups: dict[str, list] = {}
    for e in events:
        if e.content_hash:
            key = e.dedup_key or dedup_key(e.project_name, e.announcement_type, e.tenderee,
                                           tu.parse_dt(e.publish_time), e.title + e.content_hash)
        else:
            key = f"__nohash__{e.id}"   # 空哈希：强制独立，不参与跨平台合并
        groups.setdefault(key, []).append(e)
    out = []
    for key, grp in groups.items():
        res = pick_primary_source(grp, priority_cfg)
        out.append({"dedup_key": key, "primary": res["primary"], "backups": res["backups"],
                    "group_size": len(grp)})
    return out
