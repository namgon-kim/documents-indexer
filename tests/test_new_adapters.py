import json
from uuid import uuid4

import httpx
from indexer.dense_index import DenseIndex, EmbeddingClient, EmbeddingProfile
from indexer.reranker import CohereReranker
from indexer.search_engine import SearchHit
from qdrant_client import QdrantClient


def test_dense_profile_and_qdrant_search():
    models = []
    def handler(request):
        data = json.loads(request.content)
        models.append(data["model"])
        return httpx.Response(200, json={"model": data["model"], "data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(data["input"]))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        embed = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "passage", "query", 2))
        client = QdrantClient(":memory:")
        index = DenseIndex(client, "new_profile", embed)
        chunk_id = str(uuid4())
        index.build([{"chunk_id": chunk_id, "document_id": "doc", "text": "대학"}])
        assert index.search("대학", 5, "doc")[0].chunk_id == chunk_id
        assert index.search("대학", 5, "other") == []
        assert models == ["passage", "query", "query"]
        client.close()


def test_cohere_rerank_maps_indices_to_original_chunks():
    def handler(request):
        data = json.loads(request.content)
        assert data["documents"] == ["문서 A", "문서 B"]
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        reranker = CohereReranker(http, "https://example.test/v2", "key", "model", lambda key: {"a": "문서 A", "b": "문서 B"}[key])
        assert reranker.rerank("질문", [SearchHit("a", 0.5), SearchHit("b", 0.4)], 1) == [SearchHit("b", 0.9)]


def test_embedding_rejects_wrong_model_dimension_and_invalid_numbers():
    import pytest

    responses = [
        {"model": "other", "data": [{"index": 0, "embedding": [1.0, 0.5]}]},
        {"model": "doc", "data": [{"index": 0, "embedding": [1.0]}]},
        {"model": "doc", "data": [{"index": 0, "embedding": [0.0, 0.0]}]},
        {"model": "doc", "data": [{"index": 1, "embedding": [1.0, 0.5]}]},
    ]
    for payload in responses:
        with httpx.Client(transport=httpx.MockTransport(lambda _, result=payload: httpx.Response(200, json=result))) as http:
            embedder = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2))
            with pytest.raises(ValueError):
                embedder.embed(["본문"])
