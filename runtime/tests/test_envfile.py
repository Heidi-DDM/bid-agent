# F018：.env 自动加载纯逻辑测试（envfile：解析/引号/不覆盖语义/pytest 下跳过）
from __future__ import annotations

import os
import sys

import pytest

from runtime.core.envfile import (
    apply_to_environ,
    load_dotenv_if_dev,
    load_env_file,
    parse_env_file,
)


def test_parse_basic() -> None:
    text = (
        "APP_ENV=dev\n"
        "# 注释行\n"
        "\n"
        "B = two\n"
        "C='quoted'\n"
        'D="d q"\n'
        "export E=5\n"
        "NOVALUE\n"
    )
    assert parse_env_file(text) == {
        "APP_ENV": "dev",
        "B": "two",
        "C": "quoted",
        "D": "d q",
        "E": "5",
    }


def test_parse_empty_and_quotes() -> None:
    assert parse_env_file("") == {}
    assert parse_env_file("   \n# only comment\n") == {}
    assert parse_env_file('K=""') == {"K": ""}
    assert parse_env_file("K='a=b'") == {"K": "a=b"}  # 值内 = 不拆分


def test_apply_does_not_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEEP", "orig")
    applied = apply_to_environ({"KEEP": "new", "NEW_KEY": "1"})
    assert os.environ["KEEP"] == "orig"
    assert os.environ["NEW_KEY"] == "1"
    assert applied == 1
    monkeypatch.delenv("NEW_KEY", raising=False)


def test_apply_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K", "old")
    apply_to_environ({"K": "new"}, overwrite=True)
    assert os.environ["K"] == "new"


def test_load_env_file_missing_returns_empty() -> None:
    assert load_env_file(__import__("pathlib").Path("/nonexistent/.env")) == {}


def test_load_dotenv_skipped_under_pytest() -> None:
    # 本测试自身运行在 pytest 下：加载必须跳过（避免 .env 干扰测试环境）
    assert "pytest" in sys.modules
    assert load_dotenv_if_dev() == 0