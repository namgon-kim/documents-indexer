from io import BytesIO

import httpx
import pytest
from indexer.artifact_store import ArtifactStore
from indexer.ingestion_graph import make_graph
from indexer.ingestion_service import IngestionService
from indexer.parsing.contracts import ParseResult
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


def pdf_pages(count):
    writer = PdfWriter()
    for _ in range(count):
        page = writer.add_blank_page(width=100, height=100)
        content = DecodedStreamObject()
        content.set_data(b"q Q")
        page[NameObject("/Contents")] = writer._add_object(content)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def element(page, text=None):
    return {"page": page, "category": "paragraph", "content": {"text": text or f"내용 {page}"}}


@pytest.mark.parametrize(("total", "missing", "expected_pages", "mode"), [
    (100, [5, 12, 34, 38, 69, 90], 6, "partial"),
    (10, [1, 3, 5, 7, 9], 5, "partial"),
    (10, [1, 2, 3, 4, 5, 6], 10, "full"),
])
def test_page_retry_threshold_and_mapping(tmp_path, total, missing, expected_pages, mode):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(total))
    class Parser:
        supports_pdf_page_subset = True
        def __init__(self):
            self.calls = []
        def parse(self, request):
            count = len(PdfReader(request.path).pages)
            self.calls.append((count, request.ocr))
            if len(self.calls) == 1:
                rows = [element(page) for page in range(1, total + 1) if page not in missing]
            else:
                rows = [element(page, f"다시 읽기 {page}") for page in range(1, count + 1)]
            return ParseResult("fake", rows, {"elements": rows})
    parser = Parser()
    state = make_graph(parser).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert parser.calls == [(total, "force"), (expected_pages, "force")]
    assert state["coverage"]["missing_pages"] == []
    assert state["coverage"]["initial_missing_pages"] == missing
    assert state["coverage"]["retry_page_limit"] == 200
    assert state["coverage"]["attempts"][-1]["mode"] == mode
    assert sorted({row["page"] for row in state["elements"]}) == list(range(1, total + 1))
    if mode == "partial":
        assert next(row for row in state["elements"] if row["page"] == missing[-1])["content"]["text"] == f"다시 읽기 {len(missing)}"


def test_failed_automatic_retry_records_partial_without_losing_first_result(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(4))
    class Parser:
        supports_pdf_page_subset = True
        calls = 0
        def parse(self, request):
            self.calls += 1
            if self.calls == 2:
                raise httpx.ReadTimeout("request outcome unknown")
            rows = [element(page) for page in (1, 3, 4)]
            return ParseResult("fake", rows, {"elements": rows})
    parser = Parser()
    storage = ArtifactStore(tmp_path)
    result = IngestionService(storage, parser).ingest(path)
    assert result["status"] == "partial" and result["coverage"]["missing_pages"] == [2]
    attempts = storage.load_job(result["document_id"], result["job_id"], "parse_attempts")
    assert attempts[0]["result"]["elements"][0]["page"] == 1
    assert attempts[1]["status"] == "failed" and parser.calls == 2
    assert len(storage.load_job(result["document_id"], result["job_id"], "parser_elements")) == 3


def test_full_retry_over_budget_is_not_silently_replaced_with_partial(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(6))
    class Parser:
        supports_pdf_page_subset = True
        calls = 0
        def parse(self, request):
            self.calls += 1
            rows = [element(1), element(2)]
            return ParseResult("fake", rows, {"elements": rows})
    parser = Parser()
    state = make_graph(parser, retry_pages=5).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert parser.calls == 1
    assert state["coverage"]["missing_pages"] == [3, 4, 5, 6]
    assert state["coverage"]["attempts"][-1]["status"] == "skipped"
    assert state["coverage"]["initial_missing_pages"] == [3, 4, 5, 6]
    assert state["coverage"]["retry_page_limit"] == 5


