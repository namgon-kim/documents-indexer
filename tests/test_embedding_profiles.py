import json
import stat
import time
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from indexer.artifact_store import ArtifactStore
from indexer.config import Config
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.embedding_profiles import EmbeddingProfiles
from indexer.index_publication import IndexPublisher
from indexer.ingestion_service import IngestionService
from indexer.parsing.contracts import ParseResult
from pypdf import PdfWriter
from qdrant_client import QdrantClient

from apps.api.main import create_app


def test_profiles_use_distinct_indexes_and_reject_changing_used_identity(tmp_path):
    def provider(request):
        body = json.loads(request.content)
        vector = [1.0, 0.0] if body["model"].startswith("a") else [0.0, 1.0]
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": index, "embedding": vector} for index in range(len(body["input"]))]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        default = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://example.test/v1", "secret-a",
                                  EmbeddingProfile("alpha", "a-doc", "a-query", 2)))
        registry = EmbeddingProfiles(tmp_path, default, http)
        assert registry.list()[0]["name"] == "a-doc"
        saved = registry.save({"profile_id": "beta", "name": "두 번째", "base_url": "https://example.test/v1",
                               "api_key": "secret-b", "document_model": "b-doc", "query_model": "b-query",
                               "dimensions": 2, "max_input_bytes": 12000, "enabled": True})
        assert "secret-b" not in json.dumps(saved) and saved["api_key_configured"]
        assert saved["max_input_bytes"] == 12000
        assert registry.publisher("beta").embedder.profile.max_input_bytes == 12000
        assert registry.publisher("beta").embedder.embed(["가" * 1400]) == [[0.0, 1.0]]
        assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600
        from uuid import uuid4
        first = {"chunk_id": str(uuid4()), "document_id": "one", "text": "교육"}
        second = {"chunk_id": str(uuid4()), "document_id": "two", "text": "연구"}
        default.publish("one", [first])
        beta = registry.publisher("beta", for_ingest=True)
        beta.publish("two", [second])
        assert default.active()["document_ids"] == ["one"] and beta.active()["document_ids"] == ["two"]
        assert beta.search("연구", "dense")["results"][0]["document_id"] == "two"
        beta_version = beta.active()["version"]
        default.embedder = EmbeddingClient(http, "https://example.test/v1", "secret-a",
                                           EmbeddingProfile("alpha", "a-new-doc", "a-new-query", 2))
        default.rebuild_with_current_profile()
        assert beta.active()["version"] == beta_version
        with pytest.raises(ValueError, match="새 ID"):
            registry.save({"profile_id": "beta", "name": "두 번째", "base_url": "https://example.test/v1",
                           "document_model": "other", "query_model": "b-query", "dimensions": 2, "enabled": True})
        updated = registry.save({"profile_id": "beta", "name": "두 번째", "base_url": "https://example.test/v1",
                                 "document_model": "b-doc", "query_model": "b-query", "dimensions": 2, "enabled": False})
        assert updated["api_key_configured"] and not updated["enabled"]
        with pytest.raises(ValueError, match="사용 중지"):
            registry.publisher("beta", for_ingest=True)
        assert registry.publisher("beta").search("연구", "bm25")["results"]
        with pytest.raises(ValueError, match="사용 중"):
            registry.delete("beta")
        default.delete_document("two")
        assert registry.publisher("beta").active()["document_ids"] == []
        registry.path.write_text(json.dumps([{"profile_id": "broken", "api_key": "secret-to-protect"}]), encoding="utf-8")
        with pytest.raises(ValueError) as error:
            registry.list()
        assert "secret-to-protect" not in str(error.value)
        qdrant.close()


