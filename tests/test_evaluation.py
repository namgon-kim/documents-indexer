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


def test_trace_evaluation_uses_single_diagnostic_request_and_pins_version():
    import json
    from uuid import uuid4

    document, version = str(uuid4()), str(uuid4())
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == '/v1/diagnostics/retrieve'
        trace = {'trace_id': 'trace', 'stages': {'retrieval_returned': [{'chunk_id': 'c'}]},
                 'probes': [{'returned_chunk_ids': ['c']}]}
        return httpx.Response(200, json={'index_version': version, 'trace': trace})
    cases = [{'id': str(i), 'query': 'private-query',
              'evidence': [{'document_id': document, 'page': 2, 'contains_line': ['private-value']}]} for i in range(2)]
    with httpx.Client(base_url='https://test.example', transport=httpx.MockTransport(handler)) as client:
        report = evaluate(client, cases, 5, method='bm25', trace_mode='search')
    assert len(calls) == 2 and calls[1]['index_version'] == version
    assert report['hit_rate'] == 1 and report['trace_mode'] == 'search'
    assert 'private-value' not in json.dumps(report) and 'private-query' not in json.dumps(report)


def test_chat_trace_metrics_use_model_citation_order_without_extra_request():
    import json
    from uuid import uuid4

    doc, version = str(uuid4()), str(uuid4())
    calls = []
    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert request.url.path == '/v1/diagnostics/chat' and payload['top_k'] == 5
        assert payload['rerank'] and 'query' not in payload
        return httpx.Response(200, json={'index_version': version, 'trace': {'trace_id': 't',
            'stages': {'model_evidence': [{'chunk_id': 'a', 'citation_id': None},
                                           {'chunk_id': 'b', 'citation_id': 1}]},
            'probes': [{'returned_chunk_ids': ['b']}]}})
    cases = [{'id': 'x', 'query': 'question', 'evidence': [{'document_id': doc, 'page': 1}]}]
    with httpx.Client(base_url='https://test.example', transport=httpx.MockTransport(handler)) as client:
        report = evaluate(client, cases, 5, method='hybrid', trace_mode='chat', rerank=True)
    assert len(calls) == 1 and report['mrr'] == 1 and report['rerank']


def test_chat_trace_reports_context_presence_separately_from_citable_recall():
    from uuid import uuid4

    doc, version = str(uuid4()), str(uuid4())
    def handler(_request):
        return httpx.Response(200, json={'index_version': version, 'trace': {'trace_id': 't',
            'stages': {'model_evidence': [{'chunk_id': 'c', 'citation_id': 1}]},
            'probes': [{'returned_chunk_ids': [], 'model_input_status': 'captured',
                        'model_input_matches': [{'chunk_id': 'c', 'fields': ['context']}],
                        'truncated_cell_match_unverified_chunk_ids': []}]}})
    cases = [{'id': 'x', 'query': 'question', 'evidence': [{'document_id': doc, 'page': 1}]}]
    with httpx.Client(base_url='https://test.example', transport=httpx.MockTransport(handler)) as client:
        report = evaluate(client, cases, 5, method='bm25', trace_mode='chat')
    assert report['recall'] == 0
    assert report['cases'][0]['model_input_recall'] == 1
    assert report['cases'][0]['model_input_recall_status'] == 'literal_source_fields_only'


def test_chat_trace_reports_truncated_cell_as_unknown_not_missing():
    from uuid import uuid4

    doc, version = str(uuid4()), str(uuid4())
    def handler(_request):
        return httpx.Response(200, json={'index_version': version, 'trace': {'trace_id': 't',
            'stages': {'model_evidence': [{'chunk_id': 'c', 'citation_id': 1}]},
            'probes': [{'returned_chunk_ids': [], 'model_input_status': 'captured',
                        'model_input_matches': [], 'truncated_cell_match_unverified_chunk_ids': ['c']}]}})
    cases = [{'id': 'x', 'query': 'question', 'evidence': [{'document_id': doc, 'page': 1, 'row': 2}]}]
    with httpx.Client(base_url='https://test.example', transport=httpx.MockTransport(handler)) as client:
        report = evaluate(client, cases, 5, method='bm25', trace_mode='chat')
    assert report['cases'][0]['model_input_recall'] is None
    assert report['cases'][0]['model_input_recall_status'] == 'unknown'
