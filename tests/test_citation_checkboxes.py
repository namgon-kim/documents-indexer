"""Citation syntax must not rewrite ordinary Markdown checklist items."""

import json
from types import SimpleNamespace

import httpx
import pytest
from indexer.answer_audit import audit_answer
from indexer.chat import RagChat
from indexer.citations import CITATION, citation_numbers, render_citations, sanitize_history_citations
from indexer.config import Config
from indexer.contracts import ChatMessage, ChatRequest
from indexer.diagnostics import capture_trace, set_answer


@pytest.mark.parametrize('marker', ['[ ]', '[  ]', '[x]', '[X]', '[-]', '[]', '[\t]'])
def test_checkboxes_are_not_citations_in_all_paths(marker):
    answer = f'- {marker} 할 일'
    assert CITATION.search(answer) is None
    assert citation_numbers(answer) == []
    assert render_citations(answer, 3) == answer
    assert sanitize_history_citations(answer, {1}) == answer
    assert audit_answer(answer, [{}])['warnings'] == []
    with capture_trace() as trace:
        set_answer(answer, [{'chunk_id': 'a'}])
    metadata = trace['stages']['answer_sentences']['items'][0]
    assert metadata['citation_ids'] == []
    assert metadata['invalid_citation_ids'] == []
    assert 'invalid_citation_format' not in metadata


@pytest.mark.parametrize(('raw', 'expected', 'numbers'), [
    ('[1]', '[1]', [1]), ('[ 1 ]', '[1]', [1]), ('[1,2]', '[1][2]', [1, 2]),
    ('[1, 2]', '[1][2]', [1, 2]), ('[1-3]', '[1][2][3]', [1, 2, 3]),
    ('[0]', '[근거 확인 불가]', []), ('[3-1]', '[근거 확인 불가]', []),
    ('[1-101]', '[근거 확인 불가]', []), ('[1,,2]', '[근거 확인 불가]', []),
    ('[4]', '[근거 확인 불가]', [4]), ('[-1]', '[근거 확인 불가]', []),
])
def test_numeric_citation_rules_are_preserved(raw, expected, numbers):
    assert render_citations(raw, 3) == expected
    assert citation_numbers(raw) == numbers
    audit = audit_answer(raw, [{}, {}, {}])
    assert bool(audit['warnings']) == ('근거 확인 불가' in expected)


@pytest.mark.parametrize('count', [8, 17])
def test_multiple_checklist_items_reach_user_and_history_unchanged(tmp_path, count):
    # Synthetic list sizes reproduce the two reported warning volumes without
    # storing the source document or asserting the factual content of answers.
    checklist = '\n'.join(f'- [ ] 항목 {i}' for i in range(count))
    hit = {'chunk_id': 'chunk-a', 'filename': 'doc', 'source_refs': [], 'search_text': '근거'}
    retrieval = SimpleNamespace(search=lambda _: {'index_version': 'v', 'results': [hit]})
    payloads = []
    def provider(request):
        payloads.append(json.loads(request.content))
        answer = checklist + '\n완료 항목 [1]' if len(payloads) == 1 else '이전 목록을 확인했습니다. [1]'
        return httpx.Response(200, json={'choices': [{'message': {'content': answer}}]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        chat = RagChat(Config(tmp_path, external_processing=True, llm_api_key='fake'), retrieval, client)
        with capture_trace() as trace:
            first = chat.invoke(ChatRequest(message='목록을 정리해 줘', method='bm25'))
        assert first['answer'] == checklist + '\n완료 항목 [1]'
        assert first['answer_verification']['warnings'] == []
        metadata = trace['stages']['answer_sentences']['items']
        assert all('invalid_citation_format' not in item for item in metadata)
        assert metadata[-1]['citation_ids'] == [1] and metadata[-1]['chunk_ids'] == ['chunk-a']
        history = [ChatMessage(role='assistant', content=first['answer'], citation_numbers=[1])]
        chat.invoke(ChatRequest(message='목록을 다시 설명해 줘', method='bm25', history=history))
    sent = payloads[1]['messages'][1]['content']
    assert sent == checklist + '\n완료 항목 '
    assert '[근거 확인 불가]' not in sent and '［ ］' not in sent
