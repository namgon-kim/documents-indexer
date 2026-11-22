import json
from types import SimpleNamespace

import httpx
import pytest
from indexer.bm25_index import BM25Index
from indexer.chat import RagChat, _needs_retrieval_context
from indexer.config import Config
from indexer.contracts import ChatMessage, ChatRequest
from indexer.diagnostics import capture_trace
from indexer.search_engine import SearchHit, SearchService


@pytest.mark.parametrize('question', [
    '업무 시스템의 접수 데이터는 30건이고 처리 데이터는 70건입니다. 이 수치의 차이는 무엇인가요?',
    '교육 시스템의 연간 예산은 100만원입니다. 그 금액은 어떻게 사용하나요?',
    '초록 서비스의 요금과 파랑 서비스의 요금을 비교하고 이 서비스의 장점을 알려줘',
])
def test_questions_with_a_local_referent_do_not_reuse_old_topics(question):
    assert not _needs_retrieval_context(question)


@pytest.mark.parametrize('question', [
    '그 서비스는?', '두 번째는?', '앞의 것과 비교해줘', '가격 말고 그 서비스의 기능은?',
    '2025년 새 정책에 적용된 그 서비스의 가격은?', '이전에 설명한 시스템은 어떤 장점이 있나요?',
    '2025년 새 정책에 적용된 그 결과는 무엇인가요?',
    '2025년 4분기 정책에 적용된 그 결과는 무엇인가요?',
    '목표는?', '사용료는?',
])
def test_followups_still_use_context_when_local_antecedent_is_unclear(question):
    assert _needs_retrieval_context(question)


def test_hybrid_reranking_uses_independent_question_with_or_without_history(tmp_path):
    texts = {'relevant-a': '업무 시스템 접수 데이터 30건 처리 데이터 70건',
             'relevant-b': '업무 시스템 접수 처리 현황과 수치',
             'unrelated': '교통 노선 운영 시간과 정류장',
             'other-a': '업무 시스템 운용 절차', 'other-b': '업무 시스템 서비스 안내',
             'other-c': '교통 경로', 'other-d': '교통 정류장', 'other-e': '교통 안내'}
    index = BM25Index(tmp_path / 'bm25.sqlite')
    index.replace_document('doc', [{'chunk_id': key, 'text': value} for key, value in texts.items()])
    queries = []
    class Dense:
        def search(self, query, limit, document_id=None):
            assert limit == 25
            queries.append(('dense', query))
            return [SearchHit('relevant-a', 1), SearchHit('relevant-b', .9), SearchHit('unrelated', .8),
                    SearchHit('other-a', .7), SearchHit('other-b', .6), SearchHit('other-c', .5),
                    SearchHit('other-d', .4), SearchHit('other-e', .3)]
    class Reranker:
        def rerank(self, query, hits, limit):
            assert limit == 5
            queries.append(('rerank', query))
            order = (['unrelated', 'other-a', 'other-b', 'other-c', 'other-d', 'other-e', 'relevant-a', 'relevant-b']
                     if '교통' in query else ['relevant-a', 'relevant-b', 'unrelated', 'other-a', 'other-b', 'other-c', 'other-d', 'other-e'])
            selected = {hit.chunk_id: hit for hit in hits}
            return [selected[key] for key in order if key in selected][:limit]
    search = SearchService(Dense(), index, Reranker())
    def retrieve(request):
        hits = search.search(request.query, 'hybrid', 5, rerank=True, retrieval_top_n=25, rerank_top_n=5)
        return {'index_version': 'v', 'results': [{'chunk_id': hit.chunk_id, 'filename': 'doc',
                  'source_refs': [], 'search_text': texts[hit.chunk_id]} for hit in hits]}
    payloads = []
    def provider(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '비교 결과 [1] [2]'}}]})
    question = '업무 시스템의 접수 데이터는 30건이고 처리 데이터는 70건입니다. 이 수치의 차이는 무엇인가요?'
    history = [ChatMessage(role='user', content='교통 노선 정보를 알려줘'),
               ChatMessage(role='assistant', content='교통 정류장과 운행 시간')]
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        chat = RagChat(Config(tmp_path, external_processing=True, llm_api_key='fake'), SimpleNamespace(search=retrieve), client)
        for prior in ([], history, history * 9):
            with capture_trace() as trace:
                result = chat.invoke(ChatRequest(message=question, history=prior, method='hybrid', rerank=True))
            assert [source['chunk_id'] for source in result['sources'][:2]] == ['relevant-a', 'relevant-b']
            for stage in ('dense', 'sparse', 'fused_candidates', 'reranked', 'ranked_selected'):
                assert {'relevant-a', 'relevant-b'} <= {row['chunk_id'] for row in trace['stages'][stage]}
            assert [row['chunk_id'] for row in trace['stages']['reranked'][:2]] == ['relevant-a', 'relevant-b']
            assert {'relevant-a', 'relevant-b'} <= {row['chunk_id'] for row in trace['stages']['model_evidence']
                                                    if row['status'] in {'included', 'truncated'}}
            assert trace['stages']['retrieval_query']['history_used'] is False
            assert trace['stages']['retrieval_query']['history_messages_available'] == len(prior)
            assert trace['stages']['answer_citations'] == [1, 2]
    assert all(query == question for _, query in queries)
    assert len(payloads[-1]['messages']) > len(payloads[0]['messages'])
    assert '교통' in json.dumps(payloads[-1], ensure_ascii=False)


