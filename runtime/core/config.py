# R018/F018：运行环境与应用配置
# 约束：密钥/真实路径不进 Git；APP_ENV=test 时所有运行数据落在临时目录。
# 本文件不 import 第三方库，保证 py_compile 与逻辑测试在无依赖环境下可跑。

import os

# F018 §3 环境配置清单
REQUIRED_ENV = (
    "DATABASE_URL",
    "OBJECT_STORE_ROOT",
    "APP_ENV",
    "LOG_LEVEL",
)


def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def database_url() -> str:
    # 本地开发默认账号 bid_agent/bid_agent_dev（由 setup_local_env.sh 自动创建，
    # 仅限本机开发；生产/内网部署必须用 DATABASE_URL 环境变量覆盖）。
    # 显式使用 127.0.0.1（IPv4 回环）：避免 localhost 解析到 ::1 时连到 Docker 端口转发。
    return _env("DATABASE_URL", "postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent")  # type: ignore[return-value]


def object_store_root() -> str:
    if _env("APP_ENV") == "test":
        import tempfile

        return os.path.join(tempfile.gettempdir(), "bid_agent_objects_test")
    return _env("OBJECT_STORE_ROOT", os.path.abspath("runtime/objects"))  # type: ignore[return-value]


def model_base_url() -> str | None:
    return _env("MODEL_BASE_URL")


def model_name() -> str | None:
    return _env("MODEL_NAME")


def ocr_lang() -> str:
    return _env("OCR_LANG", "chi_sim")  # type: ignore[return-value]


def app_env() -> str:
    return _env("APP_ENV", "dev")  # type: ignore[return-value]


def log_level() -> str:
    return _env("LOG_LEVEL", "INFO")  # type: ignore[return-value]


def bind_host() -> str:
    # F018 §6：开发环境默认仅绑定本机回环地址
    return _env("BIND_HOST", "127.0.0.1")  # type: ignore[return-value]


def bind_port() -> int:
    return int(_env("BIND_PORT", "8000"))  # type: ignore[return-value]


def job_running_timeout_seconds() -> int:
    return int(_env("JOB_RUNNING_TIMEOUT_SECONDS", "600"))  # type: ignore[return-value]


def worker_poll_interval_seconds() -> float:
    return float(_env("WORKER_POLL_INTERVAL_SECONDS", "5"))  # type: ignore[return-value]


def readyz_timeout_seconds() -> float:
    return float(_env("READYZ_TIMEOUT_SECONDS", "3"))  # type: ignore[return-value]


def logging_config() -> dict:
    """日志只记录元数据与错误摘要，禁止完整原文/身份证号/手机号（F018 §5）。

    默认级别 DEBUG 即开启脱敏；INFO 级别同样经过 RecordSensitiveFilter 过滤。
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "sensitive": {
                "()": "runtime.core.logging_utils.RecordSensitiveFilter",
            },
        },
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": log_level(),
                "formatter": "default",
                "filters": ["sensitive"],
            },
        },
        "root": {"level": log_level(), "handlers": ["console"]},
    }