def test_incomplete_reparse_does_not_replace_previous_search_version(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(4))
    class Parser:
        supports_pdf_page_subset = True
        calls = 0
        def parse(self, request):
            self.calls += 1
            if self.calls == 3:
                raise httpx.ReadTimeout("unknown")
            pages = (1, 2, 3, 4) if self.calls == 1 else (1, 3, 4)
            rows = [element(page) for page in pages]
            return ParseResult("fake", rows, {"elements": rows})
    class Publisher:
        manifest = None
        calls = 0
        def active(self):
            return self.manifest
        def deleted_documents(self):
            return set()
        def publish(self, document_id, chunks):
            self.calls += 1
            self.manifest = {"version": "complete", "document_ids": [document_id]}
            return self.manifest
    parser, publisher = Parser(), Publisher()
    service = IngestionService(ArtifactStore(tmp_path), parser, publisher)
    first = service.ingest(path)
    assert first["status"] == "completed"
    new = service.enqueue_reprocess(first["document_id"], first["job_id"], "reparse", {})
    result = service.run_job(first["document_id"], new["job_id"])
    assert result["status"] == "partial" and result["coverage"]["missing_pages"] == [2]
    assert result["existing_index_retained"] and publisher.calls == 1
    assert publisher.active()["version"] == "complete"
    assert result["can_accept_partial"]
    accepted = service.accept_content_changes(first["document_id"], new["job_id"], accept_partial=True)
    assert accepted["status"] == "partial" and accepted["partial_accepted"]
    assert not accepted["existing_index_retained"] and publisher.calls == 2
    assert accepted["coverage"]["missing_pages"] == [2]


def test_page_limit_rejects_pdf_before_paid_request(tmp_path):
    from indexer.contracts import IndexerError

    path = tmp_path / "large.pdf"
    path.write_bytes(pdf_pages(101))
    class Parser:
        max_pdf_pages = 100
        calls = 0
        def parse(self, request):
            self.calls += 1
            raise AssertionError("provider should not be called")
    parser = Parser()
    with pytest.raises(IndexerError) as error:
        make_graph(parser).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert error.value.code == "PARSER_PAGE_LIMIT" and parser.calls == 0


def test_out_of_range_retry_result_cannot_hide_missing_page(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(4))
    class Parser:
        supports_pdf_page_subset = True
        calls = 0
        def parse(self, request):
            self.calls += 1
            rows = [element(1), element(3), element(4)] if self.calls == 1 else [element(9)]
            return ParseResult("fake", rows, {"elements": rows})
    parser = Parser()
    state = make_graph(parser).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert state["coverage"]["missing_pages"] == [2]
    assert state["coverage"]["attempts"][-1]["status"] == "failed"


def test_detected_blank_page_is_not_retried(tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    content = DecodedStreamObject()
    content.set_data(b"q Q")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.add_blank_page(width=100, height=100)
    path = tmp_path / "blank.pdf"
    with path.open("wb") as stream:
        writer.write(stream)
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            rows = [element(1)]
            return ParseResult("fake", rows, {"elements": rows})
    parser = Parser()
    result = make_graph(parser).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert result["coverage"]["blank_pages"] == [2]
    assert result["coverage"]["missing_pages"] == [] and parser.calls == 1


def test_long_document_resumes_existing_request_and_saved_pages(tmp_path, monkeypatch):
    from indexer.ingestion_service import IngestionFailure
    from indexer.parsing.contracts import ParserConfig
    from indexer.parsing.upstage_adapter import UpstageParser

    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_pages(101))
    calls = {"submitted": 0, "poll": 0, "first_download": 0}
    recovered = False
    def handler(request):
        if request.method == "POST":
            assert request.url.path.endswith("/document-digitization/async")
            calls["submitted"] += 1
            return httpx.Response(200, json={"request_id": "request_123"})
        if request.url.path.endswith("/requests/request_123"):
            calls["poll"] += 1
            if not recovered and calls["poll"] > 1:
                return httpx.Response(503)
            batches = [{"start_page": 1, "end_page": 100, "status": "completed", "download_url": "https://downloads.example/first"}]
            if recovered:
                batches.append({"start_page": 101, "end_page": 101, "status": "completed", "download_url": "https://downloads.example/last"})
            return httpx.Response(200, json={"status": "completed" if recovered else "processing", "batches": batches})
        assert "authorization" not in request.headers
        if request.url.path == "/first":
            calls["first_download"] += 1
            return httpx.Response(200, json={"elements": [element(page) for page in range(1, 101)]})
        return httpx.Response(200, json={"elements": [element(101)]})
    monkeypatch.setattr("indexer.parsing.upstage_adapter.time.sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        parser = UpstageParser(ParserConfig("upstage", "https://parser.example/v1", "secret", "model", "enhanced"), client)
        storage = ArtifactStore(tmp_path)
        service = IngestionService(storage, parser)
        with pytest.raises(IngestionFailure) as failure:
            service.ingest(path)
        doc, job = failure.value.document_id, failure.value.job_id
        failed = storage.load_job(doc, job, "processing")
        assert failed["can_resume"] and failed["progress"] == {"completed": 100, "total": 101}
        checkpoint = storage.provider_checkpoint(doc, job, "initial")
        assert checkpoint["request_id"] == "request_123" and len(checkpoint["batches"]) == 1
        recovered = True
        service.resume_processing(doc, job)
        result = service.run_job(doc, job)
        assert result["status"] == "parsed" and result["coverage"]["missing_pages"] == []
        assert calls["submitted"] == 1 and calls["first_download"] == 1
        assert len(storage.load_job(doc, job, "parser_elements")) == 101


def test_interrupted_submission_without_receipt_is_not_repeated(tmp_path):
    from indexer.ingestion_service import IngestionFailure
    from indexer.parsing.contracts import ParserConfig
    from indexer.parsing.upstage_adapter import UpstageParser

    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_pages(101))
    calls = []
    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("connection lost")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = IngestionService(ArtifactStore(tmp_path), UpstageParser(
            ParserConfig("upstage", "https://parser.example/v1", "secret", "model", "enhanced"), client))
        with pytest.raises(IngestionFailure) as failure:
            service.ingest(path)
        doc, job = failure.value.document_id, failure.value.job_id
        assert service.storage.provider_checkpoint(doc, job, "initial")["started"]
        assert not service.storage.load_job(doc, job, "processing")["can_resume"]
        with pytest.raises(ValueError):
            service.resume_processing(doc, job)
        assert calls == ["POST"]


