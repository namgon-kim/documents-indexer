import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from indexer.artifact_store import ArtifactStore
from indexer.chat import RagChat
from indexer.config import Config
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.diagnostics import artifact_summary, capture_trace
from indexer.index_publication import IndexPublisher
from indexer.ingestion_service import IngestionService
from indexer.parsing.contracts import ParseResult
from qdrant_client import QdrantClient

from apps.api.main import create_app
from tests.test_ingestion_search_flow import pdf_bytes


def test_diagnostics_execute_real_path_without_logging_content_or_extra_calls(tmp_path):
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            elements = [{'page': 1, 'category': 'paragraph', 'content': {'text': 'privateNeedle sensitiveBody'}}]
            return ParseResult('fake', elements, {'elements': elements})

    counts = {'embed': 0, 'chat': 0}
    def provider(request):
        body = json.loads(request.content)
        if request.url.path.endswith('/chat/completions'):
            counts['chat'] += 1
            return httpx.Response(200, json={'choices': [{'message': {'content': 'sensitiveAnswer [1]'}}]})
        counts['embed'] += 1
        return httpx.Response(200, json={'model': body['model'], 'data': [
            {'index': i, 'embedding': [1.0, 0.5]} for i in range(len(body['input']))]})

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        qdrant = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(http, 'https://example.test/v1', 'secretKey',
                                    EmbeddingProfile('p', 'doc', 'query', 2)))
        parser, storage = Parser(), ArtifactStore(tmp_path)
        service = IngestionService(storage, parser, publisher)
        pdf = tmp_path / 'source.pdf'
        pdf.write_bytes(pdf_bytes())
        ingested = service.ingest(pdf)
        doc, job, version = ingested['document_id'], ingested['job_id'], ingested['index_version']
        config = Config(tmp_path, access_key='localKey', external_processing=True, llm_api_key='secretKey')
        with TestClient(create_app(config, ingestion_service=service), base_url='https://testserver') as client:
            client.app.state.published_chat = RagChat(config, None, http)
            assert client.post('/v1/diagnostics/retrieve', json={'query': 'privateNeedle', 'method': 'bm25'}).status_code == 401
            login = client.post('/v1/auth/login', json={'access_key': 'localKey'}, headers={'Origin': 'https://testserver'})
            client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
            denied = client.post('/v1/diagnostics/retrieve', headers={'Authorization': 'Bearer token'},
                                 json={'query': 'privateNeedle', 'method': 'bm25'})
            assert denied.status_code == 403
            payload = {'query': 'privateNeedle', 'method': 'bm25', 'index_version': version,
                       'probes': [{'document_id': doc, 'page': 1, 'contains_line': ['sensitiveBody']}]}
            baseline = client.post('/v1/retrieve', json={key: value for key, value in payload.items() if key != 'probes'})
            assert baseline.status_code == 200
            before = dict(counts)
            response = client.post('/v1/diagnostics/retrieve', json=payload)
            assert response.status_code == 200, response.text
            report = response.json()
            trace = report['trace']
            assert report['index_version'] == version
            assert trace['stages']['bm25_unicode61'] and trace['stages']['ranked_selected']
            assert [item['chunk_id'] for item in trace['stages']['retrieval_returned']] == [
                item['chunk_id'] for item in baseline.json()['results']]
            chunk_id = trace['stages']['ranked_selected'][0]['chunk_id']
            assert trace['probes'][0]['stage_chunk_ids']['ranked_selected'] == [chunk_id]
            assert trace['probes'][0]['returned_chunk_ids'] == [chunk_id]
            assert counts == before and parser.calls == 1
            chat_request = {'message': 'privateNeedle', 'method': 'bm25', 'index_version': version,
                            'probes': payload['probes']}
            reply = client.post('/v1/diagnostics/chat', json=chat_request)
            assert reply.status_code == 200, reply.text
            chat_trace = reply.json()['trace']
            assert chat_trace['stages']['model_evidence'][0]['citation_id'] == 1
            assert chat_trace['stages']['answer_citations'] == [1]
            assert len(chat_trace['stages']['model_contract']['system_prompt_sha256']) == 64
            assert counts['chat'] == before['chat'] + 1 and counts['embed'] == before['embed']
            assert not chat_trace['probes'][0]['answer_literal_match']
            for item in (report, reply.json()):
                content = json.dumps(item)
                assert all(secret not in content for secret in ('privateNeedle', 'sensitiveBody', 'sensitiveAnswer', 'secretKey'))
            summary = client.get(f'/v1/diagnostics/artifacts/{doc}/{job}', params={'index_version': version})
            assert summary.status_code == 200 and summary.json()['published_status'] == 'available'
            assert summary.json()['chunk_comparison']['shared_count'] == 1
            (publisher.root / version / 'chunks.json').unlink()
            missing = client.get(f'/v1/diagnostics/artifacts/{doc}/{job}', params={'index_version': version})
            assert missing.json()['published_status'] == 'published_snapshot_unavailable'
        qdrant.close()


