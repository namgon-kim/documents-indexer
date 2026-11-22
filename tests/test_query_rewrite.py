import json
from types import SimpleNamespace

import httpx
import pytest
from indexer.chat import RagChat
from indexer.config import Config
from indexer.contracts import ChatMessage, ChatRequest
from indexer.diagnostics import capture_trace


def run_chat(tmp_path, history, question, response, *, enabled=True, method='hybrid'):
    calls, searches = [], []
    def provider(request):
        calls.append(json.loads(request.content))
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json={'choices': [{'message': {'content': response}}]})
    def search(request):
        searches.append(request)
        return {'index_version': 'v', 'results': []}
    config = Config(tmp_path, external_processing=True, llm_api_key='key', chat_query_rewrite_enabled=enabled)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client, capture_trace() as trace:
        RagChat(config, SimpleNamespace(search=search), client).retrieve(
            ChatRequest(message=question, history=history, method=method))
    return searches[0].query, calls, trace['stages']['retrieval_query']


def encoded(independent, query, reference=''):
    return json.dumps({'independent': independent, 'query': query, 'resolved_reference': reference}, ensure_ascii=False)


def test_independent_question_is_preserved_even_if_model_changes_it(tmp_path):
    question = '2025년 사업에서 민원정보 10,000건을 구축했나요?'
    history = [ChatMessage(role='user', content='교통 노선을 설명해줘')]
    query, calls, trace = run_chat(tmp_path, history, question, encoded(True, '다른 질의'))
    assert query == question and len(calls) == 1
    assert trace['rewrite_status'] == 'independent' and not trace['history_used']
    assert question not in json.dumps(trace, ensure_ascii=False)


def test_contextual_question_is_resolved_from_history(tmp_path):
    history = [ChatMessage(role='user', content='나래 시스템을 소개해줘'),
               ChatMessage(role='assistant', content='나래 시스템은 문서 관리 서비스입니다. [1]', citation_numbers=[1])]
    question = '그 시스템의 주요 기능은?'
    query, calls, trace = run_chat(tmp_path, history, question,
                                   encoded(False, '나래 시스템의 주요 기능은?', '나래 시스템'))
    assert query == '나래 시스템의 주요 기능은?' and len(calls) == 1
    assert trace['rewrite_status'] == 'rewritten' and trace['history_used']
    assert '[1]' not in calls[0]['messages'][1]['content']


def test_ordinal_reference_uses_a_target_actually_in_the_history(tmp_path):
    history = [ChatMessage(role='assistant', content='1. 알파 서비스\n2. 베타 서비스')]
    query, _, trace = run_chat(tmp_path, history, '두 번째 항목은?',
                               encoded(False, '베타 서비스 항목은?', '베타 서비스'))
    assert query == '베타 서비스 항목은?' and trace['rewrite_status'] == 'rewritten'


@pytest.mark.parametrize('result', [
    encoded(False, '나래 서비스의 예산 10억원은?', '나래 서비스'),
    encoded(False, '나래 서비스의 예산 11만원은?', '나래 서비스'),
    encoded(False, '가짜 서비스의 예산 10만원은?', '가짜 서비스'),
    encoded(False, '나래 서비스의 예산 10만원과 보안은?', '나래 서비스'),
    encoded(False, '나래 서비스의 예산 10만원과 X-22는?', '나래 서비스'),
    '{bad json',
    json.dumps({'independent': False, 'query': 'missing reference'}),
    httpx.ReadTimeout('timeout'),
])
def test_untrusted_rewrite_falls_back_to_original(tmp_path, result):
    question = '그 서비스의 예산 10만원은?'
    history = [ChatMessage(role='assistant', content='나래 서비스의 예산은 공개됩니다.')]
    query, _, trace = run_chat(tmp_path, history, question, result)
    assert query == question and not trace['history_used']
    assert trace['rewrite_status'] in {'failed', 'invalid_response', 'validation_failed'}


def test_no_call_without_history_or_when_disabled(tmp_path):
    query, calls, trace = run_chat(tmp_path, [], '새로운 문서는?', '{}')
    assert query == '새로운 문서는?' and not calls and trace['rewrite_status'] == 'no_history'
    history = [ChatMessage(role='user', content='교통 노선에 대해 알려줘')]
    query, calls, trace = run_chat(tmp_path, history, '업무 시스템의 처리 결과는?', '{}', enabled=False)
    assert query == '업무 시스템의 처리 결과는?' and not calls and trace['rewrite_status'] == 'disabled'


