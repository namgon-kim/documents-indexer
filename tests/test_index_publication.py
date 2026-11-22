import json
from uuid import uuid4

import httpx
import pytest
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.index_publication import IndexPublisher
from qdrant_client import QdrantClient


def test_publication_search_and_failed_rebuild_preserves_previous(tmp_path):
    failed = False
    def handler(request):
        if failed:
            return httpx.Response(503)
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        qdrant = QdrantClient(":memory:")
        embedder = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("one", "passage", "query", 2))
        publisher = IndexPublisher(tmp_path, qdrant, embedder)
        chunk = {"chunk_id": str(uuid4()), "document_id": "one", "text": "대학 교육혁신"}
        first = publisher.publish("one", [chunk])
        assert publisher.search("교육혁신", "bm25")["results"][0]["chunk_id"] == chunk["chunk_id"]
        assert publisher.search("대학", "dense")["results"][0]["chunk_id"] == chunk["chunk_id"]
        failed = True
        with pytest.raises(httpx.HTTPStatusError):
            publisher.publish("two", [{"chunk_id": str(uuid4()), "document_id": "two", "text": "계획"}])
        assert publisher.active()["version"] == first["version"]
        assert publisher.search("교육혁신", "bm25")["results"][0]["chunk_id"] == chunk["chunk_id"]
        qdrant.close()


def test_rebuild_reuses_document_vectors_and_rejects_changed_endpoint(tmp_path):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body["input"])
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        qdrant = QdrantClient(":memory:")
        profile = EmbeddingProfile("one", "passage", "query", 2)
        embedder = EmbeddingClient(http, "https://one.example/v1", "key", profile)
        publisher = IndexPublisher(tmp_path, qdrant, embedder)
        first = {"chunk_id": str(uuid4()), "document_id": "one", "text": "기존 문서"}
        second = {"chunk_id": str(uuid4()), "document_id": "two", "text": "새 문서"}
        publisher.publish("one", [first])
        publisher.publish("two", [second])
        assert requests == [["기존 문서"], ["새 문서"]]

        other = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://two.example/v1", "key", profile))
        assert other.search("기존", "bm25")["results"][0]["chunk_id"] == first["chunk_id"]
        with pytest.raises(ValueError, match="임베딩 프로필"):
            other.search("기존", "dense")
        with pytest.raises(ValueError, match="임베딩 프로필"):
            other.publish("two", [second])
        qdrant.close()


def test_failed_delete_hides_document_until_cleanup_is_retried(tmp_path, monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        first = {"chunk_id": str(uuid4()), "document_id": "one", "text": "대학 교육"}
        second = {"chunk_id": str(uuid4()), "document_id": "two", "text": "대학 연구"}
        publisher.publish("one", [first])
        publisher.publish("two", [second])
        original = qdrant.delete
        def fail(*args, **kwargs):
            raise RuntimeError("unavailable")
        monkeypatch.setattr(qdrant, "delete", fail)
        with pytest.raises(RuntimeError):
            publisher.delete_document("one")
        assert "one" in publisher.deleted_documents()
        for method in ("bm25", "dense", "hybrid"):
            hits = publisher.search("대학", method)["results"]
            assert [hit["document_id"] for hit in hits] == ["two"]
        monkeypatch.setattr(qdrant, "delete", original)
        publisher.delete_document("one")
        assert publisher.active()["document_ids"] == ["two"]
        assert (publisher.root / "embedding-cache.sqlite3").exists()
        qdrant.close()


def test_rebuild_with_changed_model_keeps_old_index_until_success(tmp_path):
    unavailable = False
    calls = []
    def handler(request):
        if unavailable:
            return httpx.Response(503)
        body = json.loads(request.content)
        calls.append(body["model"])
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [0.4, 0.8]} for i in range(len(body["input"]))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        qdrant = QdrantClient(":memory:")
        original = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://example.test/v1", "key",
                                  EmbeddingProfile("old", "old-doc", "old-query", 2)))
        chunk = {"chunk_id": str(uuid4()), "document_id": "one", "text": "대학 교육"}
        old = original.publish("one", [chunk])
        new = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://example.test/v1", "key",
                             EmbeddingProfile("new", "new-doc", "new-query", 2)))
        unavailable = True
        with pytest.raises(httpx.HTTPStatusError):
            new.rebuild_with_current_profile()
        assert new.active()["version"] == old["version"]
        assert original.search("대학", "bm25")["results"][0]["chunk_id"] == chunk["chunk_id"]
        unavailable = False
        assert original.search("대학", "dense")["results"][0]["chunk_id"] == chunk["chunk_id"]
        result = new.rebuild_with_current_profile()
        assert result["version"] != old["version"] and result["embedding_profile"]["profile_id"] == "new"
        assert new.search("대학", "dense")["results"][0]["chunk_id"] == chunk["chunk_id"]
        assert new.search("대학", "bm25", version=old["version"])["results"][0]["chunk_id"] == chunk["chunk_id"]
        with pytest.raises(ValueError, match="임베딩 프로필"):
            new.search("대학", "dense", version=old["version"])
        assert "new-doc" in calls and "new-query" in calls
        qdrant.close()
