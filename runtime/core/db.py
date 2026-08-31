# F018 §4.1：API 启动时执行数据库迁移检查。
# 纯标准库实现：解析 DATABASE_URL 中的主机/端口并探测可达性，
# 在缺少数据库依赖或未安装 psycopg 时返回"不可用"，而不是抛异常。
import socket
from urllib.parse import unquote, urlparse

from runtime.core.config import database_url


class DatabaseStatus:
    def __init__(self, available: bool, error: str | None = None):
        self.available = available
        self.error = error

    def as_dict(self) -> dict:
        return {"available": self.available, "error": self.error}


def _parse_host_port(url: str) -> tuple[str, int] | None:
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    port = parsed.port or 5432
    return parsed.hostname, port


def check_database(timeout: float = 3.0) -> DatabaseStatus:
    """探测数据库连接。仅用于健康检查/迁移前置，不做任何业务读写。"""
    url = database_url()
    host_port = _parse_host_port(url)
    if host_port is None:
        return DatabaseStatus(False, "DATABASE_URL 缺少主机")
    host, port = host_port
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return DatabaseStatus(True)
    except OSError as exc:
        return DatabaseStatus(False, f"{type(exc).__name__}: {exc}")


def migrate_check() -> DatabaseStatus:
    """迁移检查：数据库可用但迁移未执行时，只报状态，由运维执行 alembic upgrade head。

    禁止在 API 启动时自动写库（F019 §5.1：所有结构变更使用 Alembic）。
    """
    status = check_database()
    if status.available:
        return status
    return status