# R018/F018：运行环境与应用配置
# 约束：密钥/真实路径不进 Git；APP_ENV=test 时所有运行数据落在临时目录。
# 本文件不 import 第三方库，保证 py_compile 与逻辑测试在无依赖环境下可跑。

import os

# API/worker 启动时自动加载 runtime/.env（README §3；测试运行与已有环境变量优先，不覆盖）
from .envfile import load_dotenv_if_dev

load_dotenv_if_dev()

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
    return int(_env("BIND_PORT", "8000"))


# ---------- RAG 知识库（F025 / docs/07 方案 §3.1） ----------


def pgvector_enabled() -> bool:
    """pgvector 向量索引开关；关闭时不得创建成功索引/检索任务。"""
    return _env("PGVECTOR_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def embedding_model() -> str | None:
    return _env("EMBEDDING_MODEL")


def embedding_dim() -> int:
    return int(_env("EMBEDDING_DIM", "1024"))


def reranker_enabled() -> bool:
    return _env("RERANKER_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def reranker_model() -> str | None:
    return _env("RERANKER_MODEL")


def rag_top_k() -> int:
    return int(_env("RAG_TOP_K", "20"))


def rag_rerank_candidate_k() -> int:
    """重排前 RRF 候选池上限；最终候选数始终至少为请求 top-k。"""
    return int(_env("RAG_RERANK_CANDIDATE_K", "60"))


def rag_chunk_size() -> int:
    return int(_env("RAG_CHUNK_SIZE", "600"))


def rag_chunk_overlap() -> int:
    return int(_env("RAG_CHUNK_OVERLAP", "80"))


# ---------- DeepSeek（仅 public_read 数据，F025 §5 / ADR-002 §2.3） ----------


def deepseek_enabled() -> bool:
    return _env("DEEPSEEK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def deepseek_public_only() -> bool:
    """public-only 开关：false 时 /readyz 失败且外部抽取被拒绝（fail-closed）。"""
    return _env("DEEPSEEK_PUBLIC_ONLY", "true").strip().lower() in ("1", "true", "yes", "on")


def deepseek_base_url() -> str | None:
    return _env("DEEPSEEK_BASE_URL")


def deepseek_model() -> str | None:
    return _env("DEEPSEEK_MODEL", "deepseek-chat")


def deepseek_api_key() -> str | None:
    return _env("DEEPSEEK_API_KEY")  # type: ignore[return-value]


# ---------- P4：规则预筛 + 云端大模型受约束兜底（docs/10 §5 P4） ----------


def llm_fallback_enabled() -> bool:
    """兜底总开关（默认关）。生效还需 DeepSeek 出域门禁通过（check_deepseek_allowed）；
    任一不满足即 fail-closed：规则未命中字段保持 missing 转人工，不调模型。"""
    return _env("LLM_FALLBACK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def llm_fallback_max_chars() -> int:
    """送模型的原文上限（公开公告正文，超长截断——只影响定位覆盖，不影响正确性）。"""
    return int(_env("LLM_FALLBACK_MAX_CHARS", "12000"))  # type: ignore[arg-type]


def llm_fallback_timeout_seconds() -> float:
    return float(_env("LLM_FALLBACK_TIMEOUT_SECONDS", "60"))  # type: ignore[arg-type]


def job_running_timeout_seconds() -> int:
    return int(_env("JOB_RUNNING_TIMEOUT_SECONDS", "600"))  # type: ignore[return-value]


def worker_poll_interval_seconds() -> float:
    return float(_env("WORKER_POLL_INTERVAL_SECONDS", "5"))  # type: ignore[return-value]


def worker_heartbeat_stale_seconds() -> float:
    """ADR-004 §2.6：worker 进程心跳超过此秒数视为不可用（/readyz worker 检查项）。

    默认 60s（≥ 3 倍默认轮询间隔 + 数据库抖动余量）；部署可按轮询间隔调整。"""
    return float(_env("WORKER_HEARTBEAT_STALE_SECONDS", "60"))  # type: ignore[return-value]


def readyz_timeout_seconds() -> float:
    """短时健康检查超时；不用于实际 embedding/rerank 推理。"""
    return float(_env("READYZ_TIMEOUT_SECONDS", "3"))  # type: ignore[return-value]


def model_request_timeout_seconds() -> float:
    """本地模型推理请求超时，覆盖冷启动与较长候选池的重排。"""
    return float(_env("MODEL_REQUEST_TIMEOUT_SECONDS", "60"))  # type: ignore[return-value]


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