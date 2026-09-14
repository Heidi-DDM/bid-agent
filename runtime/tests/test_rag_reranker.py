# F025：本地 reranker 仅重排已过滤候选；故障时由 hybrid_search 可解释降级。
from types import SimpleNamespace

import pytest

from runtime.rag.retriever import _rerank


def test_rerank_reorders_only_candidate_pool():
    ordered = [{"chunk_id": "CH-A", "rrf": 0.03}, {"chunk_id": "CH-B", "rrf": 0.02}]
    chunks = {
        "CH-A": SimpleNamespace(text="资质等级二级"),
        "CH-B": SimpleNamespace(text="建筑工程施工总承包二级及以上"),
    }
    calls: list[tuple[str, list[str]]] = []

    def fake_rerank(query: str, documents: list[str]) -> list[float]:
        calls.append((query, documents))
        return [0.1, 0.9]

    result, applied = _rerank("建筑工程施工总承包二级", ordered, chunks, fake_rerank)

    assert applied is True
    assert [item["chunk_id"] for item in result] == ["CH-B", "CH-A"]
    assert calls == [("建筑工程施工总承包二级", ["资质等级二级", "建筑工程施工总承包二级及以上"])]


def test_rerank_rejects_incomplete_scores_without_fabricating_rank():
    ordered = [{"chunk_id": "CH-A", "rrf": 0.03}, {"chunk_id": "CH-B", "rrf": 0.02}]
    chunks = {"CH-A": SimpleNamespace(text="A"), "CH-B": SimpleNamespace(text="B")}

    with pytest.raises(ValueError, match="数量"):
        _rerank("query", ordered, chunks, lambda _query, _documents: [0.9])
