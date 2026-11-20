import httpx

from scripts.evaluate import evaluate, evidence_matches


def test_cell_evidence_matches_actual_cell_not_other_cell_in_same_chunk():
    result = {"document_id": "doc", "source_refs": [{"page": 4}], "search_text": "교육 / 12\n연구 / 15",
              "table_segments": [{"row": 2, "column": 2, "text": "교육 / 12"},
                                 {"row": 3, "column": 2, "text": "연구 / 15"}]}
    assert evidence_matches({"document_id": "doc", "page": 4, "row": 2, "column": 2,
                             "contains_line": ["교육", "12"]}, result)
    assert not evidence_matches({"document_id": "doc", "page": 4, "row": 3, "column": 2,
                                 "contains_line": ["교육", "12"]}, result)


def test_evaluation_uses_chosen_method_and_pins_version():
    import json
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/v1/retrieve" and body["method"] == "bm25"
        return httpx.Response(200, json={"index_version": "version-one", "results": [
            {"document_id": "doc", "source_refs": [{"page": 2}], "search_text": "교육 계획"}]})
    cases = [{"id": "1", "query": "교육", "evidence": [{"document_id": "doc", "page": 2}]},
             {"id": "2", "query": "계획", "evidence": [{"document_id": "doc", "page": 2}]}]
    with httpx.Client(base_url="https://test.example", transport=httpx.MockTransport(handler)) as client:
        report = evaluate(client, cases, 5, method="bm25")
    assert "index_version" not in calls[0] and calls[1]["index_version"] == "version-one"
    assert report["hit_rate"] == 1 and report["method"] == "bm25" and "p50_ms" in report
