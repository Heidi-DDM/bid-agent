# F018 §2 模型 / §6 私有资料处理链路禁止外发：
# 仅接入公司批准的内网/本地模型适配器，接口采用结构化 JSON。
# 模型适配器必须有"本地/内网"配置检查：MODEL_BASE_URL 缺失或不满足内网约束时禁止调用。
#
# 纯标准库实现（urllib），不引入 requests。
from __future__ import annotations

import ipaddress
import json
import socket
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from runtime.core import config
from runtime.core.config import model_base_url, model_name

# 外网域名黑名单：命中即拒绝（防止把私有资料发往外网）
PUBLIC_DOMAINS = {".aliyuncs.com", ".baidu.com", ".qcloud.com", ".amazonaws.com"}

# 模型用途（F025 §5 / docs/07 方案 §3.1）：
#   public_extraction  —— DeepSeek API，仅接收 permission_scope=public_read 数据
#   local_embedding    —— 本地/内网 embedding（BGE-M3 等），L2/L3 向量化不出内网
#   local_reranker     —— 可选本地 reranker，只改变排序不改变可见性
PUBLIC_EXTRACTION = "public_extraction"
LOCAL_EMBEDDING = "local_embedding"
LOCAL_RERANKER = "local_reranker"

# 允许进入外部模型（DeepSeek）的数据范围：仅公开可读
PUBLIC_ONLY_SCOPE = "public_read"


class ModelAdapterError(Exception):
    pass


class ModelNotAllowedError(ModelAdapterError):
    """模型未通过本地/内网检查，禁止调用。"""


class ModelUnavailableError(ModelAdapterError):
    pass


@dataclass
class ModelResult:
    ok: bool
    data: Optional[dict] = None
    error: Optional[str] = None


def _is_private_hostname(host: str) -> bool:
    host = host.lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if any(host.endswith(suffix) for suffix in PUBLIC_DOMAINS):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    # 域名：必须可解析且解析结果为内网 IP
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return True
        except ValueError:
            continue
    return False


def check_model_allowed(base_url: str | None = None) -> bool:
    """本地/内网配置检查（F018 §6）。MODEL_BASE_URL 未配置视为禁用。"""
    base_url = base_url or model_base_url()
    if not base_url:
        return False
    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return False
    if not host:
        return False
    return _is_private_hostname(host)


def health(base_url: str | None = None, timeout: float = 3.0) -> ModelResult:
    """模型适配器健康检查：GET {base_url}/health，返回结构化 JSON。"""
    base_url = base_url or model_base_url()
    if not check_model_allowed(base_url):
        return ModelResult(ok=False, error="模型未启用或非内网地址，禁止外发")
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return ModelResult(ok=True, data=payload)
    except Exception as exc:
        return ModelResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def chat(
    messages: list[dict],
    *,
    base_url: str | None = None,
    timeout: float = 30.0,
    model: str | None = None,
) -> ModelResult:
    """调用模型适配器（结构化 JSON 请求/响应）。仅在通过内网检查后执行。"""
    base_url = base_url or model_base_url()
    if not check_model_allowed(base_url):
        raise ModelNotAllowedError("模型未启用或非内网地址，禁止外发")
    payload = {
        "model": model or model_name(),
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ModelResult(ok=True, data=data)
    except Exception as exc:
        return ModelResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def embed(
    text: str,
    *,
    base_url: str | None = None,
    timeout: float = 60.0,
    model: str | None = None,
) -> ModelResult:
    """本地 embedding：POST {base_url}/v1/embeddings（OpenAI 兼容，F025 §5）。

    仅通过内网检查后执行；data 为单个文本的向量列表 list[float]。
    失败/非法响应返回 ModelResult(ok=False)，由调用方转 retryable，不伪造成功。
    """
    base_url = base_url or model_base_url()
    if not base_url or not check_model_allowed(base_url):
        raise ModelNotAllowedError("模型未启用/未配置或非内网地址，禁止外发")
    payload = {
        "model": model or config.embedding_model() or "bge-m3",
        "input": [text],
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") or []
        if not items or not isinstance(items[0].get("embedding"), list):
            return ModelResult(ok=False, error=f"embedding 响应缺少 data[].embedding: {str(data)[:200]}")
        return ModelResult(ok=True, data=items[0]["embedding"])
    except Exception as exc:
        return ModelResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def extract_text_with_model(raw_text: str, task: str = "extract") -> ModelResult:
    """F021 预留：本地模型结构化抽取入口。此处只做接口骨架，不包含任何业务解析。"""
    return chat(
        [
            {"role": "system", "content": "你是一个投标材料结构化抽取服务，只输出 JSON。"},
            {"role": "user", "content": f"任务: {task}\n原文:\n{raw_text[:4000]}"},
        ]
    )


# ---------- F025：模型用途与出域门禁（docs/07 方案 §3.1） ----------


def check_embedding_allowed() -> bool:
    """本地 embedding 检查：复用本地模型适配器地址，仅本地/内网允许（F025 §5）。"""
    return check_model_allowed()


def check_reranker_allowed() -> bool:
    """本地 reranker 检查：与 embedding 同域约束；未启用视为允许（不参与就绪判定）。"""
    if not config.reranker_enabled():
        return True
    return check_model_allowed()


def check_deepseek_allowed() -> bool:
    """DeepSeek 出域门禁：必须启用、public-only 开关打开且配置了合法地址。

    DEEPSEEK_PUBLIC_ONLY=false 时一律 fail-closed（/readyz 失败 + 调用拒绝）。
    """
    if not config.deepseek_enabled():
        return False
    if not config.deepseek_public_only():
        return False
    base_url = config.deepseek_base_url()
    if not base_url:
        return False
    return True


def deepseek_extract(
    raw_text: str,
    *,
    permission_scope: str,
    task: str = "extract",
    timeout: float = 60.0,
) -> ModelResult:
    """仅允许 public_read 数据调用 DeepSeek；企业/受限数据一律拒绝（F025 §5）。

    结构化 JSON 请求/响应（OpenAI 兼容格式）；日志不保存完整原文。
    """
    if permission_scope != PUBLIC_ONLY_SCOPE:
        raise ModelNotAllowedError(
            f"数据范围 {permission_scope} 非 public_read，禁止发送外部模型（public-only）"
        )
    if not check_deepseek_allowed():
        raise ModelNotAllowedError("DeepSeek 未启用或 public-only 开关关闭，禁止外部抽取")
    payload = {
        "model": config.deepseek_model(),
        "messages": [
            {"role": "system", "content": "你是一个公开招标文本结构化抽取服务，只输出 JSON。"},
            {"role": "user", "content": f"任务: {task}\n原文:\n{raw_text[:4000]}"},
        ],
        "response_format": {"type": "json_object"},
    }
    base_url = config.deepseek_base_url().rstrip("/")  # type: ignore[union-attr]
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.deepseek_api_key() or ''}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ModelResult(ok=True, data=data)
    except Exception as exc:
        return ModelResult(ok=False, error=f"{type(exc).__name__}: {exc}")