def test_diagnostic_context_is_isolated_and_missing_snapshot_is_explicit(tmp_path):
    from indexer import diagnostics
    with capture_trace() as first:
        diagnostics.record('only_first', 1)
        with capture_trace() as second:
            diagnostics.record('only_second', 2)
        diagnostics.record('again_first', 3)
    diagnostics.record('ignored', 4)
    assert first['stages'] == {'only_first': 1, 'again_first': 3}
    assert second['stages'] == {'only_second': 2}
    storage = SimpleNamespace(load_job=lambda *_: (_ for _ in ()).throw(FileNotFoundError()))
    publisher = SimpleNamespace(root=tmp_path, deleted_documents=lambda: set(), get_version=lambda _: None)
    result = artifact_summary(storage, publisher, str(uuid4()), str(uuid4()), str(uuid4()))
    assert result['published_status'] == 'published_snapshot_unavailable'
    assert all(value == 'unavailable' for value in result['artifacts'].values())


def test_trace_preserves_ranking_and_does_not_repeat_reranker():
    from indexer.search_engine import SearchHit, SearchService

    calls = []
    class Retriever:
        def __init__(self, hits):
            self.hits = hits
        def search(self, query, limit, document_id=None):
            calls.append(('search', query, limit))
            return self.hits
    class Reranker:
        def rerank(self, query, hits, limit):
            calls.append(('rerank', query, limit))
            return hits[:limit]
    service = SearchService(Retriever([SearchHit('a', 0.9)]), Retriever([SearchHit('b', 0.8)]), Reranker())
    baseline = service.search('sensitive query', 'hybrid', 2, rerank=True)
    calls.clear()
    with capture_trace() as trace:
        observed = service.search('sensitive query', 'hybrid', 2, rerank=True)
    assert observed == baseline and [name for name, *_ in calls].count('rerank') == 1
    assert [entry['chunk_id'] for entry in trace['stages']['ranked_selected']] == [hit.chunk_id for hit in observed]
    assert {'dense', 'sparse', 'fused_candidates', 'reranked', 'ranked_selected'} <= trace['stages'].keys()
    assert 'sensitive query' not in json.dumps(trace)


def test_table_expansion_diagnostics_explain_limits_and_scope():
    from indexer.published_retrieval import PublishedRetrieval

    def table(number, page=1):
        return {'chunk_id': str(number), 'document_id': 'd', 'job_id': 'j', 'kind': 'table',
                'element_ids': ['e1'], 'part_number': number, 'text': 'sensitive text', 'page': page}
    chunks = [table(i, 2 if i == 1 else 1) for i in range(6)]
    with capture_trace() as trace:
        expanded = PublishedRetrieval._expand_table([chunks[0]], {item['chunk_id']: item for item in chunks},
                                                   allowed_pages={1})
    decisions = {item['chunk_id']: item['reason'] for item in trace['stages']['table_expansion_decisions']['candidates']}
    assert decisions['0'] == 'already_selected' and decisions['1'] == 'outside_page_scope'
    assert decisions['2'] == decisions['3'] == decisions['4'] == 'added'
    assert decisions['5'] == 'expansion_limit' and len(expanded) == 4
    assert 'sensitive text' not in json.dumps(trace)


def test_artifact_summary_detects_changes_without_disclosing_values(tmp_path):
    from indexer import diagnostics

    doc, job, version = (str(uuid4()) for _ in range(3))
    storage = ArtifactStore(tmp_path)
    initial = [{'content': {'text': 'sensitive before'}}]
    final = [{'content': {'text': 'sensitive after'}}]
    storage.save_job(doc, job, 'parser_initial_elements', initial)
    storage.save_job(doc, job, 'parser_elements', final)
    storage.save_job(doc, job, 'elements', [{'text': 'normalized private', 'html': ''}])
    storage.save_job(doc, job, 'processing', {'provider': 'private-token', 'settings': {'target_chars': 1000, 'api_key': 'secret'}})
    storage.save_job(doc, job, 'chunks', [{'chunk_id': 'chunk', 'text': 'original private'}])
    folder = tmp_path / version
    folder.mkdir()
    (folder / 'chunks.json').write_text(json.dumps([{'chunk_id': 'chunk', 'document_id': doc,
                                                    'job_id': job, 'text': 'changed private'}]))
    publisher = SimpleNamespace(root=tmp_path, deleted_documents=lambda: set(),
                                get_version=lambda _: {'version': version, 'document_ids': [doc]})
    summary = diagnostics.artifact_summary(storage, publisher, doc, job, version)
    assert summary['parser_comparison']['changed_positions'] == 1
    assert summary['normalization_comparison']['text_changed_count'] == 1
    assert summary['chunk_comparison']['shared_changed_ids'] == ['chunk']
    assert summary['processing']['provider'] == 'other'
    assert all(secret not in json.dumps(summary) for secret in ('sensitive', 'private', 'secret'))


