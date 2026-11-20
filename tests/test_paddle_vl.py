import base64
import json

import httpx
import pytest
from indexer.chunking import normalize_elements
from indexer.contracts import IndexerError
from indexer.parsing.contracts import ParserConfig, ParseRequest
from indexer.parsing.registry import build_registry
from indexer.settings import ServerSettings


def test_paddle_vl_request_and_structured_result(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-test")
    page = {"prunedResult": {"width": 1000, "height": 2000, "parsing_res_list": [
        {"block_id": 0, "block_label": "doc_title", "block_content": "목표", "block_bbox": [1, 2, 3, 4]},
        {"block_id": 1, "block_label": "table", "block_content": "<table><tr><th>구분</th><th>목표</th></tr><tr><td>교육</td><td>혁신</td></tr></table>", "block_bbox": [10, 20, 30, 40]},
    ]}, "markdown": {"text": "내용"}}

    def respond(request):
        assert str(request.url) == "http://proxy/ai/stack/ocr/vl/layout-parsing"
        assert request.headers["authorization"] == "Bearer proxy-key"
        body = json.loads(request.content)
        assert base64.b64decode(body["file"]) == b"%PDF-test"
        assert body["fileType"] == 0 and body["visualize"] is False and body["useChartRecognition"] is True
        return httpx.Response(200, json={"errorCode": 0, "result": {"layoutParsingResults": [page]}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        parser = build_registry(client, remote_timeout=30, job_timeout=900).create(ParserConfig("paddle_vl", "http://proxy/ai/stack", "proxy-key", options={"useChartRecognition": "true"}))
        result = parser.parse(ParseRequest(path, "pdf", page_count=1))
        incomplete = parser.parse(ParseRequest(path, "pdf", page_count=2))
    assert result.provider == "paddle_vl" and not result.unavailable and not result.incomplete
    assert incomplete.unavailable == {1, 2} and incomplete.incomplete and not incomplete.elements
    assert result.raw["result"]["layoutParsingResults"][0] == page
    elements = normalize_elements(result.elements)
    assert elements[0]["kind"] == "heading" and elements[0]["text"] == "목표"
    assert elements[1]["kind"] == "table" and "<table>" in elements[1]["html"]
    assert elements[1]["coordinates"] == [10, 20, 30, 40]
    assert elements[1]["coordinate_space"]["width"] == 1000


def test_paddle_vl_rejects_unsupported_format_and_dpi(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-test")
    with httpx.Client() as client:
        parser = build_registry(client, remote_timeout=30, job_timeout=900).create(ParserConfig("paddle_vl", "http://proxy/ocr/vl", ""))
        for request, code in [(ParseRequest(path, "hwp"), "PARSER_FORMAT_UNSUPPORTED"),
                              (ParseRequest(path, "pdf", dpi=300), "DPI_UNSUPPORTED")]:
            with pytest.raises(IndexerError) as error:
                parser.parse(request)
            assert error.value.code == code


def test_paddle_vl_sends_embedded_image_as_image_type(tmp_path):
    path = tmp_path / 'figure.png'
    path.write_bytes(b'example-image')

    def respond(request):
        body = json.loads(request.content)
        assert body['fileType'] == 1 and base64.b64decode(body['file']) == b'example-image'
        assert body['useOcrForImageBlock'] is True and body['useChartRecognition'] is True
        return httpx.Response(200, json={'errorCode': 0, 'result': {'layoutParsingResults': [
            {'prunedResult': {'parsing_res_list': [{'block_label': 'text', 'block_content': '내용'}]}}]}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        parser = build_registry(client, remote_timeout=30, job_timeout=900).create(
            ParserConfig('paddle_vl', 'http://proxy', ''))
        result = parser.parse(ParseRequest(path, 'png', page_count=1))
        assert result.elements[0]['content']['text'] == '내용'


def test_paddle_vl_image_options_honor_explicit_settings(tmp_path):
    path = tmp_path / 'figure.png'
    path.write_bytes(b'image')
    def respond(request):
        body = json.loads(request.content)
        assert body['useOcrForImageBlock'] is False and body['useChartRecognition'] is False
        return httpx.Response(200, json={'errorCode': 0, 'result': {'layoutParsingResults': [
            {'prunedResult': {'parsing_res_list': []}}]}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        parser = build_registry(client, remote_timeout=30, job_timeout=900).create(
            ParserConfig('paddle_vl', 'http://proxy', '', options={'useOcrForImageBlock': 'false', 'useChartRecognition': 'false'}))
        parser.parse(ParseRequest(path, 'png', page_count=1))


def test_paddle_vl_is_available_as_settings_choice(tmp_path):
    from indexer.config import Config
    from indexer.settings import values
    assert ServerSettings.model_validate(values(Config(tmp_path)) | {"parser_provider": "paddle_vl"}).parser_provider == "paddle_vl"


def test_paddle_vl_uses_configured_base_and_custom_path(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-test")

    def respond(request):
        assert str(request.url) == "http://proxy/custom/v1/infer"
        return httpx.Response(200, json={"errorCode": 0, "result": {"layoutParsingResults": [
            {"prunedResult": {"parsing_res_list": []}}]}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        parser = build_registry(client, remote_timeout=30, job_timeout=900).create(
            ParserConfig("paddle_vl", "http://proxy/custom", "", options={"endpoint_path": "/v1/infer"}))
        assert parser.parse(ParseRequest(path, "pdf", page_count=1)).provider == "paddle_vl"


def test_processing_options_hide_unsupported_paddle_settings(tmp_path):
    from fastapi.testclient import TestClient
    from indexer.config import Config

    from apps.api.main import create_app

    config = Config(tmp_path, access_key="example", parser_provider="paddle_vl", default_dpi=400,
                    default_ocr_policy="force")
    with TestClient(create_app(config), base_url="https://testserver") as client:
        login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
        client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
        options = client.get("/v1/processing-options").json()
        assert options["formats"] == ["docx", "hwp", "hwpx", "pdf", "pptx", "xlsx"]
        assert options["parser_formats"] == ["pdf"]
        assert options["supports_dpi"] is False and options["force_ocr_formats"] == []
        assert options["defaults"]["dpi"] is None and options["defaults"]["ocr_policy"] == "automatic"


def test_image_timeout_is_shorter_than_pdf_timeout_and_never_sent_as_api_option(tmp_path):
    from indexer.parsing.paddle_vl_adapter import PaddleVLParser
    image = tmp_path / 'page.png'
    image.write_bytes(b'png')
    received = []
    def respond(request):
        received.append((request.extensions['timeout']['read'], json.loads(request.content)))
        return httpx.Response(200, json={'errorCode': 0, 'result': {'layoutParsingResults': [
            {'prunedResult': {'parsing_res_list': [{'block_label': 'text', 'block_content': '내용'}]}}]}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        parser = PaddleVLParser(ParserConfig('paddle_vl', 'http://proxy', '', sync_timeout=900,
                                options={'image_timeout_seconds': '12'}), client)
        parser.parse(ParseRequest(image, 'png', page_count=1, timeout_seconds=parser.image_timeout_seconds))
        parser.parse(ParseRequest(image, 'png', page_count=1))
    assert [item[0] for item in received] == [12, 900]
    assert all('image_timeout_seconds' not in body for _, body in received)
