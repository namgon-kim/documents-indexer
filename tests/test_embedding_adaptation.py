import json
from uuid import uuid4

import httpx
import pytest
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.index_publication import IndexPublisher
from qdrant_client import QdrantClient


def test_provider_length_rejection_splits_only_offending_table_and_preserves_evidence(tmp_path):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        texts = body["input"]
        requests.append(texts)
        if any(len(text.encode()) > 120 for text in texts):
            return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(texts))]})
    chunk = {"chunk_id": str(uuid4()), "document_id": "doc", "job_id": "job", "page": 1,
             "kind": "table", "element_ids": ["e1"], "heading_path": [], "heading_element_ids": [],
             "text": "사업비\n" + "가나다 " * 30, "table_cells": [{"row": 2, "column": 2}],
             "table_segments": [{"row": 2, "column": 2, "text": "가나다 " * 30}]}
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc-model", "query-model", 2)))
        result = publisher.publish("doc", [chunk])
        saved = publisher.document_chunks("doc", result)
        assert len(saved) > 1 and all(item["element_ids"] == ["e1"] for item in saved)
        assert all(item["table_cells"] == [{"row": 2, "column": 2}] for item in saved)
        assert all(len(item["text"].encode()) <= 120 for item in saved)
        assert all("사업비" in item["text"] for item in saved)
        assert len(requests) > 1
        qdrant.close()


def test_http_error_without_explicit_input_limit_is_not_retried(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "bad model"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        with pytest.raises(httpx.HTTPStatusError):
            publisher.publish("doc", [{"chunk_id": str(uuid4()), "document_id": "doc", "text": "교육 계획"}])
        assert len(calls) == 1 and publisher.active() is None
        qdrant.close()


def test_profile_byte_limit_and_batch_size_are_applied(tmp_path):
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body["input"])
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2, 256, 2)))
        chunks = [{"chunk_id": str(uuid4()), "document_id": "doc", "text": f"{i} " + "가" * 120} for i in range(3)]
        result = publisher.publish("doc", chunks)
        assert result["chunk_count"] > 3
        assert all(len(batch) <= 2 for batch in seen)
        assert all(len(text.encode()) <= 256 for batch in seen for text in batch)
        qdrant.close()


def test_split_table_value_keeps_its_label_for_every_fragment():
    from indexer.embedding_adaptation import split_chunk
    chunk = {"chunk_id": str(uuid4()), "document_id": "doc", "text": "사업비\n설비 / 2026 / " + "내용 " * 80,
             "table_segments": [{"row": 2, "column": 3, "label": "설비 / 2026", "value": "내용 " * 80,
                                  "text": "설비 / 2026 / " + "내용 " * 80}]}
    fragments = split_chunk(chunk, 120)
    assert len(fragments) > 1
    assert all("설비 / 2026" in item["text"] for item in fragments)
    assert all(item["table_cells"] == [{"row": 2, "column": 3}] for item in fragments)


def test_tiny_context_budget_does_not_return_unbounded_text():
    from indexer.embedding_input import context_prefix
    for budget in range(8):
        assert len(context_prefix("머리글이 아주 긴 문서 제목", budget).encode("utf-8")) <= budget