def test_long_full_retry_resumes_without_resubmitting(tmp_path):
    from indexer.ingestion_service import IngestionFailure
    from indexer.parsing.contracts import ParserConfig
    from indexer.parsing.upstage_adapter import UpstageParser

    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_pages(120))
    calls = {"post": 0}
    recovered = False
    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(200, json={"request_id": f"request_{calls['post']}"})
        if request.url.path.endswith("/requests/request_1"):
            return httpx.Response(200, json={"status": "completed", "batches": [
                {"start_page": 1, "end_page": 120, "status": "completed", "download_url": "https://downloads.example/initial"}]})
        if request.url.path.endswith("/requests/request_2"):
            if not recovered:
                return httpx.Response(503)
            return httpx.Response(200, json={"status": "completed", "batches": [
                {"start_page": 1, "end_page": 120, "status": "completed", "download_url": "https://downloads.example/retry"}]})
        assert "authorization" not in request.headers
        end = 50 if request.url.path == "/initial" else 120
        return httpx.Response(200, json={"elements": [element(page) for page in range(1, end + 1)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        storage = ArtifactStore(tmp_path)
        parser = UpstageParser(ParserConfig("upstage", "https://parser.example/v1", "secret", "model", "enhanced"), client)
        service = IngestionService(storage, parser)
        with pytest.raises(IngestionFailure) as failure:
            service.ingest(path)
        doc, job = failure.value.document_id, failure.value.job_id
        status = storage.load_job(doc, job, "processing")
        assert status["can_resume"] and status["recovery_mode"] == "retry"
        assert status["coverage"]["missing_pages"] == list(range(51, 121))
        recovered = True
        storage.save_job(doc, job, "processing", {**status, "status": "processing"})
        service.recover()
        assert storage.load_job(doc, job, "processing")["status"] == "queued"
        result = service.run_job(doc, job)
        assert result["status"] == "parsed" and result["coverage"]["missing_pages"] == []
        assert result["coverage"]["attempts"][-1]["mode"] == "full"
        assert calls["post"] == 2


def test_unavailable_batch_can_be_fetched_later_without_new_submission(tmp_path, monkeypatch):
    from indexer.parsing.contracts import ParserConfig
    from indexer.parsing.upstage_adapter import UpstageParser

    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_pages(101))
    calls = {"post": 0, "first": 0}
    restored = False
    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(200, json={"request_id": "request_1"})
        if request.url.path.endswith("/requests/request_1"):
            return httpx.Response(200, json={"status": "completed", "batches": [
                {"start_page": 1, "end_page": 100, "status": "completed", "download_url": "https://downloads.example/first"},
                {"start_page": 101, "end_page": 101, "status": "completed", "download_url": "https://downloads.example/last"}]})
        if request.url.path == "/first":
            calls["first"] += 1
            return httpx.Response(200, json={"elements": [element(page) for page in range(1, 101)]})
        return httpx.Response(200, json={"elements": [element(101)]}) if restored else httpx.Response(503)
    monkeypatch.setattr("indexer.parsing.upstage_adapter.time.sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = IngestionService(ArtifactStore(tmp_path), UpstageParser(
            ParserConfig("upstage", "https://parser.example/v1", "secret", "model", "enhanced"), client))
        result = service.ingest(path)
        assert result["status"] == "partial" and result["coverage"]["unavailable_pages"] == [101]
        assert result["can_resume"] and calls["post"] == 1
        restored = True
        service.resume_processing(result["document_id"], result["job_id"])
        result = service.run_job(result["document_id"], result["job_id"])
        assert result["status"] == "parsed" and result["coverage"]["missing_pages"] == []
        assert calls == {"post": 1, "first": 1}


def test_long_document_index_failure_resumes_index_not_parsing(tmp_path):
    from indexer.ingestion_service import IngestionFailure
    from indexer.parsing.contracts import ParserConfig
    from indexer.parsing.upstage_adapter import UpstageParser

    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_pages(101))
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "request_1"})
        if request.url.path.endswith("/requests/request_1"):
            return httpx.Response(200, json={"status": "completed", "batches": [
                {"start_page": 1, "end_page": 101, "status": "completed", "download_url": "https://downloads.example/result"}]})
        return httpx.Response(200, json={"elements": [element(page) for page in range(1, 102)]})
    class Publisher:
        calls = 0
        def active(self): return None
        def publish(self, document_id, chunks):
            self.calls += 1
            if self.calls == 1: raise ValueError("index unavailable")
            return {"version": "ready"}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        publisher = Publisher()
        service = IngestionService(ArtifactStore(tmp_path), UpstageParser(
            ParserConfig("upstage", "https://parser.example/v1", "secret", "model", "enhanced"), client), publisher)
        with pytest.raises(IngestionFailure) as failure:
            service.ingest(path)
        doc, job = failure.value.document_id, failure.value.job_id
        status = service.storage.load_job(doc, job, "processing")
        assert status["stage"] == "indexing" and not status["can_resume"]
        before = list(calls)
        assert service.resume_index(doc, job)["status"] == "completed"
        assert calls == before and publisher.calls == 2


