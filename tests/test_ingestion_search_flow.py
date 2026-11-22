import json
from io import BytesIO

import httpx
from fastapi.testclient import TestClient
from indexer.artifact_store import ArtifactStore
from indexer.config import Config
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.index_publication import IndexPublisher
from indexer.ingestion_service import IngestionService
from indexer.parsing.contracts import ParseResult
from pypdf import PdfWriter
from qdrant_client import QdrantClient

from apps.api.main import create_app


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_ingest_publishes_searchable_dense_and_bm25(tmp_path):
    class Parser:
        def parse(self, request):
            element = {"page": 1, "category": "paragraph", "content": {"text": "대학 교육혁신 계획"}}
            return ParseResult("fake", [element], {"elements": [element]})

    def provider(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={"model": payload["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(payload["input"]))]})

    source = tmp_path / "report.pdf"
    source.write_bytes(pdf_bytes())
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        embedder = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2))
        publisher = IndexPublisher(tmp_path, qdrant, embedder)
        result = IngestionService(ArtifactStore(tmp_path), Parser(), publisher).ingest(source, "report")
        assert result["status"] == "completed"
        for method in ("bm25", "dense", "hybrid"):
            response = publisher.search("교육혁신", method)
            assert response["index_version"] == result["index_version"]
            assert response["results"][0]["document_id"] == result["document_id"]
        qdrant.close()


def test_api_ingest_then_hybrid_search(tmp_path):
    class Parser:
        def parse(self, request):
            element = {"page": 1, "category": "paragraph", "content": {"text": "대학 교육혁신 계획"}}
            return ParseResult("fake", [element], {"elements": [element]})
    def provider(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [{"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        embedder = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2))
        publisher = IndexPublisher(tmp_path, qdrant, embedder)
        service = IngestionService(ArtifactStore(tmp_path), Parser(), publisher)
        config = Config(tmp_path, access_key="example", external_processing=True)
        with TestClient(create_app(config, ingestion_service=service), base_url="https://testserver") as client:
            response = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
            client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": response.json()["csrf_token"]})
            result = client.post("/v1/ingest", files={"file": ("report.pdf", pdf_bytes())}, data={"profile": "report"})
            assert result.status_code == 200, result.text
            job = service.storage.load_job(result.json()["document_id"], result.json()["job_id"], "processing")
            assert (job["settings"]["target_chars"], job["settings"]["overlap_chars"]) == (1200, 200)
            found = client.post("/v1/retrieve", json={"query": "교육혁신", "method": "hybrid"})
            assert found.status_code == 200 and found.json()["results"][0]["document_id"] == result.json()["document_id"]
            hit = found.json()["results"][0]
            assert hit["filename"] == "report.pdf" and hit["source_refs"][0]["page"] == 1
            assert client.get(hit["evidence_urls"][0]).json()["content"]["text"] == "대학 교육혁신 계획"
            versions = client.get("/v1/ingest-index-versions").json()["versions"]
            assert versions[0]["index_version"] == result.json()["index_version"]
            publisher.embedder = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("new", "doc2", "query2", 2))
            info = client.get("/v1/ingest-index-status").json()
            assert not info["profile_matches"] and info["chunk_count"] > 0
            rebuild = client.post("/v1/ingest-index-rebuild", json={})
            assert rebuild.status_code == 200 and rebuild.json()["index_version"] != result.json()["index_version"]
            assert client.get("/v1/ingest-index-status").json()["profile_matches"]
            assert client.post("/v1/retrieve", json={"query": "교육혁신", "method": "dense"}).status_code == 200
        qdrant.close()


def test_published_version_search_and_chat_share_results(tmp_path):
    from indexer.chat import RagChat
    from indexer.contracts import ChatRequest, SearchRequest
    from indexer.published_retrieval import PublishedRetrieval

    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            element = {"page": 1, "category": "paragraph", "content": {"text": "대학 교육혁신" if self.calls == 1 else "산학협력 계획"}}
            return ParseResult("fake", [element], {"elements": [element]})

    def provider(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            assert "교육혁신" in body["messages"][-1]["content"]
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "답변 [1]"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        embedder = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2))
        publisher = IndexPublisher(tmp_path, qdrant, embedder)
        storage = ArtifactStore(tmp_path)
        service = IngestionService(storage, Parser(), publisher)
        first_file, second_file = tmp_path / "first.pdf", tmp_path / "second.pdf"
        first_file.write_bytes(pdf_bytes())
        second_file.write_bytes(pdf_bytes())
        first = service.ingest(first_file)
        service.ingest(second_file, allow_duplicate=True)
        retrieval = PublishedRetrieval(publisher, storage)
        result = retrieval.search(SearchRequest(query="교육혁신", method="bm25", index_version=first["index_version"]))
        assert result["index_version"] == first["index_version"]
        assert result["results"][0]["filename"] == "first.pdf"
        chat = RagChat(Config(tmp_path, external_processing=True, llm_api_key="key"), retrieval, http)
        answer = chat.invoke(ChatRequest(message="교육혁신", method="bm25", index_version=first["index_version"]))
        assert answer["sources"][0]["unit_id"] == result["results"][0]["unit_id"]
        assert len(publisher.versions()) == 2
        qdrant.close()


def test_search_and_chat_expand_nearby_text_with_original_evidence(tmp_path):
    from indexer.chat import RagChat
    from indexer.contracts import ChatRequest, SearchRequest
    from indexer.published_retrieval import PublishedRetrieval

    class Parser:
        def parse(self, request):
            rows = [
                {"page": 1, "category": "heading1", "content": {"html": "<h1>교육 전략</h1>"}},
                {"page": 1, "category": "paragraph", "content": {"text": "앞선 설명 " * 35}},
                {"page": 1, "category": "paragraph", "content": {"text": "고유검색어 " + "실행 계획 " * 35}},
                {"page": 2, "category": "paragraph", "content": {"text": "다른 페이지 내용"}},
            ]
            return ParseResult("fake", rows, {"elements": rows})
    def provider(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            assert "앞선 설명" in payload["messages"][-1]["content"]
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "답변 [1]"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"model": payload["model"], "data": [
            {"index": index, "embedding": [1.0, 0.5]} for index in range(len(payload["input"]))]})
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes())
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        storage = ArtifactStore(tmp_path)
        # The parser stub supplies two page numbers; use an actual two-page PDF.
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_blank_page(width=100, height=100)
        with source.open("wb") as stream:
            writer.write(stream)
        result = IngestionService(storage, Parser(), publisher).ingest(source, settings={"target_chars": 400, "overlap_chars": 0})
        retrieval = PublishedRetrieval(publisher, storage)
        found = retrieval.search(SearchRequest(query="고유검색어", method="bm25"))["results"][0]
        assert "앞선 설명" in found["context_before"] and "다른 페이지" not in found["context_text"]
        assert {url.rsplit("/", 1)[-1] for url in found["evidence_urls"]} == {"e3"}
        assert {url.rsplit("/", 1)[-1] for url in found["context_urls"]} == {"e1", "e2"}
        chat = RagChat(Config(tmp_path, external_processing=True, llm_api_key="key"), retrieval, http)
        answer = chat.invoke(ChatRequest(message="고유검색어", method="bm25"))
        assert answer["index_version"] == result["index_version"]
        qdrant.close()


