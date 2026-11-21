import json
import stat

from fastapi.testclient import TestClient
from indexer.config import Config

from apps.api.main import create_app


def test_parser_profiles_save_select_edit_and_keep_secrets(tmp_path):
    config = Config(tmp_path, access_key="example", parser_provider="upstage", parser_api_key="existing-key")
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
        client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
        result = client.put("/v1/parser-profiles/upstage", json={"name": "기존 파서"})
        assert result.status_code == 200 and result.json()["active"]
        assert "existing-key" not in result.text
        result = client.put("/v1/parser-profiles/paddle", json={"name": "문서 OCR", "parser_provider": "paddle_vl",
            "parser_base_url": "https://proxy.test/ai/stack", "parser_api_key": "new-secret"})
        assert result.status_code == 200 and not result.json()["active"]
        assert "new-secret" not in result.text
        result = client.post("/v1/parser-profiles/paddle/activate", json={})
        assert result.status_code == 200 and not result.json()["restart_required"]
        assert client.app.state.ingestion.parser.config.base_url == "https://proxy.test/ai/stack"
        assert client.app.state.ingestion.parser.config.api_key == "new-secret"
        profiles = client.get("/v1/parser-profiles").json()["profiles"]
        assert next(item for item in profiles if item["profile_id"] == "paddle")["active"]
        assert "new-secret" not in json.dumps(profiles)
        assert client.put("/v1/parser-profiles/paddle", json={"name": "수정"}).status_code == 200
        assert client.post("/v1/parser-profiles/paddle/activate", json={}).status_code == 200
        assert client.app.state.ingestion.parser.config.api_key == "new-secret"
        assert client.post("/v1/parser-profiles/upstage/activate", json={}).status_code == 200
        assert client.app.state.ingestion.parser.config.api_key == "existing-key"
        assert client.delete("/v1/parser-profiles/paddle").status_code == 200
    assert stat.S_IMODE((tmp_path / "parser-profiles.json").stat().st_mode) == 0o600


def test_parser_profiles_require_session(tmp_path):
    with TestClient(create_app(Config(tmp_path, access_key="example")), base_url="https://testserver") as client:
        assert client.get("/v1/parser-profiles").status_code == 401


def test_editing_selected_parser_keeps_selection_and_reactivation_does_not_duplicate(tmp_path):
    config = Config(tmp_path, access_key="example", parser_provider="upstage", parser_api_key="env-key")
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
        client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
        assert client.put("/v1/parser-profiles/a", json={"name": "A"}).status_code == 200
        assert client.post("/v1/parser-profiles/a/activate", json={}).status_code == 200
        edited = client.put("/v1/parser-profiles/a", json={"name": "A", "parser_options": "timeout=120"})
        assert edited.status_code == 200
        assert edited.json()["active"] and not edited.json()["applied"]
        listing = client.get("/v1/parser-profiles").json()
        assert next(row for row in listing["profiles"] if row["profile_id"] == "a")["active"]
        assert client.post("/v1/parser-profiles/a/activate", json={}).status_code == 200
        profiles = client.get("/v1/parser-profiles").json()["profiles"]
        assert len(profiles) == 1 and profiles[0]["active"] and profiles[0]["applied"]
        assert client.app.state.ingestion.parser.config.options == {"timeout": "120"}


def test_environment_parser_can_be_reselected_and_current_connection_is_preserved(tmp_path):
    from indexer.settings import save_overrides
    config = Config(tmp_path, access_key='example', parser_provider='upstage', parser_api_key='env-secret',
                    parser_base_url='http://env-parser.test/v1')
    save_overrides(config, {'parser_provider': 'paddle_vl', 'parser_api_key': 'paddle-secret',
                            'parser_base_url': 'http://paddle.test'})
    with TestClient(create_app(config), base_url='https://testserver') as client:
        login = client.post('/v1/auth/login', json={'access_key': 'example'}, headers={'Origin': 'https://testserver'})
        client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
        listing = client.get('/v1/parser-profiles')
        assert listing.json()['environment']['parser_provider'] == 'upstage'
        assert not listing.json()['environment']['active']
        assert 'env-secret' not in listing.text and 'paddle-secret' not in listing.text
        assert client.put('/v1/parser-profiles/paddle', json={'name': 'Paddle 현재 연결'}).status_code == 200
        assert client.post('/v1/parser-profiles/environment/activate', json={}).status_code == 200
        assert client.app.state.ingestion.parser.config.api_key == 'env-secret'
        profiles = client.get('/v1/parser-profiles').json()['profiles']
        saved = next(row for row in profiles if row['parser_provider'] == 'paddle_vl')
        assert client.post(f"/v1/parser-profiles/{saved['profile_id']}/activate", json={}).status_code == 200
        assert client.app.state.ingestion.parser.config.api_key == 'paddle-secret'
        assert client.put('/v1/parser-profiles/environment', json={'name': 'invalid'}).status_code == 422


