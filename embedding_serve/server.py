"""本地 embedding HTTP 服务（OpenAI 兼容 /v1/embeddings，bge-m3）。

F025 §5 / docs/07 方案：企业向量化不出内网 —— 本服务仅监听 127.0.0.1，
由 runtime/core/model.py 适配器（MODEL_BASE_URL=http://127.0.0.1:8001）调用。

环境变量:
  BGE_M3_PATH  模型目录（默认 HF 标准缓存中的 BAAI/bge-m3 snapshot）
  EMBED_PORT   监听端口（默认 8001）

用法:
  python server.py            # 前台运行
  bash start.sh start         # 常驻运行（nohup），stop/status 见 start.sh
"""
from __future__ import annotations

import os
import time
from typing import Union

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

MODEL_PATH = os.environ.get(
    "BGE_M3_PATH"
) or os.path.expanduser(
    "~/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots/"
    "5617a9f61b028005a4858fdac845db406aefb181"
)
PORT = int(os.environ.get("EMBED_PORT", "8001"))
DEFAULT_MODEL_NAME = "bge-m3-local"
_DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

_model: SentenceTransformer | None = None
_loaded_at: float | None = None


def get_model() -> SentenceTransformer:
    global _model, _loaded_at
    if _model is None:
        print(f"[embedding] loading {MODEL_PATH} (device={_DEVICE}) ...", flush=True)
        t0 = time.time()
        _model = SentenceTransformer(MODEL_PATH, device=_DEVICE)
        _loaded_at = time.time()
        print(f"[embedding] loaded in {time.time() - t0:.1f}s", flush=True)
    return _model


app = FastAPI(title="local-embedding", version="0.1.0")


class EmbedRequest(BaseModel):
    model: str | None = None
    input: Union[str, list[str]] = Field(
        ..., description="OpenAI 兼容：单个字符串或字符串数组"
    )


@app.get("/health")
def health() -> dict:
    # 注意：不返回模型绝对路径（MODEL_PATH 含本机用户名，内网共享部署时会被 /health 探测到）；
    # 排查路径问题看启动日志（server.log 打印了加载路径）。
    return {
        "status": "ok",
        "model": DEFAULT_MODEL_NAME,
        "dim": 1024,
        "device": _DEVICE,
        "loaded": _model is not None,
    }


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest) -> dict:
    if _model is None:
        # 首次请求触发加载（uvicorn 单 worker 常驻后通常已预热）
        get_model()
    texts = req.input if isinstance(req.input, list) else [req.input]
    for t in texts:
        if not isinstance(t, str) or not t.strip():
            raise HTTPException(status_code=400, detail="input 必须为非空字符串或字符串数组")
    model = get_model()
    vectors = model.encode(
        texts,
        normalize_embeddings=False,
        batch_size=32,
        show_progress_bar=False,
        convert_to_numpy=True,
    ).tolist()
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vec, "index": i}
            for i, vec in enumerate(vectors)
        ],
        "model": req.model or DEFAULT_MODEL_NAME,
        "usage": {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": sum(len(t) for t in texts)},
    }


if __name__ == "__main__":
    import uvicorn

    print(f"[embedding] bge-m3 serve on 127.0.0.1:{PORT} (device={_DEVICE})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