def test_local_context_diagnostics_do_not_record_question_or_history(tmp_path):
    query = '연구 기관의 인원은 7명이고 장비는 3개입니다. 이 수치는 무엇인가요?'
    history = [ChatMessage(role='user', content='다른 주제의 비밀 단어')]
    retrieval = SimpleNamespace(search=lambda _: {'index_version': 'v', 'results': []})
    with capture_trace() as trace:
        RagChat(Config(tmp_path), retrieval, None).retrieve(ChatRequest(message=query, history=history, method='bm25'))
    assert not trace['stages']['retrieval_query']['history_used']
    assert query not in json.dumps(trace, ensure_ascii=False)
    assert history[0].content not in json.dumps(trace, ensure_ascii=False)


@pytest.mark.parametrize('question', [
    '정책 A는 개인정보 보호 기준입니다. 이것을 어떻게 적용하나요?',
    '서비스 B는 이용 신청 절차입니다. 그것의 적용 방법은 무엇인가요?',
])
def test_explicit_single_local_statement_does_not_import_previous_history(tmp_path, question):
    seen = []
    retrieval = SimpleNamespace(search=lambda request: seen.append(request) or {'index_version': 'v', 'results': []})
    history = [ChatMessage(role='user', content='이전 무관한 주제')]
    with capture_trace() as trace:
        RagChat(Config(tmp_path), retrieval, None).retrieve(ChatRequest(message=question, history=history, method='bm25'))
    assert seen[0].query == question
    assert trace['stages']['retrieval_query']['reference_states'] == ['local']


@pytest.mark.parametrize('question', [
    '2025년 1차 평가를 거쳤다고 했습니다. 그 결과는 무엇인가요?',
    '2차 심사를 진행한다고 설명했습니다. 그 결과는 무엇인가요?',
    '작년 정책이 유효하다고 했습니다. 이것을 어떻게 적용하나요?',
])
def test_reported_context_or_ordinal_number_does_not_prove_a_local_referent(tmp_path, question):
    seen = []
    retrieval = SimpleNamespace(search=lambda request: seen.append(request) or {'index_version': 'v', 'results': []})
    history = [ChatMessage(role='user', content='이전 평가 기록'), ChatMessage(role='assistant', content='이전 평가 내용')]
    with capture_trace() as trace:
        RagChat(Config(tmp_path), retrieval, None).retrieve(ChatRequest(message=question, history=history, method='bm25'))
    assert '이전 평가' in seen[0].query
    assert trace['stages']['retrieval_query']['reference_states'] == ['external']


def test_multiple_matching_referents_are_reported_as_ambiguous(tmp_path):
    question = '2025년 신규 서비스 1은 A이고 신규 서비스 2는 B입니다. 그 서비스는 언제 개시하나요?'
    seen = []
    retrieval = SimpleNamespace(search=lambda request: seen.append(request) or {'index_version': 'v', 'results': []})
    history = [ChatMessage(role='user', content='관련 없는 이전 주제')]
    with capture_trace() as trace:
        RagChat(Config(tmp_path), retrieval, None).retrieve(ChatRequest(message=question, history=history, method='bm25'))
    assert trace['stages']['retrieval_query']['reference_states'] == ['ambiguous']
    assert seen[0].query == question