def test_queued_and_resumed_jobs_keep_original_parser(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService
    from indexer.parsing.contracts import ParserConfig, ParseResult
    from pypdf import PdfWriter

    calls = []
    class Parser:
        def __init__(self, cfg):
            self.config = cfg
        def parse(self, request):
            calls.append(self.config.base_url)
            row = {"page": 1, "category": "paragraph", "content": {"text": "본문"}}
            return ParseResult(self.config.provider, [row], {"elements": [row]})
    def factory(snapshot):
        return Parser(ParserConfig(**snapshot["config"]))
    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as stream:
        writer.write(stream)
    store = ArtifactStore(tmp_path)
    first = IngestionService(store, Parser(ParserConfig("upstage", "https://old.test/v1", "old-secret")), parser_factory=factory)
    job = first.enqueue(source)
    second = IngestionService(store, Parser(ParserConfig("paddle_vl", "https://new.test", "new-secret")), parser_factory=factory)
    result = second.run_job(job["document_id"], job["job_id"])
    assert result["status"] == "parsed" and calls == ["https://old.test/v1"]
    snapshot = store.load_job(job["document_id"], job["job_id"], "parser_config")
    listed = store.list_jobs(job['document_id'])[0]
    assert listed['parser_provider'] == 'upstage'
    assert 'old-secret' not in json.dumps(listed)
    assert snapshot["config"]["api_key"] == "old-secret"
    assert "old-secret" not in json.dumps(result)
    status = store.load_job(job["document_id"], job["job_id"], "processing")
    store.save_provider_event(job["document_id"], job["job_id"], "initial", "request_id", {"request_id": "saved-request"})
    store.save_job(job["document_id"], job["job_id"], "processing", {**status, "status": "failed", "can_resume": True, "recovery_mode": "initial"})
    resumed = second.resume_processing(job["document_id"], job["job_id"])
    assert resumed["status"] == "queued"
    assert second._job_parser(job["document_id"], job["job_id"], resumed).config.base_url == "https://old.test/v1"
    assert stat.S_IMODE((store.document_dir(job["document_id"]) / "jobs" / job["job_id"] / "parser_config.json").stat().st_mode) == 0o600


def test_switch_parser_does_not_wait_for_inflight_job(tmp_path, monkeypatch):
    import threading

    from indexer.parsing.registry import PROVIDERS
    from pypdf import PdfWriter

    started, release = threading.Event(), threading.Event()
    class BlockingParser:
        def __init__(self, cfg):
            self.config = cfg
        def parse(self, request):
            started.set()
            assert release.wait(5)
            raise ValueError("test ends processing")
    monkeypatch.setitem(PROVIDERS, "upstage", lambda config, client, remote_timeout, job_timeout: BlockingParser(config))
    config = Config(tmp_path, access_key="example", parser_provider="upstage", parser_api_key="key")
    source = tmp_path / "example.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as stream:
        writer.write(stream)
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
        client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
        old_service = client.app.state.ingestion
        old_service.enqueue(source)
        try:
            assert started.wait(3)
            assert client.put("/v1/parser-profiles/paddle", json={"name": "Paddle", "parser_provider": "paddle_vl",
                "parser_base_url": "https://proxy.test"}).status_code == 200
            response = client.post("/v1/parser-profiles/paddle/activate", json={})
            assert response.status_code == 200
            assert client.app.state.ingestion.parser.config.provider == "paddle_vl"
            assert old_service.parser.config.provider == "upstage"
        finally:
            release.set()