def test_confirmed_missing_retries_while_unavailable_is_retrieved_later_without_repeat(tmp_path):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(pdf_pages(8))
    class Parser:
        supports_pdf_page_subset = True
        restored = False
        retry_submissions = 0
        initial_submissions = 0
        def parse(self, request):
            if request.page_count == 8:
                if not request.checkpoint.get("request_id"):
                    self.initial_submissions += 1
                    request.persist("request_id", {"request_id": "original"})
                rows = [element(page) for page in range(1, 9) if page not in ({2, 4} if not self.restored else {2})]
                return ParseResult("fake", rows, {"elements": rows}, failed={2},
                                   unavailable={4} if not self.restored else set(), incomplete=not self.restored)
            self.retry_submissions += 1
            assert request.page_count == 1 and len(PdfReader(request.path).pages) == 1
            return ParseResult("fake", [element(1, "복구된 이쪽")], {"elements": [element(1, "복구된 이쪽")]})
    parser = Parser()
    service = IngestionService(ArtifactStore(tmp_path), parser)
    initial = service.ingest(path)
    assert initial["status"] == "partial" and initial["coverage"]["unavailable_pages"] == [4]
    assert initial["coverage"]["missing_pages"] == [4]
    assert initial["recovery_mode"] == "initial"
    assert initial["coverage"]["attempts"][-1]["pages"] == [2]
    assert parser.initial_submissions == 1 and parser.retry_submissions == 1
    parser.restored = True
    service.resume_processing(initial["document_id"], initial["job_id"])
    result = service.run_job(initial["document_id"], initial["job_id"])
    assert result["status"] == "parsed" and result["coverage"]["missing_pages"] == []
    assert result["coverage"]["attempts"][-1]["reused"] is True
    rows = service.storage.load_job(initial["document_id"], initial["job_id"], "parser_elements")
    assert any(row["page"] == 2 and row["content"]["text"] == "복구된 이쪽" for row in rows)
    assert parser.initial_submissions == 1 and parser.retry_submissions == 1


