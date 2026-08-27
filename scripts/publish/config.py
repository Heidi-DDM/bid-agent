#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置加载：config/publish_config.yml（任务书 §十二）。"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]          # 项目根
CONFIG_PATH = ROOT / "config" / "publish_config.yml"


def load_config(path: Path | str | None = None) -> dict:
    """加载发布层配置。配置缺失时给出显式错误，不静默回退。"""
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"发布层配置不存在: {p}（应含 §十二 全部配置项）")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _validate(cfg, p)
    return cfg


def _validate(cfg: dict, p: Path) -> None:
    required = ["timezone", "daily_issue", "delivery", "source_report"]
    for k in required:
        if k not in cfg:
            raise ValueError(f"配置缺少必需项 {k}: {p}")
    tz = cfg.get("timezone")
    if tz != "Asia/Shanghai":
        raise ValueError(f"timezone 必须为 Asia/Shanghai（当前 {tz}）— 日期口径硬约束")
    pol = cfg["delivery"].get("late_announcement_policy")
    if pol != "backend_only":
        # P1-2（2026-08-17）：客户发布策略收敛。客户日报只展示当天公告；
        # supplement_section / urgent_only 仅允许用于内部审计预览（如 make_preview 审计样例），
        # 不得作用于微信/网页/邮件等任何客户渠道。正式配置一律 backend_only。
        raise ValueError(
            f"late_announcement_policy 必须为 backend_only（客户日报只展示当天公告）：当前 {pol}。"
            f"supplement_section/urgent_only 仅可用于内部审计预览，禁止用于客户渠道")


def storage_paths(cfg: dict) -> dict[str, Path]:
    """将 storage 段相对路径解析为项目根下绝对路径。"""
    s = cfg.get("storage", {})
    return {k: ROOT / v for k, v in s.items()}