def test_opt_in_search_and_rerank_use_rewritten_query_and_original_answer_question(tmp_path):
    from indexer.search_engine import SearchHit, SearchService
    queries = []
    class Retriever:
        def search(self, query, limit, document_id=None):
            queries.append(('retrieve', query))
            return [SearchHit('target', 1)]
    class Reranker:
        def rerank(self, query, hits, limit):
            queries.append(('rerank', query))
            return hits
    search = SearchService(Retriever(), Retriever(), Reranker())
    def retrieve(request):
        hits = search.search(request.query, 'hybrid', 5, rerank=True, retrieval_top_n=25, rerank_top_n=5)
        return {'index_version': 'v', 'results': [{'chunk_id': hit.chunk_id, 'filename': 'doc',
                 'source_refs': [], 'search_text': '나래 시스템 기능'} for hit in hits]}
    calls = []
    def provider(request):
        payload = json.loads(request.content)
        calls.append(payload)
        text = (encoded(False, '나래 시스템의 주요 기능은?', '나래 시스템') if len(calls) == 1 else '기능 설명 [1]')
        return httpx.Response(200, json={'choices': [{'message': {'content': text}}]})
    config = Config(tmp_path, external_processing=True, llm_api_key='key', chat_query_rewrite_enabled=True)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = RagChat(config, SimpleNamespace(search=retrieve), client).invoke(ChatRequest(
            message='그 시스템의 주요 기능은?', history=[ChatMessage(role='assistant', content='나래 시스템 소개')],
            method='hybrid', rerank=True))
    assert result['sources'][0]['chunk_id'] == 'target' and len(calls) == 2
    assert all(query == '나래 시스템의 주요 기능은?' for _, query in queries)
    assert json.loads(calls[1]['messages'][-1]['content'])['질문'] == '그 시스템의 주요 기능은?'


def test_rewrite_setting_persists_and_can_be_disabled(tmp_path):
    from indexer.settings import apply_overrides, save_overrides

    config = Config(tmp_path)
    assert not apply_overrides(config).chat_query_rewrite_enabled
    save_overrides(config, {'chat_query_rewrite_enabled': True})
    assert apply_overrides(config).chat_query_rewrite_enabled
    save_overrides(config, {'chat_query_rewrite_enabled': False})
    assert not apply_overrides(config).chat_query_rewrite_enabled


def test_model_cannot_change_currency_even_if_other_currency_is_in_history(tmp_path):
    question = '그 서비스의 예산 10만원은?'
    history = [ChatMessage(role='assistant', content='나래 서비스 및 만엔 환율 정보')]
    result = encoded(False, '나래 서비스의 예산 10만엔은?', '나래 서비스')
    query, _, trace = run_chat(tmp_path, history, question, result)
    assert query == question and trace['rewrite_status'] == 'validation_failed'


def test_rewrite_does_not_replace_a_named_target_with_a_similar_one(tmp_path):
    question = '그 서비스의 민원정보는?'
    history = [ChatMessage(role='assistant', content='나래 서비스는 민원정보와 민원관리 기능을 제공합니다.')]
    query, _, trace = run_chat(tmp_path, history, question,
                               encoded(False, '나래 서비스의 민원관리는?', '나래 서비스'))
    assert query == question and trace['rewrite_status'] == 'validation_failed'


def test_rewrite_failure_is_visible_in_chat_response(tmp_path):
    retrieval = SimpleNamespace(search=lambda _: {'index_version': 'v', 'results': []})
    def provider(request):
        raise httpx.ReadTimeout('timeout')
    config = Config(tmp_path, external_processing=True, llm_api_key='key', chat_query_rewrite_enabled=True)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = RagChat(config, retrieval, client).invoke(ChatRequest(
            message='그 서비스의 기능은?', history=[ChatMessage(role='user', content='서비스 소개')], method='bm25'))
    assert result['search_counts']['query_rewrite_status'] == 'failed'
    assert result['sources'] == []


def test_truncated_rewrite_is_not_used(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {
            'content': encoded(False, '나래 시스템의 주요 기능은?', '나래 시스템')}}]})
    seen = []
    retrieval = SimpleNamespace(search=lambda request: seen.append(request) or {'index_version': 'v', 'results': []})
    config = Config(tmp_path, external_processing=True, llm_api_key='key', chat_query_rewrite_enabled=True)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client, capture_trace() as trace:
        RagChat(config, retrieval, client).retrieve(ChatRequest(message='그 시스템의 주요 기능은?',
            history=[ChatMessage(role='assistant', content='나래 시스템')], method='bm25'))
    assert seen[0].query == '그 시스템의 주요 기능은?'
    assert trace['stages']['retrieval_query']['rewrite_status'] == 'invalid_response'