def test_confirmed_majority_defers_full_retry_until_unavailable_results_resolved(tmp_path):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(pdf_pages(10))
    class Parser:
        supports_pdf_page_subset = True
        def __init__(self): self.calls = 0
        def parse(self, request):
            self.calls += 1
            rows = [element(page) for page in (8, 9, 10)]
            return ParseResult("fake", rows, {"elements": rows}, failed={1, 2, 3, 4, 5, 6}, unavailable={7}, incomplete=True)
    parser = Parser()
    state = make_graph(parser).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert parser.calls == 1
    assert state["coverage"]["attempts"][-1]["mode"] == "full"
    assert state["coverage"]["attempts"][-1]["reason"] == "results_unavailable"


def test_mixed_missing_uses_confirmed_count_for_50_percent_threshold(tmp_path):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(pdf_pages(10))
    class Parser:
        supports_pdf_page_subset = True
        def __init__(self): self.calls = []
        def parse(self, request):
            self.calls.append(request.page_count)
            if len(self.calls) == 1:
                rows = [element(page) for page in (8, 9, 10)]
                return ParseResult("fake", rows, {"elements": rows}, failed={1, 2, 3, 4, 5}, unavailable={6, 7}, incomplete=True)
            return ParseResult("fake", [element(page) for page in range(1, 6)], {})
    parser = Parser()
    state = make_graph(parser).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert parser.calls == [10, 5]
    assert state["coverage"]["missing_pages"] == [6, 7]
    assert state["coverage"]["attempts"][-1]["mode"] == "partial"


def test_initial_and_retry_unavailable_are_resumed_from_their_own_receipts(tmp_path):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(pdf_pages(8))
    class Parser:
        supports_pdf_page_subset = True
        initial_ready = False
        retry_ready = False
        initial_submissions = 0
        retry_submissions = 0
        def parse(self, request):
            if request.page_count == 8:
                if not request.checkpoint.get("request_id"):
                    self.initial_submissions += 1
                    request.persist("request_id", {"request_id": "initial"})
                rows = [element(page) for page in range(1, 9) if page not in ({2, 3, 4} if not self.initial_ready else {2, 3})]
                return ParseResult("fake", rows, {"elements": rows}, failed={2, 3},
                    unavailable=set() if self.initial_ready else {4}, incomplete=not self.initial_ready)
            if not request.checkpoint.get("request_id"):
                self.retry_submissions += 1
                request.persist("request_id", {"request_id": "retry"})
            rows = [element(page, f"재시도 {page}") for page in (1, 2) if page == 1 or self.retry_ready]
            return ParseResult("fake", rows, {"elements": rows}, unavailable=set() if self.retry_ready else {2},
                               incomplete=not self.retry_ready)
    parser = Parser()
    service = IngestionService(ArtifactStore(tmp_path), parser)
    first = service.ingest(path)
    assert first["coverage"]["missing_pages"] == [3, 4] and first["recovery_mode"] == "initial"
    parser.initial_ready = True
    service.resume_processing(first["document_id"], first["job_id"])
    second = service.run_job(first["document_id"], first["job_id"])
    assert second["coverage"]["missing_pages"] == [3] and second["recovery_mode"] == "retry"
    assert parser.retry_submissions == 1
    parser.retry_ready = True
    service.resume_processing(first["document_id"], first["job_id"])
    final = service.run_job(first["document_id"], first["job_id"])
    assert final["status"] == "parsed" and not final["coverage"]["missing_pages"]
    assert parser.initial_submissions == parser.retry_submissions == 1


def test_ambiguous_partial_retry_is_not_resubmitted_when_initial_result_resumes(tmp_path):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(pdf_pages(8))
    class Parser:
        supports_pdf_page_subset = True
        restored = False
        retry_submissions = 0
        def parse(self, request):
            if request.page_count == 8:
                if not request.checkpoint.get("request_id"):
                    request.persist("request_id", {"request_id": "original"})
                rows = [element(page) for page in range(1, 9) if page not in ({2, 4} if not self.restored else {2})]
                return ParseResult("fake", rows, {"elements": rows}, failed={2},
                                   unavailable=set() if self.restored else {4}, incomplete=not self.restored)
            self.retry_submissions += 1
            raise httpx.ReadTimeout("접수 여부 알 수 없음")
    parser = Parser()
    service = IngestionService(ArtifactStore(tmp_path), parser)
    first = service.ingest(path)
    assert first["coverage"]["missing_pages"] == [2, 4] and parser.retry_submissions == 1
    parser.restored = True
    service.resume_processing(first["document_id"], first["job_id"])
    result = service.run_job(first["document_id"], first["job_id"])
    assert result["coverage"]["missing_pages"] == [2]
    assert result["coverage"]["attempts"][-1]["reason"] == "retry_outcome_unknown"
    assert parser.retry_submissions == 1


