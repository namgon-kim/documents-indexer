import pytest
from fastapi.testclient import TestClient
from indexer.config import Config, base_path

from apps.api.base_path import prefix_variants, with_base_tag
from apps.api.main import create_app

ORIGIN = "https://proxy.example"
PREFIX = "/ai/labs/project/app"
INDEX = '<!doctype html><html><head><script type="module" src="./assets/index.js"></script></head><body></body></html>'


def test_base_path_is_normalized_and_validated():
    assert base_path("") == ""
    assert base_path(" /ai/labs/app/ ") == "/ai/labs/app"
    for bad in ("ai/labs", "/ai labs", "/ai/<x>", "https://host/ai"):
        with pytest.raises(ValueError):
            base_path(bad)


def test_prefix_variants_cover_proxies_that_strip_leading_segments():
    assert prefix_variants(PREFIX) == ["/ai/labs/project/app", "/labs/project/app", "/project/app", "/app"]
    assert prefix_variants("") == []


def test_base_tag_precedes_relative_assets():
    page = with_base_tag(INDEX, PREFIX)
    assert page.index('<base href="/ai/labs/project/app/">') < page.index("./assets/index.js")
    assert with_base_tag(INDEX, "") == INDEX


@pytest.mark.parametrize("received", [PREFIX, "/project/app", ""])
def test_app_serves_pages_and_api_under_any_stripped_prefix(tmp_path, received):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(INDEX, encoding="utf-8")
    config = Config(tmp_path / "data", web_dir=web, public_origin=ORIGIN, access_key="k" * 32,
                    public_base_path=PREFIX)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        for page in ("/", "/chat", "/settings"):
            response = client.get(received + page)
            assert response.status_code == 200
            assert '<base href="/ai/labs/project/app/">' in response.text
        assert client.get(received + "/health").status_code == 200
        login = client.post(received + "/v1/auth/login", json={"access_key": "k" * 32}, headers={"Origin": ORIGIN})
        assert login.status_code == 200
        assert client.get(received + "/v1/auth/session").status_code == 200


def test_unprefixed_deployment_is_unchanged(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(INDEX, encoding="utf-8")
    with TestClient(create_app(Config(tmp_path / "data", web_dir=web)), base_url=ORIGIN) as client:
        assert client.get("/chat").text == INDEX
        assert client.get(PREFIX + "/chat").status_code == 404
