import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from indexer.answer_audit import audit_answer
from indexer.bm25_index import BM25Index
from indexer.chat import RagChat
from indexer.chunking import make_chunks, normalize_elements
from indexer.citations import citation_numbers, render_citations, sanitize_history_citations
from indexer.config import Config
from indexer.contracts import ChatMessage, ChatRequest
from indexer.embedding_input import byte_size, heading_prefix
from indexer.heading_hierarchy import heading_marker


def build_index(path, old=False):
    if old:
        with sqlite3.connect(path) as db:
            db.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text, tokenize='unicode61')")
    index = BM25Index(path)
    index.replace_document('d', [
        {'chunk_id': 'a', 'text': '과제 1-1-1 항목 시스템'},
        {'chunk_id': 'b', 'text': '과제 1-1-2 항목 시스템'},
        {'chunk_id': 'c', 'text': '과제 1-1-11 항목 시스템'},
        {'chunk_id': 'd', 'text': '프로젝트 ABC-123 작업'},
        {'chunk_id': 'e', 'text': '프로젝트 ABC-124 작업'},
        {'chunk_id': 'f', 'text': '버전 v1.2.3 릴리스'},
        {'chunk_id': 'g', 'text': '버전 v1.2.4 릴리스'},
    ])
    return index


@pytest.mark.parametrize('old', [False, True])
@pytest.mark.parametrize(('query', 'expected'), [('1-1-1', 'a'), ('1-1-2', 'b'), ('1-1-11', 'c'),
                                                   ('ABC-123', 'd'), ('v1.2.3', 'f')])
def test_identifier_exact_match_ranks_above_similar_codes(tmp_path, old, query, expected):
    index = build_index(tmp_path / 'db.sqlite', old)
    result = index.search(query, 10, 'd')
    assert result and result[0].chunk_id == expected
    assert len(result) >= 2


def test_unicode_and_spaced_identifiers_match_exact_normalized_code(tmp_path):
    index = BM25Index(tmp_path / 'db')
    index.replace_document('d', [{'chunk_id': 'a', 'text': '대상 2 ‐ 4 ‐ 8 내용'},
                                 {'chunk_id': 'b', 'text': '대상 2-4-9 내용'}])
    assert index.search('2-4-8', 10)[0].chunk_id == 'a'


def test_korean_partial_fallback_retains_exact_and_avoids_common_suffix(tmp_path):
    index = BM25Index(tmp_path / 'db')
    index.replace_document('d', [{'chunk_id': 'a', 'text': '학습지원시스템'},
                                 {'chunk_id': 'b', 'text': '학습지원 시스템'},
                                 {'chunk_id': 'c', 'text': '기타 시스템'}])
    for query in ('학습지원시스템은', '학습지원시스템에게'):
        hits = [hit.chunk_id for hit in index.search(query, 10)]
        assert {'a', 'b'} <= set(hits) and 'c' not in hits
    assert index.search('학습지원시스템', 10)[0].chunk_id == 'a'


def test_hyphen_headings_create_siblings_without_changing_dates_and_explicit_levels():
    rows = [{'category': 'heading1', 'page': 1, 'content': {'text': text}}
            for text in ['Ⅰ. 계획', '1-1 부모', '1-1-1 첫째', '1-1-2 둘째', '2-4 범위']]
    elements = normalize_elements(rows)
    assert elements[2]['heading_path'][-2:] == ['1-1 부모', '1-1-1 첫째']
    assert elements[3]['heading_path'][-2:] == ['1-1 부모', '1-1-2 둘째']
    assert heading_marker('2025-03-15 일정').kind == 'ambiguous'
    assert heading_marker('2-4 범위').kind == 'ambiguous'
    assert heading_marker('1-2-3 릴리스').kind == 'ambiguous'
    explicit = normalize_elements([{'category': 'heading1', 'content': {'text': '1-1-1 A'}},
                                   {'category': 'heading2', 'content': {'html': '<h2>1-1-2 B</h2>'}}])
    assert explicit[-1]['heading_path'] == ['1-1-1 A', '1-1-2 B']