def test_interruption_after_saved_partial_result_does_not_resubmit(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(4))
    class Parser:
        supports_pdf_page_subset = True
        calls = 0
        def parse(self, request):
            self.calls += 1
            if self.calls == 1:
                rows = [element(page) for page in (1, 3, 4)]
                return ParseResult("fake", rows, {"elements": rows}, failed={2})
            return ParseResult("fake", [element(1, "복구")], {"elements": [element(1, "복구")]})
    class InterruptedService(IngestionService):
        interrupted = False
        def _provider_event(self, state, attempt, event, data):
            super()._provider_event(state, attempt, event, data)
            if attempt == "retry" and event == "result" and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt()
    parser = Parser()
    service = InterruptedService(ArtifactStore(tmp_path), parser)
    queued = service.enqueue(path)
    with pytest.raises(KeyboardInterrupt):
        service.run_job(queued["document_id"], queued["job_id"])
    service.recover()
    recovered = service.storage.load_job(queued["document_id"], queued["job_id"], "processing")
    assert recovered["status"] == "queued" and recovered["recovery_mode"] == "retry"
    result = service.run_job(queued["document_id"], queued["job_id"])
    assert result["status"] == "parsed" and not result["coverage"]["missing_pages"]
    assert result["coverage"]["attempts"][-1]["reused"] and parser.calls == 2


def test_duplicate_requires_explicit_new_document(tmp_path):
    from indexer.ingestion_service import DuplicateDocument
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(1))
    service = IngestionService(ArtifactStore(tmp_path), object())
    first = service.enqueue(path)
    with pytest.raises(DuplicateDocument) as error:
        service.enqueue(path)
    assert error.value.document["document_id"] == first["document_id"]
    assert service.enqueue(path, allow_duplicate=True)["document_id"] != first["document_id"]


def test_hwp_invalid_header_rejected_before_parser(tmp_path):
    from indexer.contracts import IndexerError
    path = tmp_path / "document.hwp"
    path.write_bytes(b"not an hwp")
    class Parser:
        def parse(self, request):
            raise AssertionError("invalid file must not reach parser")
    with pytest.raises(IndexerError):
        make_graph(Parser()).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})


def test_provider_incomplete_is_distinct_from_unknown_page_scope(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(1))
    class Parser:
        def parse(self, request):
            rows = [element(1)]
            return ParseResult("fake", rows, {"elements": rows}, incomplete=True)
    result = make_graph(Parser()).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert result["coverage"]["missing_pages"] == []
    assert result["coverage"]["unknown_scope"] is False
    assert result["coverage"]["provider_incomplete"] is True


def test_textless_figure_is_not_treated_as_a_blank_page(tmp_path):
    path = tmp_path / "figure.pdf"
    path.write_bytes(pdf_pages(1))
    class Parser:
        def parse(self, request):
            rows = [{"page": 1, "category": "figure", "content": {"html": "<img src='diagram.png'>"}}]
            return ParseResult("fake", rows, {"elements": rows})
    result = make_graph(Parser(), retry_pages=0).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert result["coverage"]["missing_pages"] == [1]
    assert result["coverage"]["visual_without_text_pages"] == [1]
    assert result["coverage"]["blank_pages"] == []


def test_hwpx_missing_sections_rejected_before_parser(tmp_path):
    from zipfile import ZipFile

    from indexer.contracts import IndexerError
    path = tmp_path / "document.hwpx"
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/content.hpf", "")
    class Parser:
        def parse(self, request):
            raise AssertionError("invalid file must not reach parser")
    with pytest.raises(IndexerError):
        make_graph(Parser()).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})