def test_probe_does_not_treat_omitted_or_truncated_model_text_as_visible(tmp_path):
    from indexer.diagnostics import probe_results

    from apps.api.main import DiagnosticProbe

    doc, version = str(uuid4()), str(uuid4())
    folder = tmp_path / version
    folder.mkdir()
    indexed = [{'chunk_id': key, 'document_id': doc, 'page': 1, 'text': 'prefix hiddenValue'}
               for key in ('a', 'b')]
    (folder / 'chunks.json').write_text(json.dumps(indexed))
    publisher = SimpleNamespace(root=tmp_path, deleted_documents=lambda: set())
    result = {'index_version': version, 'answer': 'prefix', 'sources': [
        {'chunk_id': 'a', 'document_id': doc, 'source_refs': [{'page': 1}], 'search_text': 'prefix'}]}
    trace = {'stages': {'ranked_selected': [{'chunk_id': 'a'}, {'chunk_id': 'b'}],
                        'model_evidence': [{'chunk_id': 'a', 'citation_id': 1, 'status': 'truncated'},
                                           {'chunk_id': 'b', 'citation_id': None, 'status': 'omitted'}]}}
    probe = DiagnosticProbe(document_id=doc, page=1, contains_line=['hiddenValue'])
    [observed] = probe_results([probe], result, trace, publisher, chat=True)
    assert observed['stage_chunk_ids']['ranked_selected'] == ['a', 'b']
    assert observed['stage_chunk_ids']['model_evidence'] == []
    assert observed['returned_chunk_ids'] == [] and not observed['answer_literal_match']


def test_model_trace_reports_count_limit_and_truncation_without_evidence_content():
    from indexer.chat import _chat_evidence

    hits = [{'chunk_id': str(index), 'filename': 'doc', 'source_refs': [], 'search_text': 'private' * 1000}
            for index in range(3)]
    with capture_trace() as trace:
        sources, _, counts = _chat_evidence(hits, 2, 4096)
    decisions = {item['chunk_id']: item['reason'] for item in trace['stages']['model_fit_decisions']}
    assert len(sources) == 1 and counts['evidence_truncated_count'] == 1
    assert decisions == {'2': 'evidence_count_limit', '0': 'truncated_byte_budget', '1': 'evidence_byte_budget'}
    assert 'private' not in json.dumps(trace)


def test_truncated_table_segment_not_mistaken_for_model_input(tmp_path):
    from indexer.diagnostics import probe_results

    from apps.api.main import DiagnosticProbe

    doc, version = str(uuid4()), str(uuid4())
    folder = tmp_path / version
    folder.mkdir()
    segment = {'row': 7, 'column': 3, 'text': 'confidential cell'}
    (folder / 'chunks.json').write_text(json.dumps([{'chunk_id': 'a', 'document_id': doc, 'page': 1,
                                                   'text': 'prefix confidential cell', 'table_segments': [segment]}]))
    publisher = SimpleNamespace(root=tmp_path, deleted_documents=lambda: set())
    result = {'index_version': version, 'answer': 'prefix', 'sources': [{
        'chunk_id': 'a', 'document_id': doc, 'source_refs': [{'page': 1}], 'search_text': 'prefix',
        'evidence_truncated': True, 'table_segments': [segment]}]}
    trace = {'stages': {'model_evidence': [{'chunk_id': 'a', 'citation_id': 1, 'status': 'truncated'}]}}
    probe = DiagnosticProbe(document_id=doc, page=1, row=7, column=3, contains_line=['confidential'])
    [observed] = probe_results([probe], result, trace, publisher, chat=True)
    assert observed['published_matching_chunk_ids'] == ['a']
    assert observed['returned_chunk_ids'] == [] and observed['stage_chunk_ids']['model_evidence'] == []
    assert observed['truncated_cell_match_unverified_chunk_ids'] == ['a']
