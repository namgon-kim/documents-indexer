import httpx
import pytest
from indexer.parsing.contracts import ParserConfig, ParseRequest
from indexer.parsing.registry import ParserRegistry
from indexer.parsing.settings import parse_options
from indexer.parsing.upstage_adapter import UpstageParser


def test_options_and_conflicting_keys():
    assert parse_options('words=true;"hint=a;b"') == {"words": "true", "hint": "a;b"}
    for value in ("model=other", "words=true;words=false", "broken"):
        with pytest.raises(ValueError):
            parse_options(value)


def test_provider_registry_and_upstage_request(tmp_path):
    document = tmp_path / "example.pdf"
    document.write_bytes(b"%PDF-1.4")
    def respond(request):
        assert request.url.path == "/v1/document-digitization"
        assert b'name="mode"' in request.content and b'enhanced' in request.content
        assert b'name="dpi"' in request.content and b'400' in request.content
        assert b'name="words"' in request.content
        return httpx.Response(200, json={"elements": [{"page": 1, "content": {"text": "본문"}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        registry = ParserRegistry()
        registry.register("upstage", lambda config: UpstageParser(config, client))
        config = ParserConfig("upstage", "https://example.test/v1", "secret", "model", "enhanced", {"words": "true"})
        result = registry.create(config).parse(ParseRequest(document, "pdf", dpi=400))
        assert result.elements[0]["content"]["text"] == "본문"
        with pytest.raises(ValueError):
            registry.create(ParserConfig("unknown", "https://example.test", ""))


def test_long_document_accepts_http_download_only_from_configured_proxy(tmp_path):
    document = tmp_path / "long.pdf"
    document.write_bytes(b"%PDF-1.4")
    events = []
    def respond(request):
        if request.method == "POST":
            assert request.url == "http://proxy.test:8080/v1/document-digitization/async"
            return httpx.Response(200, json={"request_id": "id_1"})
        if request.url.path.endswith("/requests/id_1"):
            return httpx.Response(200, json={"status": "completed", "batches": [{
                "start_page": 1, "end_page": 101, "status": "completed",
                "download_url": "http://proxy.test:8080/results/1"}]})
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"elements": [{"page": 1, "content": {"text": "본문"}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        parser = UpstageParser(ParserConfig("upstage", "http://proxy.test:8080/v1", "secret", "model", "enhanced"), client)
        result = parser.parse(ParseRequest(document, "pdf", page_count=101,
                                         persist=lambda kind, value: events.append((kind, value))))
    assert result.elements[0]["page"] == 1
    assert [item[0] for item in events] == ["started", "request_id", "batch"]


def test_registered_parser_is_available_to_settings_and_runtime(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from indexer.config import Config
    from indexer.parsing.registry import PROVIDERS

    from apps.api.main import create_app

    class AlternateParser:
        def parse(self, request):
            raise AssertionError("not called")
    alternate = AlternateParser()
    monkeypatch.setitem(PROVIDERS, "alternate", lambda config, client, remote_timeout, job_timeout: alternate)
    with TestClient(create_app(Config(tmp_path, parser_provider="alternate", access_key="example")),
                    base_url="https://testserver") as client:
        assert client.app.state.ingestion.parser is alternate


def test_async_submission_rejection_and_ambiguous_failure(tmp_path):
    from indexer.contracts import IndexerError
    path = tmp_path / "long.pdf"
    path.write_bytes(b"%PDF-1.4")
    config = ParserConfig("upstage", "https://example.test/v1", "secret")
    for status, expected in [(401, "PARSER_REQUEST_REJECTED"), (413, "PARSER_REQUEST_REJECTED"), (503, "REMOTE_SUBMISSION_UNKNOWN")]:
        with httpx.Client(transport=httpx.MockTransport(lambda request, status=status: httpx.Response(status))) as client:
            with pytest.raises(IndexerError) as error:
                UpstageParser(config, client).parse(ParseRequest(path, "pdf", page_count=101))
            assert error.value.code == expected


def test_poll_transient_errors_are_retried_without_new_submission(tmp_path, monkeypatch):
    path = tmp_path / "long.pdf"
    path.write_bytes(b"%PDF-1.4")
    calls = {"post": 0, "poll": 0}
    def respond(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(200, json={"request_id": "id_1"})
        if request.url.path.endswith("/requests/id_1"):
            calls["poll"] += 1
            if calls["poll"] == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"status": "completed", "batches": [{"start_page": 1, "end_page": 101,
                "status": "completed", "download_url": "https://example.test/result"}]})
        return httpx.Response(200, json={"elements": [{"page": 1, "content": {"text": "본문"}}]})
    monkeypatch.setattr("indexer.parsing.upstage_adapter.time.sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        UpstageParser(ParserConfig("upstage", "https://example.test/v1", "key"), client).parse(ParseRequest(path, "pdf", page_count=101))
    assert calls == {"post": 1, "poll": 2}


def test_sync_timeout_is_independent_from_remote_timeout(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-1.4")
    def respond(request):
        assert request.extensions["timeout"]["read"] == 900
        raise httpx.ReadTimeout("timeout", request=request)
    from indexer.contracts import IndexerError
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(IndexerError) as error:
            UpstageParser(ParserConfig("upstage", "https://example.test/v1", "key"), client, remote_timeout=90).parse(ParseRequest(path, "pdf"))
        assert error.value.code == "PARSER_SYNC_TIMEOUT" and "비용" in error.value.message


def test_async_connect_failure_is_not_ambiguous(tmp_path):
    from indexer.contracts import IndexerError
    path = tmp_path / "long.pdf"
    path.write_bytes(b"%PDF-1.4")
    def respond(request):
        raise httpx.ConnectError("connection failed", request=request)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(IndexerError) as error:
            UpstageParser(ParserConfig("upstage", "https://example.test/v1", "key"), client).parse(ParseRequest(path, "pdf", page_count=101))
        assert error.value.code == "PARSER_CONNECTION_FAILED"


@pytest.mark.parametrize("status", [400, 401, 404, 413, 429, 500])
def test_sync_parser_http_error_preserves_status_without_response_body(tmp_path, status):
    from indexer.contracts import IndexerError
    path = tmp_path / "document.xlsx"
    path.write_bytes(b"document")
    def respond(request):
        return httpx.Response(status, json={"message": "private document content or secret"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client, pytest.raises(IndexerError) as error:
        UpstageParser(ParserConfig("upstage", "https://example.test/v1", "key"), client).parse(ParseRequest(path, "xlsx"))
    assert error.value.code == "PARSER_HTTP_ERROR"
    assert f"HTTP {status}" in error.value.message
    assert "private" not in error.value.message
    assert error.value.__cause__.response.status_code == status


def test_parser_http_status_saved_and_logged_without_provider_response(tmp_path, caplog):
    import json

    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionFailure, IngestionService
    from pypdf import PdfWriter

    path = tmp_path / "document.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    def respond(request):
        return httpx.Response(415, json={"message": "sensitive response"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        service = IngestionService(ArtifactStore(tmp_path), UpstageParser(
            ParserConfig("upstage", "https://example.test/v1", "secret"), client))
        with pytest.raises(IngestionFailure) as error:
            service.ingest(path)
        status = service.storage.load_job(error.value.document_id, error.value.job_id, "processing")
    assert status["error"]["code"] == "PARSER_HTTP_ERROR" and status["error"]["http_status"] == 415
    event = json.loads(next(record.message for record in caplog.records if 'ingestion_failed' in record.message))
    assert event["http_status"] == 415 and event["error_code"] == "PARSER_HTTP_ERROR"
    assert "sensitive response" not in caplog.text and "secret" not in caplog.text


def test_hwp_async_without_page_count_uses_provider_total(tmp_path):
    path = tmp_path / "document.hwp"
    path.write_bytes(b"dummy")
    events = []
    progress = []
    def respond(request):
        if request.method == "POST":
            assert request.url.path.endswith("/document-digitization/async")
            return httpx.Response(200, json={"request_id": "hwp_1"})
        if request.url.path.endswith("/requests/hwp_1"):
            return httpx.Response(200, json={"status": "completed", "total_pages": 120, "batches": [
                {"start_page": 1, "end_page": 100, "status": "completed", "download_url": "https://example.test/batch1"},
                {"start_page": 101, "end_page": 120, "status": "completed", "download_url": "https://example.test/batch2"}]})
        page = 1 if request.url.path.endswith("batch1") else 101
        return httpx.Response(200, json={"elements": [{"page": page, "content": {"text": "본문"}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        parser = UpstageParser(ParserConfig("upstage", "https://example.test/v1", "key"), client)
        result = parser.parse(ParseRequest(path, "hwp", persist=lambda event, data: events.append((event, data)),
                                         progress=lambda done, total: progress.append((done, total))))
    assert result.raw["total_pages"] == 120 and not result.unavailable and not result.incomplete
    assert [item["page"] for item in result.elements] == [1, 101]
    assert [event for event, _ in events] == ["started", "request_id", "batch", "batch"]
    assert progress[-1] == (120, 120)


def test_hwp_async_resumes_unavailable_batch_without_resubmission(tmp_path, monkeypatch):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    path = tmp_path / "document.hwp"
    path.write_bytes(b"dummy")
    monkeypatch.setattr("indexer.ingestion_graph.inspect_document", lambda *args: None)
    monkeypatch.setattr("indexer.parsing.upstage_adapter.time.sleep", lambda _: None)
    calls = {"post": 0, "first": 0}
    recovered = False
    def respond(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(200, json={"request_id": "hwp_1"})
        if request.url.path.endswith("/requests/hwp_1"):
            return httpx.Response(200, json={"status": "completed", "total_pages": 120, "batches": [
                {"start_page": 1, "end_page": 100, "status": "completed", "download_url": "https://example.test/batch1"},
                {"start_page": 101, "end_page": 120, "status": "completed", "download_url": "https://example.test/batch2"}]})
        if request.url.path.endswith("batch1"):
            calls["first"] += 1
            return httpx.Response(200, json={"elements": [{"page": 1, "category": "paragraph", "content": {"text": "본문"}}]})
        return httpx.Response(200, json={"elements": [{"page": 101, "category": "paragraph", "content": {"text": "복구"}}]}) if recovered else httpx.Response(503)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        service = IngestionService(ArtifactStore(tmp_path), UpstageParser(ParserConfig("upstage", "https://example.test/v1", "key"), client))
        first = service.ingest(path)
        assert first["status"] == "partial" and first["can_resume"]
        assert first["coverage"]["unavailable_pages"] == list(range(101, 121))
        assert first["coverage"]["provider_total_pages"] == 120 and first["coverage"]["unknown_scope"]
        recovered = True
        service.resume_processing(first["document_id"], first["job_id"])
        second = service.run_job(first["document_id"], first["job_id"])
        assert second["status"] == "parsed" and second["coverage"]["missing_pages"] == []
        assert calls == {"post": 1, "first": 1}


def test_hwp_async_does_not_treat_missing_page_total_as_complete(tmp_path, monkeypatch):
    from indexer.contracts import IndexerError
    path = tmp_path / "document.hwp"
    path.write_bytes(b"dummy")
    monkeypatch.setattr("indexer.parsing.upstage_adapter.time.sleep", lambda _: None)
    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "hwp_1"})
        return httpx.Response(200, json={"status": "completed", "batches": []})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client, pytest.raises(IndexerError) as error:
        UpstageParser(ParserConfig("upstage", "https://example.test/v1", "key"), client).parse(ParseRequest(path, "hwp"))
    assert error.value.code == "PARSE_RESULTS_UNAVAILABLE"


def test_hwp_async_provider_page_limit_is_reported(tmp_path):
    from indexer.contracts import IndexerError
    path = tmp_path / "document.hwp"
    path.write_bytes(b"dummy")
    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "hwp_1"})
        return httpx.Response(200, json={"status": "failed", "total_pages": 2001, "batches": []})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client, pytest.raises(IndexerError) as error:
        UpstageParser(ParserConfig("upstage", "https://example.test/v1", "key"), client).parse(ParseRequest(path, "hwp"))
    assert error.value.code == "PARSER_PAGE_LIMIT"


@pytest.mark.parametrize("download_url,expected_auth", [
    ("https://proxy.test/results/1", "Bearer secret"),
    ("https://proxy.test:444/results/1", None),
    ("https://downloads.test/results/1", None),
])
def test_result_download_sends_key_only_to_same_origin(tmp_path, download_url, expected_auth):
    document = tmp_path / "long.pdf"
    document.write_bytes(b"%PDF-1.4")

    def respond(request):
        if request.method == "POST":
            assert request.headers["authorization"] == "Bearer secret"
            return httpx.Response(200, json={"request_id": "id_1"})
        if request.url.path.endswith("/requests/id_1"):
            assert request.headers["authorization"] == "Bearer secret"
            return httpx.Response(200, json={"status": "completed", "batches": [{
                "start_page": 1, "end_page": 101, "status": "completed", "download_url": download_url}]})
        assert request.headers.get("authorization") == expected_auth
        return httpx.Response(200, json={"elements": [{"page": 1, "content": {"text": "본문"}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        parser = UpstageParser(ParserConfig("upstage", "https://proxy.test/v1", "secret", "model", "enhanced"), client)
        assert parser.parse(ParseRequest(document, "pdf", page_count=101)).elements