def test_resume_published_partial_forks_job_and_preserves_old_evidence(tmp_path):
    from indexer.dense_index import EmbeddingClient, EmbeddingProfile
    from indexer.index_publication import IndexPublisher
    from qdrant_client import QdrantClient

    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(3))
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            if self.calls == 1:
                request.persist("started", {})
                request.persist("request_id", {"request_id": "existing_request"})
                rows = [element(1, "첫 페이지"), element(3, "기존 셋째 페이지")]
                return ParseResult("fake", rows, {"elements": rows}, unavailable={2}, incomplete=True)
            assert request.checkpoint["request_id"] == "existing_request"
            rows = [element(1, "첫 페이지"), element(2, "복구된 둘째 페이지")]
            if self.calls == 2:
                return ParseResult("fake", rows, {"elements": rows}, unavailable={3}, incomplete=True)
            rows.append(element(3, "기존 셋째 페이지"))
            return ParseResult("fake", rows, {"elements": rows})
    def embedding(request):
        import json
        data = json.loads(request.content)
        return httpx.Response(200, json={"model": data["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(data["input"]))]})
    parser = Parser()
    with httpx.Client(transport=httpx.MockTransport(embedding)) as http:
        publisher = IndexPublisher(tmp_path, QdrantClient(":memory:"),
            EmbeddingClient(http, "https://embed.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        # The job is published through a different profile pointer; the default publisher
        # must still detect its references when deciding whether to fork.
        default_publisher = IndexPublisher(tmp_path, publisher.qdrant, publisher.embedder,
                                           active_name="active-other.json", lock=publisher.lock,
                                           search_lock=publisher.search_lock)
        storage = ArtifactStore(tmp_path)
        service = IngestionService(storage, parser, default_publisher)
        service.publisher_for = lambda *args, **kwargs: publisher
        original = service.ingest(path)
        doc, old_job = original["document_id"], original["job_id"]
        assert original["status"] == "partial" and original["can_resume"]
        old_elements = storage.load_job(doc, old_job, "elements")
        old_chunks = storage.load_job(doc, old_job, "chunks")
        assert publisher.active() is None and original["pending_review"]
        original = service.accept_content_changes(doc, old_job, accept_partial=True)
        old_version = original["index_version"]
        queued = service.resume_processing(doc, old_job)
        assert queued["job_id"] != old_job and queued["progress"] is None
        resumed = service.run_job(doc, queued["job_id"])
        assert parser.calls == 2 and resumed["status"] == "partial" and resumed["existing_index_retained"]
        assert resumed["can_resume"] and publisher.active()["version"] == old_version
        assert storage.load_job(doc, old_job, "elements") == old_elements
        next_queued = service.resume_processing(doc, queued["job_id"])
        assert next_queued["job_id"] == queued["job_id"]
        resumed = service.run_job(doc, next_queued["job_id"])
        assert parser.calls == 3 and resumed["status"] == "completed"
        assert storage.load_job(doc, old_job, "elements") == old_elements
        assert storage.load_job(doc, old_job, "chunks") == old_chunks
        assert publisher.get_version(old_version) is not None
        old_hits = publisher.search("셋째", "bm25", version=old_version)["results"]
        assert old_hits and old_hits[0]["job_id"] == old_job
        assert old_elements[1]["text"] == "기존 셋째 페이지"


def test_terminated_remote_pages_do_not_offer_resume(tmp_path, monkeypatch):
    from indexer.parsing.contracts import ParserConfig
    from indexer.parsing.upstage_adapter import UpstageParser
    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_pages(101))
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "request_1"})
        if request.url.path.endswith("/requests/request_1"):
            return httpx.Response(200, json={"status": "failed", "batches": [
                {"start_page": 1, "end_page": 100, "status": "completed", "download_url": "https://downloads.example/first"}]})
        return httpx.Response(200, json={"elements": [element(page) for page in range(1, 101)]})
    monkeypatch.setattr("indexer.parsing.upstage_adapter.time.sleep", lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = IngestionService(ArtifactStore(tmp_path), UpstageParser(
            ParserConfig("upstage", "https://parser.example/v1", "secret"), client))
        result = service.ingest(path)
        assert result["coverage"]["terminal_unavailable_pages"] == [101]
        assert result["coverage"]["provider_incomplete"] and not result["can_resume"]


def test_expired_request_disables_resume_and_preserves_checkpoint(tmp_path):
    from indexer.parsing.contracts import ParserConfig
    from indexer.parsing.upstage_adapter import UpstageParser
    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_pages(101))
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "request_1"})
        return httpx.Response(410)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = IngestionService(ArtifactStore(tmp_path), UpstageParser(
            ParserConfig("upstage", "https://parser.example/v1", "secret"), client))
        from indexer.ingestion_service import IngestionFailure
        with pytest.raises(IngestionFailure) as error:
            service.ingest(path)
        job = service.storage.load_job(error.value.document_id, error.value.job_id, "processing")
        assert job["error"]["code"] == "PARSE_REQUEST_TERMINATED" and not job["can_resume"]
        assert service.storage.provider_checkpoint(error.value.document_id, error.value.job_id, "initial")["request_id"] == "request_1"