def test_heading_prefix_keeps_intermediate_level_within_utf8_budget():
    path = ['상위' * 100, '중간 과제 식별정보', '하위' * 100]
    text = heading_prefix(path, 160)
    assert '중간 과제 식별정보' in text and byte_size(text) <= 160
    assert heading_prefix(['짧은 제목', '세부'], 160) == '짧은 제목 > 세부'
    raw = [{'category': 'heading1', 'page': 1, 'content': {'text': path[0]}},
           {'category': 'heading2', 'page': 1, 'content': {'text': path[1]}},
           {'category': 'heading3', 'page': 1, 'content': {'text': path[2]}},
           {'category': 'paragraph', 'page': 1, 'content': {'text': '본문'}}]
    chunk = make_chunks('d', 'j', normalize_elements(raw), 400, 0)[-1]
    assert '중간 과제 식별정보' in chunk['heading_text'] and byte_size(chunk['heading_text']) <= 200


def test_retrieval_preserves_independent_questions_and_reuses_followup_history(tmp_path):
    seen = []
    retrieval = SimpleNamespace(search=lambda request: seen.append(request) or {'results': [], 'index_version': 'v'})
    chat = RagChat(Config(tmp_path), retrieval, None)
    history = [ChatMessage(role='user', content=f'이전 문맥 {i}') for i in range(4)]
    chat.retrieve(ChatRequest(message='새로운 정책은 무엇인가요?', method='bm25', history=history))
    assert seen[-1].query == '새로운 정책은 무엇인가요?'
    chat.retrieve(ChatRequest(message='그중 두 번째는?', method='bm25', history=history))
    assert all(item.content in seen[-1].query for item in history[-2:])
    assert all(item.content not in seen[-1].query for item in history[:-2])
    assert seen[-1].query.endswith('그중 두 번째는?')