def test_api_selects_profile_for_upload_reprocess_and_search(tmp_path):
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            row = {"page": 1, "category": "paragraph", "content": {"text": "대학 교육혁신"}}
            return ParseResult("fake", [row], {"elements": [row]})
    models = []
    def provider(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "교육혁신입니다. [1]"}, "finish_reason": "stop"}]})
        models.append(body["model"])
        vector = [1.0, 0.0] if body["model"].startswith("a") else [0.0, 1.0]
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": index, "embedding": vector} for index in range(len(body["input"]))]})
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        parser = Parser()
        publisher = IndexPublisher(tmp_path / "ingestion", qdrant, EmbeddingClient(http, "https://example.test/v1", "secret-a",
                                   EmbeddingProfile("alpha", "a-doc", "a-query", 2)))
        service = IngestionService(ArtifactStore(tmp_path), parser, publisher)
        config = Config(tmp_path, access_key="example", external_processing=True, llm_api_key="llm-secret",
                        public_origin="https://testserver", frame_origins=("https://host.example",),
                        embed_secret="synthetic-test-signing-secret-32-bytes")
        with TestClient(create_app(config, ingestion_service=service), base_url="https://testserver") as client:
            login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
            client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
            client.app.state.embedding_profiles.client = http
            client.app.state.published_chat.client = http
            profile = {"name": "두 번째", "base_url": "https://example.test/v1", "api_key": "secret-b",
                       "document_model": "b-doc", "query_model": "b-query", "dimensions": 2, "enabled": True}
            added = client.put("/v1/embedding-profiles/beta", json=profile)
            assert added.status_code == 200 and "secret-b" not in added.text
            assert "secret-b" not in client.get("/v1/embedding-profiles").text
            from scripts.issue_embed_code import issue_code
            nonce = "test-nonce-12345678"
            code = issue_code(app_origin="https://testserver", host_origin="https://host.example", nonce=nonce,
                              secret=config.embed_secret)
            grant = client.post("/v1/auth/embed/exchange", json={"code": code, "nonce": nonce,
                                "parent_origin": "https://host.example"})
            embedded = client.get("/v1/embedding-profiles", headers={"Authorization": "Bearer " + grant.json()["access_token"]}).json()
            assert set(embedded["profiles"][0]) == {"profile_id", "name", "enabled", "default"}
            assert client.put("/v1/settings", json={"values": {"embedding_profile": "beta"}}).status_code == 422
            checked = client.post("/v1/embedding-profiles/beta/validate", json={})
            assert checked.status_code == 200 and checked.json()["dimensions"] == 2
            assert "b-doc" in models and "b-query" in models
            result = client.post("/v1/ingest", files={"file": ("report.pdf", stream.getvalue())},
                                 data={"embedding_profile_id": "alpha"})
            assert result.status_code == 200, result.text
            doc, job, version = (result.json()[key] for key in ("document_id", "job_id", "index_version"))
            second = client.post(f"/v1/ingest/{doc}/jobs", json={"base_job_id": job, "mode": "rechunk", "embedding_profile_id": "beta"})
            assert second.status_code == 202, second.text
            for _ in range(200):
                status = client.get(f"/v1/ingest/{doc}/jobs/{second.json()['job_id']}").json()
                if status["status"] in {"completed", "failed"}: break
                time.sleep(0.01)
            assert status["status"] == "completed" and parser.calls == 1
            for profile_id in ("alpha", "beta"):
                found = client.post("/v1/retrieve", json={"query": "교육혁신", "method": "hybrid", "profile_id": profile_id})
                assert found.status_code == 200 and found.json()["results"][0]["document_id"] == doc
                assert client.get("/v1/ingestion-documents", params={"profile_id": profile_id}).json()["documents"][0]["indexed"]
            mismatch = client.post("/v1/retrieve", json={"query": "교육혁신", "method": "bm25", "profile_id": "beta", "index_version": version})
            assert mismatch.status_code == 409
            answer = client.post("/v1/ingest-chat", json={"message": "교육혁신은?", "profile_id": "beta"})
            assert answer.status_code == 200 and answer.json()["sources"][0]["document_id"] == doc
            assert "b-query" in models
            assert client.delete(f"/v1/ingest/{doc}").status_code == 200
            assert registry_empty(client, "alpha") and registry_empty(client, "beta")
        qdrant.close()


def registry_empty(client, profile_id):
    return client.post("/v1/retrieve", json={"query": "교육혁신", "method": "bm25", "profile_id": profile_id}).json()["results"] == []
