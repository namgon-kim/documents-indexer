import json
import stat

import pytest
from fastapi.testclient import TestClient
from indexer.config import Config
from indexer.settings import apply_overrides

from apps.api.main import create_app


def login(client):
    origin = "https://testserver"
    response = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": origin})
    assert response.status_code == 200
    client.headers.update({"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]})


def test_settings_persist_hide_secrets_and_apply_after_restart(tmp_path):
    config = Config(tmp_path, access_key="example", parser_api_key="env-secret", llm_model="base-model")
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login(client)
        original = client.get("/v1/settings").json()
        assert original["values"]["parser_api_key"] == {"configured": True}
        assert "env-secret" not in json.dumps(original)
        response = client.put("/v1/settings", json={"values": {"llm_model": "new-model", "llm_models": ["base-model"], "parser_api_key": "new-secret", "default_target_chars": 800, "default_overlap_chars": 80, "default_dpi": 300}})
        assert response.status_code == 200, response.text
        assert not response.json()["restart_required"]
        assert "new-secret" not in response.text
        assert client.get("/v1/chat/options").json()["default_model"] == "new-model"
        assert client.get("/v1/processing-options").json()["defaults"]["dpi"] == 300
    assert stat.S_IMODE((tmp_path / "server-settings.json").stat().st_mode) == 0o600
    updated = apply_overrides(config)
    assert updated.llm_model == "new-model" and updated.parser_api_key == "new-secret"
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login(client)
        assert client.get("/v1/chat/options").json()["models"] == ["new-model", "base-model"]
        assert not client.get("/v1/settings").json()["restart_required"]
        defaults = client.get("/v1/processing-options").json()["defaults"]
        assert (defaults["target_chars"], defaults["overlap_chars"], defaults["dpi"]) == (800, 80, 300)
        presets = client.get("/v1/processing-options").json()["chunk_profile_defaults"]
        assert presets == {"general": {"target_chars": 800, "overlap_chars": 80},
                           "report": {"target_chars": 1200, "overlap_chars": 200},
                           "manual": {"target_chars": 800, "overlap_chars": 100},
                           "scanned": {"target_chars": 1200, "overlap_chars": 200},
                           "spreadsheet": {"target_chars": 1500, "overlap_chars": 0},
                           "slides": {"target_chars": 1000, "overlap_chars": 100}}
        response = client.put("/v1/settings", json={"reset": ["parser_api_key"]})
        assert response.status_code == 200
    assert apply_overrides(config).parser_api_key == "env-secret"


def test_settings_require_session_and_validate_without_partial_save(tmp_path):
    config = Config(tmp_path, access_key="example")
    with TestClient(create_app(config), base_url="https://testserver") as client:
        assert client.get("/v1/settings").status_code == 401
        login(client)
        result = client.put("/v1/settings", json={"values": {"max_pages": -1, "llm_model": "new-model"}})
        assert result.status_code == 422
        assert client.get("/v1/settings").json()["values"]["llm_model"] == config.llm_model
        assert client.get("/v1/settings", headers={"Authorization": "Bearer token"}).status_code == 403


def test_qdrant_address_defaults_to_http_for_env_and_saved_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QDRANT_URL", "192.0.2.1:6333")
    config = Config.from_env()
    assert config.qdrant_url == "http://192.0.2.1:6333"
    from indexer.settings import save_overrides
    save_overrides(config, {"qdrant_url": "qdrant.example:6333"})
    assert apply_overrides(config).qdrant_url == "http://qdrant.example:6333"
    save_overrides(config, {"qdrant_url": "https://qdrant.example:6333"})
    assert apply_overrides(config).qdrant_url == "https://qdrant.example:6333"


def test_model_list_is_selectable_immediately_after_save(tmp_path):
    config = Config(tmp_path, access_key="example", llm_model="base")
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login(client)
        result = client.put("/v1/settings", json={"values": {"llm_models": ["another"]}})
        assert result.status_code == 200 and not result.json()["restart_required"]
        assert client.get("/v1/chat/options").json()["models"] == ["base", "another"]
        assert not client.get("/v1/settings").json()["restart_required"]


def test_ingestion_settings_are_secret_and_applied_to_actual_clients(tmp_path):
    config = Config(tmp_path, access_key="example")
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login(client)
        response = client.put("/v1/settings", json={"values": {
            "parser_provider": "upstage", "parser_base_url": "http://parser.example/v1",
            "parser_api_key": "parser-secret", "parser_options": "coordinates=true",
            "embedding_base_url": "http://embedding.example/v1", "embedding_api_key": "embedding-secret",
            "embedding_document_model": "document-model", "embedding_query_model": "query-model",
            "embedding_dimensions": 768, "reranker_provider": "cohere", "reranker_api_key": "rerank-secret"}})
        assert response.status_code == 200 and not response.json()["restart_required"]
        publisher = client.app.state.index_publisher
        assert publisher.embedder.profile.document_model == "document-model"
        assert publisher.embedder.profile.dimensions == 768
        assert publisher.reranker_factory is not None
        assert all(secret not in response.text for secret in ("parser-secret", "embedding-secret", "rerank-secret"))
    with TestClient(create_app(config), base_url="https://testserver") as client:
        publisher = client.app.state.index_publisher
        assert publisher.embedder.base_url == "http://embedding.example/v1"
        assert publisher.embedder.profile.dimensions == 768
        assert publisher.embedder.profile.document_model == "document-model"
        assert publisher.reranker_factory is not None
        assert client.app.state.ingestion is not None


def test_settings_expose_fixed_choices_and_rerank_availability(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    class Publisher:
        reranker_factory = None

    publisher = Publisher()
    service = IngestionService(ArtifactStore(tmp_path), object(), publisher)
    with TestClient(create_app(Config(tmp_path, access_key="example", external_processing=True), ingestion_service=service),
                    base_url="https://testserver") as client:
        login(client)
        choices = client.get("/v1/settings").json()["choices"]
        assert choices["reranker_provider"] == ["disabled", "cohere"]
        assert choices["default_ocr_policy"] == ["automatic", "force"]
        assert choices["parser_provider"][0] == "" and "upstage" in choices["parser_provider"]
        assert client.get("/v1/processing-options").json()["rerank_available"] is False
        publisher.reranker_factory = lambda load_text: None
        assert client.get("/v1/processing-options").json()["rerank_available"] is False
    with TestClient(create_app(Config(tmp_path, access_key="example"), ingestion_service=service), base_url="https://testserver") as client:
        login(client)
        assert client.get("/v1/processing-options").json()["rerank_available"] is False


def test_processing_options_expose_only_local_search_when_external_disabled(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    class Publisher:
        reranker_factory = lambda self, load_text: None
    service = IngestionService(ArtifactStore(tmp_path), object(), Publisher())
    with TestClient(create_app(Config(tmp_path, access_key="example", external_processing=False), ingestion_service=service),
                    base_url="https://testserver") as client:
        login(client)
        options = client.get("/v1/processing-options").json()
        assert options["search_methods"] == ["bm25"] and not options["rerank_available"]
        assert client.post("/v1/retrieve", json={"query": "질문", "method": "hybrid"}).status_code == 403


def test_no_auth_proxy_reranker_can_be_selected_without_api_key(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    class Publisher:
        reranker_factory = lambda self, load_text: None
    service = IngestionService(ArtifactStore(tmp_path), object(), Publisher())
    config = Config(tmp_path, access_key="example", external_processing=True, reranker_base_url="http://internal-proxy/v2")
    with TestClient(create_app(config, ingestion_service=service), base_url="https://testserver") as client:
        login(client)
        assert client.get("/v1/processing-options").json()["rerank_available"]


def test_connection_changes_are_applied_without_restart_and_invalid_changes_are_atomic(tmp_path):
    config = Config(tmp_path, access_key="example", parser_provider="upstage", external_processing=True)
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login(client)
        original = client.app.state.index_publisher
        response = client.put("/v1/settings", json={"values": {
            "parser_base_url": "http://new-parser/v1", "embedding_document_model": "new-embedding",
            "llm_base_url": "http://new-llm/v1", "default_target_chars": 800, "default_overlap_chars": 80}})
        assert response.status_code == 200, response.text
        assert not response.json()["restart_required"]
        assert client.app.state.ingestion.parser.config.base_url == "http://new-parser/v1"
        assert client.app.state.index_publisher.embedder.profile.document_model == "new-embedding"
        assert client.app.state.index_publisher.lock is original.lock
        assert client.app.state.index_publisher.search_lock is original.search_lock
        assert client.app.state.index_publisher.qdrant is original.qdrant
        assert client.app.state.chat.config.llm_base_url == "http://new-llm/v1"
        assert client.get("/v1/processing-options").json()["defaults"]["target_chars"] == 800
        result = client.put("/v1/settings", json={"values": {"data_dir": str(tmp_path / "other"), "llm_model": "must-not-save"}})
        assert result.status_code == 409
        assert client.get("/v1/settings").json()["values"]["llm_model"] != "must-not-save"


def test_added_chat_model_uses_saved_api_key_without_restart(tmp_path):
    from types import SimpleNamespace

    import httpx
    from indexer.chat import RagChat
    from indexer.contracts import ChatRequest

    config = Config(tmp_path, access_key='example', parser_provider='upstage', external_processing=True)
    with TestClient(create_app(config), base_url='https://testserver') as client:
        login(client)
        result = client.put('/v1/settings', json={'values': {'llm_api_key': 'updated-secret',
                           'llm_base_url': 'http://proxy.test/v1', 'llm_models': ['added-model']}})
        assert result.status_code == 200 and 'updated-secret' not in result.text
        def provider(request):
            assert request.headers['authorization'] == 'Bearer updated-secret'
            assert request.url.path == '/v1/chat/completions'
            assert json.loads(request.content)['model'] == 'added-model'
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '답변'}, 'finish_reason': 'stop'}]})
        hit = {'filename': '문서', 'source_refs': [], 'search_text': '근거'}
        retrieval = SimpleNamespace(search=lambda _: {'results': [hit], 'index_version': 'version'})
        with httpx.Client(transport=httpx.MockTransport(provider)) as http:
            result = RagChat(client.app.state.published_chat.config, retrieval, http).invoke(
                ChatRequest(message='질문', model='added-model', method='bm25'))
        assert result['answer'] == '답변'


def test_can_enable_parser_without_restart(tmp_path):
    with TestClient(create_app(Config(tmp_path, access_key="example")), base_url="https://testserver") as client:
        login(client)
        assert not client.get("/v1/processing-options").json()["ingestion_enabled"]
        result = client.put("/v1/settings", json={"values": {"parser_provider": "upstage"}})
        assert result.status_code == 200, result.text
        assert client.get("/v1/processing-options").json()["ingestion_enabled"]


def test_rerank_limit_and_evidence_budget_validation(tmp_path):
    from indexer.settings import ServerSettings, values
    from pydantic import ValidationError

    defaults = values(Config(tmp_path))
    assert ServerSettings.model_validate({**defaults, "retrieval_top_n": 50, "rerank_top_n": 20}).rerank_top_n == 20
    with pytest.raises(ValidationError):
        ServerSettings.model_validate({**defaults, "retrieval_top_n": 50, "rerank_top_n": 30})
    with pytest.raises(ValidationError):
        ServerSettings.model_validate({**defaults, "chat_evidence_bytes": 4095})