def test_answer_includes_more_history_and_does_not_discard_cited_messages(tmp_path):
    history = [ChatMessage(role='assistant' if i % 2 else 'user', content=f'대화 {i} [1]') for i in range(20)]
    hit = {'filename': 'doc', 'source_refs': [], 'search_text': '근거'}
    retrieval = SimpleNamespace(search=lambda _: {'index_version': 'v', 'results': [hit]})
    def provider(request):
        data = json.loads(request.content)
        assert len(data['messages']) == 22
        assert data['messages'][1]['content'] == '대화 0 [1]'
        assert data['messages'][2]['content'] == '대화 1 ［1］'
        return httpx.Response(200, json={'choices': [{'message': {'content': '답 [1]'}}]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = RagChat(Config(tmp_path, external_processing=True, llm_api_key='x'), retrieval, client).invoke(
            ChatRequest(message='독립 질문', method='bm25', history=history))
    assert result['search_counts']['history_omitted_count'] == 0


@pytest.mark.parametrize(('raw', 'numbers', 'output'), [
    ('[1]', [1], '[1]'), ('[1, 2]', [1, 2], '[1][2]'), ('[1-3]', [1, 2, 3], '[1][2][3]'),
    ('[ 1, 2 - 3 ]', [1, 2, 3], '[1][2][3]'), ('[1, 9]', [1, 9], '[1][근거 확인 불가]'),
    ('[3-1]', [], '[근거 확인 불가]'), ('[1-999999]', [], '[근거 확인 불가]'),
    ('[1,,2]', [], '[근거 확인 불가]'),
])
def test_citation_formats_and_invalid_values(raw, numbers, output):
    assert citation_numbers(raw) == numbers
    assert render_citations(raw, 3) == output
    assert bool(audit_answer(raw, [{}, {}, {}])['warnings']) == ('근거 확인 불가' in output)


def test_history_citation_cleanup_handles_ranges_without_losing_text():
    assert sanitize_history_citations('앞의 내용 [1-3] 계속', {1, 2, 3}) == '앞의 내용  계속'


def test_hybrid_and_reranker_receive_exact_identifier_without_changing_candidate_limit(tmp_path):
    from indexer.search_engine import SearchHit, SearchService

    index = build_index(tmp_path / 'db')
    class Dense:
        def search(self, query, limit, document_id=None):
            assert limit == 6
            return [SearchHit('b', 0.9), SearchHit('a', 0.8)]
    class Reranker:
        def rerank(self, query, hits, limit):
            assert query == '1-1-1' and limit == 4
            assert {'a', 'b'} <= {hit.chunk_id for hit in hits}
            return hits[:limit]
    service = SearchService(Dense(), index, Reranker())
    counts = {}
    result = service.search('1-1-1', 'hybrid', 4, rerank=True, retrieval_top_n=6, rerank_top_n=4, counts=counts)
    assert result and counts['retrieval_top_n'] == 6 and counts['rerank_top_n'] == 4


@pytest.mark.parametrize('question', ['그 시스템은?', '그중 두 번째는?', '앞의 것과 비교해줘', '목표는?'])
def test_followup_variants_retain_previous_context(tmp_path, question):
    seen = []
    retrieval = SimpleNamespace(search=lambda request: seen.append(request) or {'results': [], 'index_version': 'v'})
    chat = RagChat(Config(tmp_path), retrieval, None)
    chat.retrieve(ChatRequest(message=question, method='bm25', history=[ChatMessage(role='user', content='선행 질문')]))
    assert '선행 질문' in seen[0].query and seen[0].query.endswith(question)


@pytest.mark.parametrize('old', [False, True])
def test_identifier_candidate_survives_crowded_shared_tokens(tmp_path, old):
    path = tmp_path / 'db'
    if old:
        with sqlite3.connect(path) as db:
            db.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text, tokenize='unicode61')")
    index = BM25Index(path)
    documents = [{'chunk_id': f'a{i:03}', 'text': '과제 1-1-2 공통 항목'} for i in range(40)]
    documents.append({'chunk_id': 'z-correct', 'text': '과제 1-1-1 공통 항목'})
    index.replace_document('d', documents)
    assert index.search('1-1-1', 1, 'd')[0].chunk_id == 'z-correct'


def test_diagnostic_citations_map_list_and_range_to_correct_sources():
    from indexer.diagnostics import capture_trace, set_answer

    with capture_trace() as trace:
        set_answer('요약 [1, 2-3].', [{'chunk_id': 'a'}, {'chunk_id': 'b'}, {'chunk_id': 'c'}])
    item = trace['stages']['answer_sentences']['items'][0]
    assert item['citation_ids'] == [1, 2, 3] and item['chunk_ids'] == ['a', 'b', 'c']


def test_large_history_is_bounded_by_utf8_and_reports_omissions(tmp_path):
    history = [ChatMessage(role='user', content=f'{i} ' + '가' * 3900) for i in range(30)]
    hit = {'filename': 'doc', 'source_refs': [], 'search_text': '근거'}
    retrieval = SimpleNamespace(search=lambda _: {'index_version': 'v', 'results': [hit]})
    def provider(request):
        messages = json.loads(request.content)['messages']
        assert '29 ' in messages[-2]['content'] and len(messages) > 12
        return httpx.Response(200, json={'choices': [{'message': {'content': '응답 [1]'}}]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = RagChat(Config(tmp_path, external_processing=True, llm_api_key='x'), retrieval, client).invoke(
            ChatRequest(message='새 질문', method='bm25', history=history))
    assert result['search_counts']['history_omitted_count'] > 0
    assert result['search_counts']['history_budget_bytes'] == 262144


def test_heading_prefix_always_respects_small_unicode_budgets():
    for budget in range(1, 161):
        assert byte_size(heading_prefix(['가나다' * 60, '중간 항목', '다라마' * 60], budget)) <= budget


@pytest.mark.parametrize('old', [False, True])
def test_fullwidth_identifier_normalization_is_scoped_to_identifier(tmp_path, old):
    path = tmp_path / 'db'
    if old:
        with sqlite3.connect(path) as db:
            db.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text, tokenize='unicode61')")
    index = BM25Index(path)
    index.replace_document('d', [{'chunk_id': 'a', 'text': '과제 １－２－３ 내용'},
                                 {'chunk_id': 'b', 'text': '과제 1-2-4 내용'}])
    assert index.search('1-2-3', 10)[0].chunk_id == 'a'
    assert index.search('１－２－３', 10)[0].chunk_id == 'a'


@pytest.mark.parametrize('question', ['이전 세대 플랫폼의 장점은?', '사회적 지위에서 오는 문제는?'])
def test_independent_questions_do_not_match_embedded_reference_fragments(tmp_path, question):
    seen = []
    retrieval = SimpleNamespace(search=lambda request: seen.append(request) or {'results': [], 'index_version': 'v'})
    chat = RagChat(Config(tmp_path), retrieval, None)
    chat.retrieve(ChatRequest(message=question, method='bm25', history=[ChatMessage(role='user', content='다른 주제')]))
    assert seen[0].query == question


@pytest.mark.parametrize('old', [False, True])
def test_identifier_with_same_component_tokens_does_not_tie(tmp_path, old):
    path = tmp_path / 'db'
    if old:
        with sqlite3.connect(path) as db:
            db.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text, tokenize='unicode61')")
    index = BM25Index(path)
    index.replace_document('d', [{'chunk_id': 'a-wrong', 'text': '과제 1-2-1 공통'},
                                 {'chunk_id': 'z-right', 'text': '과제 1-1-2 공통'}])
    assert [hit.chunk_id for hit in index.search('1-1-2', 10)] == ['z-right', 'a-wrong']


def test_followup_long_history_keeps_start_and_end_within_embedding_limit(tmp_path):
    seen = []
    profile = SimpleNamespace(max_input_bytes=400)
    retrieval = SimpleNamespace(publisher=SimpleNamespace(embedder=SimpleNamespace(profile=profile)),
                                search=lambda request: seen.append(request) or {'results': [], 'index_version': 'v'})
    history = [ChatMessage(role='user', content='고유대상 ' + '자료' * 800 + ' 마지막조건')]
    RagChat(Config(tmp_path), retrieval, None).retrieve(ChatRequest(message='그 시스템은?', history=history))
    assert '고유대상' in seen[0].query and '마지막조건' in seen[0].query
    assert seen[0].query.endswith('그 시스템은?') and byte_size(seen[0].query) <= 400


def test_partial_candidates_do_not_displace_a_full_exact_candidate_pool(tmp_path):
    index = BM25Index(tmp_path / 'db')
    chunks = [{'chunk_id': f'generic-{i}', 'text': '사업 추진'} for i in range(40)]
    chunks.append({'chunk_id': 'target', 'text': '학습지원시스템 상세'})
    index.replace_document('d', chunks)
    hits = [hit.chunk_id for hit in index.search('학습지원시스템은 사업', 10)]
    assert len(hits) == 10 and all(hit.startswith('generic-') for hit in hits)


def test_history_retains_uncited_bracket_numbers_without_reusing_citation_ids():
    assert sanitize_history_citations('연도 [2024], 근거 [1, 2]', {1, 2}) == '연도 ［2024］, 근거 '
    assert sanitize_history_citations('이전 결과 [1-3]', set()) == '이전 결과 ［1］［2］［3］'
    assert sanitize_history_citations('범위 [3-1]', set()) == '범위 ［3-1］'


def test_identifier_end_punctuation_is_not_part_of_identifier(tmp_path):
    index = BM25Index(tmp_path / 'db')
    index.replace_document('d', [{'chunk_id': 'a-wrong', 'text': 'ID 1-1-11. Next item'},
                                 {'chunk_id': 'z-right', 'text': 'ID 1-1-1. Next item'}])
    assert index.search('1-1-1', 10)[0].chunk_id == 'z-right'


def test_plain_decimal_is_not_treated_as_a_compound_identifier():
    from indexer.bm25_index import identifiers

    assert identifiers('비율 2.5 및 버전 v2.5') == ['v2.5']