def test_recovery_forks_already_published_processing_job(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(1))
    class Publisher:
        def job_is_indexed(self, doc, job):
            return True
    service = IngestionService(ArtifactStore(tmp_path), object())
    first = service.enqueue(path)
    doc, job = first["document_id"], first["job_id"]
    service.storage.save_provider_event(doc, job, "initial", "request_id", {"request_id": "stored"})
    service.storage.save_job(doc, job, "elements", [{"element_id": "e1", "text": "보존"}])
    service.storage.save_job(doc, job, "processing", {**first, "status": "processing", "stage": "parsing"})
    service.publisher = Publisher()
    service.recover()
    jobs = service.storage.list_jobs(doc)
    assert len(jobs) == 2
    resumed = next(item for item in jobs if item["job_id"] != job)
    assert resumed["status"] == "queued" and resumed["recovery_mode"] == "initial"
    assert service.storage.load_job(doc, job, "elements")[0]["text"] == "보존"
    assert service.storage.provider_checkpoint(doc, resumed["job_id"], "initial")["request_id"] == "stored"


def test_resume_clears_visible_progress_without_losing_previous_comparison(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_pages(1))
    service = IngestionService(ArtifactStore(tmp_path), object())
    first = service.enqueue(path)
    doc, job = first["document_id"], first["job_id"]
    service.storage.save_provider_event(doc, job, "initial", "request_id", {"request_id": "stored"})
    changes = [{"page": 1, "missing_word_count": 2}]
    service.storage.save_job(doc, job, "processing", {**first, "status": "partial", "can_resume": True,
        "recovery_mode": "initial", "progress": {"completed": 55, "total": 100}, "content_changes": changes,
        "error": {"message": "이전 오류"}})
    resumed = service.resume_processing(doc, job)
    assert resumed["stage"] == "queued" and resumed["progress"] is None
    assert resumed["content_changes"] == changes and not resumed["can_accept_content_changes"]
    assert resumed["error"] is None and resumed["previous_error"]["message"] == "이전 오류"


def test_unsupported_forced_ocr_does_not_misreport_or_repeat_visual_page(tmp_path):
    path = tmp_path / 'scan.pdf'
    path.write_bytes(pdf_pages(1))
    class Parser:
        supports_force_ocr = False
        supports_pdf_page_subset = True
        def __init__(self):
            self.calls = []
        def parse(self, request):
            self.calls.append(request.ocr)
            return ParseResult('fake', [{'page': 1, 'category': 'figure', 'content': {'text': ''}}], {'warnings': ['그림 텍스트 미확인']}, incomplete=True)
    parser = Parser()
    state = make_graph(parser).invoke({'path': str(path), 'requested_profile': 'scanned', 'document_id': 'doc', 'job_id': 'job'})
    assert parser.calls == ['auto']
    assert state['effective_ocr'] == 'auto'
    assert state['coverage']['missing_pages'] == [1]
    assert any(a.get('reason') == 'ocr_force_unsupported' for a in state['coverage']['attempts'])
    assert not any(a.get('reason') == 'retry_limit' for a in state['coverage']['attempts'])
    assert state['coverage']['warnings'] == ['그림 텍스트 미확인']


def test_parser_without_force_still_retries_missing_pages(tmp_path):
    path = tmp_path / 'scan.pdf'
    path.write_bytes(pdf_pages(3))
    class Parser:
        supports_force_ocr = False
        supports_pdf_page_subset = True
        def __init__(self):
            self.calls = []
        def parse(self, request):
            self.calls.append(request.ocr)
            pages = (1, 3) if len(self.calls) == 1 else (1,)
            return ParseResult('fake', [element(page) for page in pages], {})
    parser = Parser()
    state = make_graph(parser).invoke({'path': str(path), 'document_id': 'doc', 'job_id': 'job'})
    assert parser.calls == ['auto', 'auto']
    assert state['coverage']['missing_pages'] == []