def test_index_failure_can_resume_without_reparsing(tmp_path):
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            element = {"page": 1, "category": "paragraph", "content": {"text": "교육혁신"}}
            return ParseResult("fake", [element], {"elements": [element]})

    class Publisher:
        calls = 0
        def publish(self, document_id, chunks):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("connection failed")
            assert chunks[0]["document_id"] == document_id
            return {"version": "new-index"}

    parser, publisher = Parser(), Publisher()
    service = IngestionService(ArtifactStore(tmp_path), parser, publisher)
    with TestClient(create_app(Config(tmp_path, access_key="example", external_processing=True),
                               ingestion_service=service), base_url="https://testserver") as client:
        login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
        client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
        failed = client.post("/v1/ingest", files={"file": ("report.pdf", pdf_bytes())})
        assert failed.status_code == 502
        document_id, job_id = failed.json()["document_id"], failed.json()["job_id"]
        url = f"/v1/ingest/{document_id}/jobs/{job_id}"
        status = client.get(url).json()
        assert (status["status"], status["stage"]) == ("failed", "indexing")
        resumed = client.post(url + "/resume")
        assert resumed.status_code == 200 and resumed.json()["index_version"] == "new-index"
        assert parser.calls == 1 and publisher.calls == 2


def test_deferred_upload_reports_progress_and_searches_same_published_index(tmp_path):
    import threading
    import time

    entered, release = threading.Event(), threading.Event()
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            element = {"page": 1, "category": "paragraph", "content": {"text": "혁신 교육 계획"}}
            return ParseResult("fake", [element], {"elements": [element]})

    def provider(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        parser = Parser()
        service = IngestionService(ArtifactStore(tmp_path), parser, publisher)
        with TestClient(create_app(Config(tmp_path, access_key="example", external_processing=True), ingestion_service=service), base_url="https://testserver") as client:
            login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
            client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
            try:
                response = client.post("/v1/ingest", files={"file": ("report.pdf", pdf_bytes())}, data={"deferred": "true"})
                assert response.status_code == 200 and response.json()["status"] == "queued"
                assert entered.wait(5)
                doc, job = response.json()["document_id"], response.json()["job_id"]
                path = f"/v1/ingest/{doc}/jobs/{job}"
                assert client.get(path).json()["stage"] == "parsing"
                release.set()
                for _ in range(200):
                    status = client.get(path).json()
                    if status["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.01)
                assert status["status"] == "completed", status
                listed = client.get("/v1/ingestion-documents").json()["documents"]
                assert listed[0]["indexed"] and listed[0]["latest_job"]["job_id"] == job
                result = client.post("/v1/retrieve", json={"query": "혁신", "method": "bm25"}).json()
                assert result["results"][0]["document_id"] == doc
                retry = client.post(f"/v1/ingest/{doc}/jobs", json={"base_job_id": job, "mode": "rechunk",
                                                                    "settings": {"target_chars": 400, "overlap_chars": 0}})
                assert retry.status_code == 202, retry.text
                new_job = retry.json()["job_id"]
                for _ in range(200):
                    state = client.get(f"/v1/ingest/{doc}/jobs/{new_job}").json()
                    if state["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.01)
                assert state["status"] == "completed", state
                assert parser.calls == 1
                assert state["index_version"] != status["index_version"]
                old = client.post("/v1/retrieve", json={"query": "혁신", "method": "bm25",
                                                        "index_version": status["index_version"]}).json()
                assert old["results"][0]["job_id"] == job
                retry = client.post(f"/v1/ingest/{doc}/jobs", json={"base_job_id": new_job, "mode": "reparse", "settings": {"dpi": 400}})
                assert retry.status_code == 202, retry.text
                for _ in range(200):
                    state = client.get(f"/v1/ingest/{doc}/jobs/{retry.json()['job_id']}").json()
                    if state["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.01)
                assert state["status"] == "completed" and parser.calls == 2
                invalid = client.post(f"/v1/ingest/{doc}/jobs", json={"base_job_id": retry.json()["job_id"],
                                                                      "mode": "rechunk", "settings": {"dpi": 300}})
                assert invalid.status_code == 409
                assert client.delete(f"/v1/ingest/{doc}/jobs/{job}").status_code == 409
                assert client.delete(f"/v1/ingest/{doc}").status_code == 200
                assert not service.storage.document_dir(doc).exists()
                for version in (status["index_version"], state["index_version"]):
                    found = client.post("/v1/retrieve", json={"query": "혁신", "method": "bm25", "index_version": version})
                    assert found.status_code == 200 and found.json()["results"] == []
                    manifest = publisher.get_version(version)
                    assert qdrant.count(manifest["collection"], exact=True).count == 0
            finally:
                release.set()
        qdrant.close()


def test_search_rejects_missing_evidence_and_unconfigured_upload_has_no_legacy_fallback(tmp_path):
    class Parser:
        def parse(self, request):
            element = {"page": 1, "category": "paragraph", "content": {"text": "교육 전략"}}
            return ParseResult("fake", [element], {"elements": [element]})

    def provider(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        storage = ArtifactStore(tmp_path)
        service = IngestionService(storage, Parser(), publisher)
        config = Config(tmp_path, access_key="example", external_processing=True)
        with TestClient(create_app(config, ingestion_service=service), base_url="https://testserver") as client:
            login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
            client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
            job = client.post("/v1/ingest", files={"file": ("report.pdf", pdf_bytes())}).json()
            storage.save_job(job["document_id"], job["job_id"], "elements", [])
            response = client.post("/v1/retrieve", json={"query": "교육", "method": "bm25"})
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "SEARCH_UNAVAILABLE"
        qdrant.close()
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
        client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
        assert not client.get("/v1/processing-options").json()["ingestion_enabled"]
        assert client.post("/v1/ingest", files={"file": ("report.pdf", pdf_bytes())}).status_code == 503
        assert client.post("/v1/documents", files={"file": ("report.pdf", pdf_bytes())}).status_code == 404


def test_content_loss_preserves_active_index_and_user_can_review_and_accept(tmp_path):
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            cell = "<td>교육혁신</td>" if self.calls == 1 else ""
            rows = [{"page": 1, "category": "table", "content": {"html": f"<table><tr><th>목표</th><td>미래인재</td>{cell}</tr></table>"}}]
            return ParseResult("fake", rows, {"elements": rows})
    def provider(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    source = tmp_path / "report.pdf"
    source.write_bytes(pdf_bytes())
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        service = IngestionService(ArtifactStore(tmp_path), Parser(), publisher)
        first = service.ingest(source)
        queued = service.enqueue_reprocess(first["document_id"], first["job_id"], "reparse", {})
        result = service.run_job(first["document_id"], queued["job_id"])
        assert result["status"] == "partial" and result["existing_index_retained"]
        assert result["content_changes"][0]["missing_cell_count"] == 1
        assert publisher.active()["version"] == first["index_version"]
        assert publisher.search("교육혁신", "bm25")["results"]
        stored = service.storage.load_job(first["document_id"], queued["job_id"], "processing")
        service.storage.save_job(first["document_id"], queued["job_id"], "processing", {**stored, "status": "failed", "stage": "indexing"})
        resumed = service.resume_index(first["document_id"], queued["job_id"])
        assert resumed["status"] == "partial" and publisher.active()["version"] == first["index_version"]
        with TestClient(create_app(Config(tmp_path, access_key="example", external_processing=True), ingestion_service=service), base_url="https://testserver") as client:
            login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
            client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
            # The service was invoked before app startup registered the default profile.
            saved = service.storage.load_job(first["document_id"], queued["job_id"], "processing")
            service.storage.save_job(first["document_id"], queued["job_id"], "processing", {**saved, "embedding_contract": publisher.embedding_contract()})
            url = f"/v1/ingest/{first['document_id']}/jobs/{queued['job_id']}"
            review_response = client.get(url + "/content-review")
            assert review_response.status_code == 200
            review = review_response.json()
            assert "교육혁신" in review["pages"][0]["previous"][0]["text"]
            assert "교육혁신" not in review["pages"][0]["current"][0]["text"]
            response = client.post(url + "/accept-content-changes")
            assert response.status_code == 200, response.text
            accepted = response.json()
            assert accepted["status"] == "completed" and accepted["content_changes_accepted"]
        assert publisher.active()["version"] != first["index_version"]
        assert publisher.search("교육혁신", "bm25")["results"] == []
        assert publisher.search("교육혁신", "bm25", version=first["index_version"])["results"]
        qdrant.close()


def test_continued_table_search_returns_all_context_evidence(tmp_path):
    from indexer.contracts import SearchRequest
    from indexer.published_retrieval import PublishedRetrieval
    class Parser:
        def parse(self, request):
            rows = [
                {"page": 1, "category": "caption", "content": {"text": "표 1 사업비"}},
                {"page": 1, "category": "table", "content": {"html": '<table><tr><th>항목</th><th>2026년</th></tr><tr><td>시설</td><td>12</td></tr></table>'}},
                {"page": 2, "category": "caption", "content": {"text": "표 1 (계속)"}},
                {"page": 2, "category": "table", "content": {"html": '<table><tr><td>연구비</td><td>27</td></tr></table>'}},
            ]
            return ParseResult("fake", rows, {"elements": rows})
    def provider(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    source = tmp_path / "report.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as file:
        writer.write(file)
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        storage = ArtifactStore(tmp_path)
        IngestionService(storage, Parser(), publisher).ingest(source)
        hit = PublishedRetrieval(publisher, storage).search(SearchRequest(query="연구비", method="bm25"))["results"][0]
        assert "2026년 / 27" in hit["search_text"]
        assert {url.split("?", 1)[0].rsplit("/", 1)[-1] for url in hit["evidence_urls"]} == {"e4"}
        assert [part["text"] for part in hit["source_segments"]] == ["연구비", "27"]
        assert {url.split("?", 1)[0].rsplit("/", 1)[-1] for url in hit["context_urls"]} == {"e1", "e2", "e3", "e4"}
        assert any('e2?table_cells=' in url for url in hit['context_urls'])
        assert {ref["page"] for ref in hit["source_refs"] if ref["role"] == "evidence"} == {2}
        assert {ref["page"] for ref in hit["source_refs"] if ref["role"] == "context"} == {1, 2}
        assert {ref["page"] for ref in hit["source_refs"]} == {1, 2}
        qdrant.close()


def test_content_review_refuses_accept_if_active_index_changed(tmp_path):
    import pytest
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            text = "교육 연구" if self.calls == 1 else "교육"
            rows = [{"page": 1, "category": "paragraph", "content": {"text": text}}]
            return ParseResult("fake", rows, {"elements": rows})
    def provider(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    source = tmp_path / "report.pdf"
    source.write_bytes(pdf_bytes())
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        service = IngestionService(ArtifactStore(tmp_path), Parser(), publisher)
        first = service.ingest(source)
        job = service.enqueue_reprocess(first["document_id"], first["job_id"], "reparse", {})
        result = service.run_job(first["document_id"], job["job_id"])
        assert result["can_accept_content_changes"] and result["content_changes"][0]["missing_word_count"] == 1
        publisher.rebuild_with_current_profile()
        with pytest.raises(ValueError, match="검색 버전이 변경"):
            service.accept_content_changes(first["document_id"], job["job_id"])
        assert service.storage.load_job(first["document_id"], job["job_id"], "processing")["existing_index_retained"]
        qdrant.close()


def test_worker_recovers_interrupted_embedding_without_reparsing(tmp_path):
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            rows = [{"page": 1, "category": "paragraph", "content": {"text": "대학 교육 계획"}}]
            return ParseResult("fake", rows, {"elements": rows})
    requests = []
    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload["input"])
        return httpx.Response(200, json={"model": payload["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(payload["input"]))]})
    source = tmp_path / "report.pdf"
    source.write_bytes(pdf_bytes())
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        parser = Parser()
        service = IngestionService(ArtifactStore(tmp_path), parser, publisher)
        first = service.ingest(source)
        status = service.storage.load_job(first["document_id"], first["job_id"], "processing")
        service.storage.save_job(first["document_id"], first["job_id"], "processing", {**status, "status": "processing", "stage": "embedding"})
        service.recover()
        recovered = service.storage.load_job(first["document_id"], first["job_id"], "processing")
        assert recovered["status"] == "queued" and recovered["recovery_mode"] == "index"
        assert service.process_next()
        completed = service.storage.load_job(first["document_id"], first["job_id"], "processing")
        assert completed["status"] == "completed" and parser.calls == 1
        assert len(requests) == 1 and publisher.active()["version"] == first["index_version"]
        qdrant.close()


def test_spreadsheet_explicit_source_location_survives_retrieval_and_chat(tmp_path):
    from indexer.chat import RagChat
    from indexer.contracts import ChatRequest, SearchRequest
    from indexer.published_retrieval import PublishedRetrieval
    from test_office_inspection import write_xlsx
    class Parser:
        def parse(self, request):
            rows = [{"page": 1, "category": "paragraph", "content": {"text": "교육 예산 12"},
                     "source_location": {"kind": "xlsx_cell", "sheet": "계획", "cell": "B2"}}]
            return ParseResult("fake", rows, {"elements": rows})
    def provider(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "12입니다 [1]"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"model": payload["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(payload["input"]))]})
    source = tmp_path / "report.xlsx"
    write_xlsx(source)
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        storage = ArtifactStore(tmp_path)
        IngestionService(storage, Parser(), publisher).ingest(source)
        retrieval = PublishedRetrieval(publisher, storage)
        found = retrieval.search(SearchRequest(query="예산", method="bm25"))["results"][0]
        assert any(ref.get("sheet") == "계획" and ref.get("cell") == "B2" and ref["status"] == "unverified" for ref in found["source_refs"])
        answer = RagChat(Config(tmp_path, external_processing=True, llm_api_key="key"), retrieval, http).invoke(ChatRequest(message="예산", method="bm25"))
        assert answer["sources"][0]["unit_id"] == found["unit_id"]
        qdrant.close()


def test_search_and_profile_lookup_continue_while_embedding_and_stale_commit_rejected(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import uuid4

    import pytest
    from indexer.embedding_profiles import EmbeddingProfiles

    started, release = Event(), Event()
    def provider(request):
        body = json.loads(request.content)
        if "slow request" in body["input"]:
            started.set()
            assert release.wait(5)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    def chunk(doc, text):
        return {"chunk_id": str(uuid4()), "document_id": doc, "text": text}
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        publisher = IndexPublisher(tmp_path, QdrantClient(":memory:"),
            EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        profiles = EmbeddingProfiles(tmp_path, publisher, http)
        first = publisher.publish("first", [chunk("first", "교육혁신 계획")])
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = pool.submit(publisher.prepare, "second", [chunk("second", "slow request")])
            try:
                assert started.wait(2)
                searched = pool.submit(publisher.search, "교육혁신", "bm25")
                listed = pool.submit(profiles.list)
                assert searched.result(timeout=2)["index_version"] == first["version"]
                assert pool.submit(publisher.search, "교육혁신", "hybrid").result(timeout=2)["index_version"] == first["version"]
                assert listed.result(timeout=2)[0]["profile_id"] == "p"
                publisher.publish("third", [chunk("third", "다른 문서")])
            finally:
                release.set()
            prepared = pending.result(timeout=5)
        with pytest.raises(ValueError, match="검색 버전이 변경"):
            publisher.commit(prepared)
        assert "third" in publisher.active()["document_ids"]


def test_credit_exhaustion_is_reported_and_resumes_without_reparsing(tmp_path):
    import pytest
    from indexer.ingestion_service import IngestionFailure

    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            row = {"page": 1, "category": "paragraph", "content": {"text": "재사용 가능한 본문"}}
            return ParseResult("fake", [row], {"elements": [row]})
    parser = Parser()
    available = False
    def provider(request):
        body = json.loads(request.content)
        if not available:
            return httpx.Response(403, json={"error": {"code": "api_key_is_not_allowed",
                "message": "API key suspended due to insufficient credit. private-provider-detail"}})
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_bytes())
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        publisher = IndexPublisher(tmp_path, QdrantClient(":memory:"),
            EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        service = IngestionService(ArtifactStore(tmp_path), parser, publisher)
        with pytest.raises(IngestionFailure) as error:
            service.ingest(path)
        doc, job = error.value.document_id, error.value.job_id
        failed = service.storage.load_job(doc, job, "processing")
        assert failed["stage"] == "embedding" and failed["error"]["code"] == "EMBED_CREDIT_EXHAUSTED"
        assert "크레딧" in failed["error"]["message"] and "private-provider-detail" not in failed["error"]["message"]
        with pytest.raises(IngestionFailure):
            service.resume_index(doc, job)
        assert service.storage.load_job(doc, job, "processing")["error"]["code"] == "EMBED_CREDIT_EXHAUSTED"
        available = True
        assert service.resume_index(doc, job)["status"] == "completed"
        assert parser.calls == 1


def test_html_table_search_evidence_is_scoped_after_provider_splits(tmp_path):
    cell = '행일치\n  ' + '세부내용 ' * 95
    html = (f'<table><tr><td style="white-space:pre-wrap">{cell}</td></tr>'
            '<tr><td>다른행식별자</td></tr></table>')

    class Parser:
        def parse(self, request):
            element = {"page": 1, "category": "table", "content": {"html": html}}
            return ParseResult("fake", [element], {})

    def provider(request):
        body = json.loads(request.content)
        if any(len(text.encode()) > 180 for text in body["input"]):
            return httpx.Response(413)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        embedder = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2))
        service = IngestionService(ArtifactStore(tmp_path), Parser(), IndexPublisher(tmp_path, qdrant, embedder))
        config = Config(tmp_path, access_key="example", external_processing=True)
        with TestClient(create_app(config, ingestion_service=service), base_url="https://testserver") as client:
            login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
            client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
            uploaded = client.post("/v1/ingest", files={"file": ("report.pdf", pdf_bytes())})
            assert uploaded.status_code == 200 and uploaded.json()['status'] == 'completed'
            response = client.post("/v1/retrieve", json={"query": "행일치", "method": "bm25", "limit": 1})
            assert response.status_code == 200, response.text
            hit = response.json()["results"][0]
            assert "embedding_split_from" in hit
            [url] = hit["evidence_urls"]
            assert '?table_cells=' in url
            evidence = client.get(url)
            assert evidence.status_code == 200, evidence.text
            [source] = evidence.json()['cells']
            assert source['row'] == 1 and source['column'] == 1
            assert source['text'].startswith('행일치\n  ')
            assert 0 < source['end'] < len(cell)
            assert source['text'] == hit['source_segments'][0]['text'] == cell[:source['end']]
            base = url.split('?')[0]
            assert '다른행식별자' in client.get(base).json()['content']['text']
            assert client.get(base + '?table_cells=99:1:0:4').status_code == 404
        qdrant.close()


def test_search_empty_table_uses_adjacent_caption_as_evidence(tmp_path):
    captions = ["표 1 학비지표", "표 2 교육지표"]

    class Parser:
        def parse(self, request):
            elements = [
                {"page": 1, "category": "caption", "content": {"text": captions[0]}},
                {"page": 1, "category": "table", "content": {"html": "<table><tr><td></td></tr></table>"}},
                {"page": 2, "category": "caption", "content": {"text": captions[1]}},
                {"page": 2, "category": "table",
                 "content": {"html": "<table><tr><td> </td></tr></table>", "text": captions[1]}},
            ]
            return ParseResult("fake", elements, {})

    def provider(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    stream = BytesIO()
    writer.write(stream)
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(":memory:")
        embedder = EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2))
        service = IngestionService(ArtifactStore(tmp_path), Parser(), IndexPublisher(tmp_path, qdrant, embedder))
        config = Config(tmp_path, access_key="example", external_processing=True)
        with TestClient(create_app(config, ingestion_service=service), base_url="https://testserver") as client:
            login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
            client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
            uploaded = client.post("/v1/ingest", files={"file": ("captions.pdf", stream.getvalue())})
            assert uploaded.status_code == 200, uploaded.text
            for page, caption in enumerate(captions, 1):
                response = client.post("/v1/retrieve", json={"query": caption.split()[-1], "method": "bm25"})
                assert response.status_code == 200, response.text
                [hit] = response.json()["results"]
                caption_id, table_id = f"e{page * 2 - 1}", f"e{page * 2}"
                assert hit["element_ids"] == [caption_id]
                assert hit["search_text"] == caption
                assert [(item["element_id"], item["text"]) for item in hit["source_segments"]] == [(caption_id, caption)]
                [evidence_url] = hit["evidence_urls"]
                [context_url] = hit["context_urls"]
                assert client.get(evidence_url).json()["content"]["text"] == caption
                assert context_url.endswith(f"/{table_id}")
                assert client.get(context_url).json()["kind"] == "table"
        qdrant.close()


def test_physical_pdf_pages_are_scoped_to_document_and_pinned_version(tmp_path):
    import pytest
    from indexer.chat import RagChat
    from indexer.contracts import ChatRequest, IndexerError, SearchRequest
    from indexer.pdf_pages import physical_pdf_pages
    from indexer.published_retrieval import PublishedRetrieval

    class Parser:
        calls = 0

        def parse(self, request):
            self.calls += 1
            rows = [{'page': 5, 'category': 'paragraph', 'content': {'text': '외부 환경과 학령인구 감소'}},
                    {'page': 9, 'category': 'paragraph', 'content': {'text': '자율전공 추진 배경과 전공 선택'}},
                    {'page': 8, 'category': 'paragraph', 'content': {'text': '다른 페이지 추진 배경'}}]
            return ParseResult('fake', rows, {'elements': rows})

    requests, embedding_calls = [], []
    def provider(request):
        body = json.loads(request.content)
        if request.url.path.endswith('/chat/completions'):
            requests.append(body)
            return httpx.Response(200, json={'choices': [{'message': {'content': '확인한 내용입니다. [1]'}}]})
        embedding_calls.append(body)
        return httpx.Response(200, json={'model': body['model'], 'data': [
            {'index': i, 'embedding': [1.0, 0.5]} for i in range(len(body['input']))]})

    assert physical_pdf_pages('PDF 9쪽과 5쪽을 비교하고 본문 18쪽도 언급') == [9, 5]
    assert physical_pdf_pages('인쇄된 PDF 9쪽과 2026년 예산') == []
    source = tmp_path / 'report.pdf'
    writer = PdfWriter()
    for _ in range(9):
        writer.add_blank_page(width=100, height=100)
    with source.open('wb') as file:
        writer.write(file)
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(
            http, 'https://example.test/v1', 'key', EmbeddingProfile('p', 'doc', 'query', 2)))
        storage = ArtifactStore(tmp_path)
        parser = Parser()
        service = IngestionService(storage, parser, publisher)
        first = service.ingest(source)
        doc, version = first['document_id'], first['index_version']
        retrieval = PublishedRetrieval(publisher, storage)
        request = SearchRequest(query='추진 배경', method='bm25', document_id=doc, index_version=version, top_k=5)
        calls_before_page_search = len(embedding_calls)
        found = retrieval.search_pdf_pages(request, [9, 5])
        assert len(embedding_calls) == calls_before_page_search
        assert found['index_version'] == version and found['page_scope']['requested_pages'] == [9, 5]
        assert {hit['page'] for hit in found['results']} == {9, 5}
        assert all(hit['document_id'] == doc for hit in found['results'])
        assert all(hit['evidence_urls'] for hit in found['results'])
        missing = retrieval.search_pdf_pages(request, [7])
        assert not missing['results'] and missing['page_scope']['pages_without_chunks'] == [7]
        mixed = retrieval.search_pdf_pages(request, [9, 7])
        assert mixed['page_scope']['pages_without_chunks'] == [7]
        assert mixed['page_scope']['source_page_completeness'] == 'unknown'
        rerank_calls = []

        class Ranker:
            def rerank(self, query, candidates, limit):
                rerank_calls.append((query, len(candidates), limit))
                return candidates[:limit]

        publisher.reranker_factory = lambda _: Ranker()
        reranked = retrieval.search_pdf_pages(request.model_copy(update={'rerank': True}), [9, 5])
        assert reranked['reranked'] and len(rerank_calls) == 1
        assert {hit['page'] for hit in reranked['results']} == {5, 9}
        one = retrieval.search_pdf_pages(request.model_copy(update={'rerank': True, 'top_k': 1}), [9, 5])
        assert len(one['results']) == 1 and rerank_calls[-1][2] == 2
        assert one['search_counts']['reranked_count'] == 2 and one['search_counts']['result_limit'] == 1
        publisher.reranker_factory = None
        chat = RagChat(Config(tmp_path, external_processing=True, llm_api_key='key'), retrieval, http)
        result = chat.invoke(ChatRequest(message='PDF 9쪽과 5쪽을 비교해 줘', method='bm25', document_id=doc,
                                         index_version=version, top_k=5))
        assert result['page_scope']['requested_pages'] == [9, 5]
        payload = json.loads(requests[-1]['messages'][-1]['content'])
        assert payload['PDF_물리쪽_조회']['requested_pages'] == [9, 5]
        assert {ref['page'] for hit in result['sources'] for ref in hit['source_refs'] if ref['role'] == 'evidence'} == {5, 9}
        config = Config(tmp_path, access_key='key', external_processing=True)
        with TestClient(create_app(config, ingestion_service=service), base_url='https://testserver') as client:
            login = client.post('/v1/auth/login', json={'access_key': 'key'}, headers={'Origin': 'https://testserver'})
            client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
            response = client.get(f'/v1/ingest/{doc}/pdf-pages', params=[('pages', 9), ('pages', 5),
                                  ('query', '추진 배경'), ('index_version', version)])
            assert response.status_code == 200, response.text
            assert {hit['page'] for hit in response.json()['results']} == {5, 9}
            response = client.post('/v1/retrieve', json={'query': 'PDF 9쪽과 5쪽의 배경', 'method': 'bm25',
                                   'document_id': doc, 'index_version': version, 'limit': 5})
            assert response.status_code == 200, response.text
            assert response.json()['page_scope']['selection_method'] == 'bm25_within_pages'
            assert {hit['page'] for hit in response.json()['results']} == {5, 9}
        local_config = Config(tmp_path, access_key='key', external_processing=False)
        with TestClient(create_app(local_config, ingestion_service=service), base_url='https://testserver') as client:
            login = client.post('/v1/auth/login', json={'access_key': 'key'}, headers={'Origin': 'https://testserver'})
            client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
            response = client.post('/v1/retrieve', json={'query': 'PDF 9쪽', 'method': 'hybrid',
                                   'document_id': doc, 'index_version': version})
            assert response.status_code == 200, response.text
            assert response.json()['method'] == 'pdf_page_bm25'
        settings = storage.load_job(doc, first['job_id'], 'processing')['settings']
        queued = service.enqueue_reprocess(doc, first['job_id'], 'rechunk', settings)
        replay = service.run_job(doc, queued['job_id'])
        assert replay['status'] == 'completed' and parser.calls == 1
        assert retrieval.search_pdf_pages(request, [9, 5])['index_version'] == version
        second_path = tmp_path / 'second.pdf'
        writer.add_metadata({'/Title': '다른 문서'})
        with second_path.open('wb') as file:
            writer.write(file)
        second = service.ingest(second_path)
        assert second['document_id'] != doc
        with pytest.raises(IndexerError, match='PDF 문서를 하나 선택'):
            retrieval.search_pdf_pages(SearchRequest(query='배경', method='bm25'), [5])
        with pytest.raises(IndexerError, match='검색 버전에 없습니다'):
            retrieval.search_pdf_pages(SearchRequest(query='배경', method='bm25',
                                       document_id=second['document_id'], index_version=version), [5])
        old = retrieval.search_pdf_pages(request, [9, 5])
        assert old['index_version'] == version and all(hit['document_id'] == doc for hit in old['results'])
        qdrant.close()


def test_pdf_page_references_do_not_assume_printed_page_numbers():
    from indexer.pdf_pages import physical_pdf_pages

    for query in ('PDF 9쪽', 'PDF의 9페이지', 'PDF 문서의 9쪽', 'PDF page 9', 'PDF p. 9', 'PDF 페이지 9'):
        assert physical_pdf_pages(query) == [9]
    assert physical_pdf_pages('PDF 9쪽과 5쪽 및 3페이지') == [9, 5, 3]
    for query in ('본문 18쪽', '인쇄된 PDF 9쪽', '2026년 9월', 'PDF 문서 2026년'):
        assert physical_pdf_pages(query) == []
