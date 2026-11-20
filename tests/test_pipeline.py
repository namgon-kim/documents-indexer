import pytest
from fastapi.testclient import TestClient
from indexer.config import Config
from indexer.parse_workflow import retry_plan

from apps.api.main import create_app

ORIGIN = "https://testserver"


@pytest.mark.parametrize(
    ("total", "missing", "budget", "action"),
    [(20, 10, 30, "partial"), (20, 11, 30, "full"), (20, 11, 10, "partial"), (7, 4, 7, "full")],
)
def test_retry_threshold_is_a_ratio(total, missing, budget, action):
    result, pages = retry_plan(total, set(range(1, missing + 1)), budget)
    assert result == action
    assert len(pages) == (total if action == "full" else min(missing, budget))


def test_retry_prioritizes_confirmed_failures_when_the_page_budget_is_short():
    assert retry_plan(100, {1, 2, 90}, 2, priority={90}) == ("partial", [1, 90])


def test_trusted_iframe_code_can_only_be_exchanged_once(tmp_path):
    from scripts.issue_embed_code import issue_code

    host = "https://host.example"
    secret = "synthetic-test-signing-secret-32-bytes"
    nonce = "test-nonce-12345678"
    config = Config(tmp_path, frame_origins=(host,), public_origin=ORIGIN, embed_secret=secret)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        code = issue_code(app_origin=ORIGIN, host_origin=host, nonce=nonce, secret=secret)
        payload = {"code": code, "nonce": nonce, "parent_origin": host}
        forbidden = client.post(
            "/v1/auth/embed/exchange",
            json={**payload, "parent_origin": "https://other.example"},
            headers={"Origin": ORIGIN},
        )
        assert forbidden.status_code == 403
        grant = client.post("/v1/auth/embed/exchange", json=payload, headers={"Origin": ORIGIN})
        assert grant.status_code == 200, grant.text
        response = client.get(
            "/v1/processing-options",
            headers={
                "Authorization": "Bearer " + grant.json()["access_token"],
            },
        )
        assert response.status_code == 200
        again = client.post("/v1/auth/embed/exchange", json=payload, headers={"Origin": ORIGIN})
        assert again.json()["error"]["code"] == "EMBED_CODE_REUSED"
