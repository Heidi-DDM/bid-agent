"""完全本地的 BGE embedding + cross-encoder reranker 服务。

接口：
- ``POST /v1/embeddings``：BAAI/bge-m3，OpenAI 兼容；
- ``POST /v1/rerank``：BAAI/bge-reranker-v2-m3，输入 query + 已过滤候选文本；
- ``GET /health``：不暴露本机路径，只报告模型资产和加载状态。

服务只监听 127.0.0.1。企业 L3 内容仅被同机/内网运行时调用，不会被发送到外部模型。
模型下载必须由 ``bash start.sh download-models`` 显式执行，服务请求不会联网下载。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Union

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import CrossEncoder, SentenceTransformer


PORT = int(os.environ.get("EMBED_PORT", "8001"))
DEFAULT_EMBEDDING_NAME = "bge-m3-local"
DEFAULT_RERANKER_NAME = "bge-reranker-v2-m3-local"
_DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
_CACHE_ROOT = Path.home() / ".cache" / "huggingface" / "hub"


def _first_snapshot(repo_cache_name: str) -> str | None:
    snapshots = _CACHE_ROOT / repo_cache_name / "snapshots"
    if not snapshots.is_dir():
        return None
    candidates = sorted(path for path in snapshots.iterdir() if path.is_dir())
    return str(candidates[-1]) if candidates else None


# 环境变量可覆盖，以支持内网共享模型盘；未配置时只查标准 HF 缓存，不触发下载。
EMBEDDING_PATH = os.environ.get("BGE_M3_PATH") or _first_snapshot("models--BAAI--bge-m3")
RERANKER_PATH = os.environ.get("BGE_RERANKER_PATH") or _first_snapshot(
    "models--BAAI--bge-reranker-v2-m3"
)

_embedding_model: SentenceTransformer | None = None
_reranker_model: CrossEncoder | None = None
_embedding_loaded_at: float | None = None
_reranker_loaded_at: float | None = None


def _asset_available(path: str | None) -> bool:
    return bool(path and Path(path).is_dir())


def get_embedding_model() -> SentenceTransformer:
    global _embedding_model, _embedding_loaded_at
    if _embedding_model is None:
        if not _asset_available(EMBEDDING_PATH):
            raise RuntimeError("BGE-M3 模型资产不存在；请先显式执行 start.sh download-models")
        print(f"[local-model] loading embedding model (device={_DEVICE}) ...", flush=True)
        started = time.time()
        _embedding_model = SentenceTransformer(EMBEDDING_PATH, device=_DEVICE)
        _embedding_loaded_at = time.time()
        print(f"[local-model] embedding loaded in {time.time() - started:.1f}s", flush=True)
    return _embedding_model


def get_reranker_model() -> CrossEncoder:
    global _reranker_model, _reranker_loaded_at
    if _reranker_model is None:
        if not _asset_available(RERANKER_PATH):
            raise RuntimeError("BGE reranker 模型资产不存在；请先显式执行 start.sh download-models")
        print(f"[local-model] loading reranker model (device={_DEVICE}) ...", flush=True)
        started = time.time()
        _reranker_model = CrossEncoder(RERANKER_PATH, device=_DEVICE)
        _reranker_loaded_at = time.time()
        print(f"[local-model] reranker loaded in {time.time() - started:.1f}s", flush=True)
    return _reranker_model


app = FastAPI(title="local-rag-models", version="0.2.0")


class EmbedRequest(BaseModel):
    model: str | None = None
    input: Union[str, list[str]] = Field(..., description="OpenAI 兼容：单个字符串或字符串数组")


class RerankRequest(BaseModel):
    model: str | None = None
    query: str = Field(..., min_length=1, max_length=10000)
    documents: list[str] = Field(..., min_length=1, max_length=200)


@app.get("/health")
def health() -> dict:
    # 禁止回传模型绝对路径，避免把本机用户名/挂载结构暴露给调用方。
    return {
        "status": "ok",
        "embedding": {
            "model": DEFAULT_EMBEDDING_NAME,
            "dim": 1024,
            "available": _asset_available(EMBEDDING_PATH),
            "loaded": _embedding_model is not None,
        },
        "reranker": {
            "model": DEFAULT_RERANKER_NAME,
            "available": _asset_available(RERANKER_PATH),
            "loaded": _reranker_model is not None,
        },
        "device": _DEVICE,
    }


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest) -> dict:
    texts = req.input if isinstance(req.input, list) else [req.input]
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise HTTPException(status_code=400, detail="input 必须为非空字符串或字符串数组")
    try:
        model = get_embedding_model()
        vectors = model.encode(
            texts,
            normalize_embeddings=False,
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).tolist()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vector, "index": index}
            for index, vector in enumerate(vectors)
        ],
        "model": req.model or DEFAULT_EMBEDDING_NAME,
        "usage": {"prompt_tokens": sum(len(text) for text in texts), "total_tokens": sum(len(text) for text in texts)},
    }


@app.post("/v1/rerank")
def rerank(req: RerankRequest) -> dict:
    if not req.query.strip() or any(not document.strip() for document in req.documents):
        raise HTTPException(status_code=400, detail="query 和 documents 必须为非空字符串")
    try:
        model = get_reranker_model()
        # 统一输出 sigmoid 后的 0..1 相关性；该数值仅用于候选排序，绝非资格置信度。
        scores = model.predict(
            [[req.query, document] for document in req.documents],
            activation_fn=torch.nn.Sigmoid(),
            show_progress_bar=False,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "object": "list",
        "data": [
            {"object": "rerank_result", "index": index, "relevance_score": float(score)}
            for index, score in enumerate(scores)
        ],
        "model": req.model or DEFAULT_RERANKER_NAME,
    }


if __name__ == "__main__":
    import uvicorn

    print(f"[local-model] serving on 127.0.0.1:{PORT} (device={_DEVICE})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
