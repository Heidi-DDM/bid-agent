# 纯标准库 .env 加载（F018：config 不依赖第三方库；.env 密钥不入 Git）
# 语义：
# - 逐行解析 KEY=VALUE；忽略空行与 # 注释；支持 `export ` 前缀；
# - 值支持单/双引号包裹（引号内原样，不解析转义）；
# - 绝不覆盖进程已有环境变量（shell 显式 export 优先）；
# - 在 pytest 下运行时跳过（测试环境由测试模块显式设置变量，避免 .env 干扰）。
from __future__ import annotations

import os
import sys
from pathlib import Path

# 候选路径：runtime/.env（README §3 约定）优先，其次项目根 .env
_ENV_CANDIDATES = (
    Path(__file__).resolve().parent.parent / ".env",  # runtime/.env
    Path(__file__).resolve().parents[2] / ".env",     # 项目根 .env
)


def parse_env_file(text: str) -> dict[str, str]:
    """解析 .env 文本为键值对（不覆盖语义由调用方决定）。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_env_file(path: Path) -> dict[str, str]:
    """读取 .env 文件；不存在/不可读返回空字典。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_env_file(text)


def apply_to_environ(values: dict[str, str], *, overwrite: bool = False) -> int:
    """把键值写入进程环境；默认不覆盖已有变量。返回实际写入数。"""
    applied = 0
    for key, value in values.items():
        if overwrite or key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied


def load_dotenv_if_dev() -> int:
    """API/worker 启动入口自动加载 .env（config.py 顶层调用）。

    测试（pytest 已加载）与已有环境变量优先，绝不覆盖；
    返回写入的环境变量数量。
    """
    if "pytest" in sys.modules:
        return 0
    applied = 0
    for path in _ENV_CANDIDATES:
        if path.is_file():
            applied += apply_to_environ(load_env_file(path))
    return applied