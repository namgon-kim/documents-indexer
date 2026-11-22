import json
from types import SimpleNamespace

import httpx
import pytest
from indexer.chat import RagChat
from indexer.config import Config
from indexer.contracts import ChatMessage, ChatRequest, IndexerError


def test_chat_retrieves_sources_and_sends_context_and_history(tmp_path):
    calls = []
    hit = {"filename": "보고서.pdf", "source_refs": [{"page": 4}], "search_text": "교육혁신 목표"}
    class Retrieval:
        def search(self, request):
            calls.append(request)
            return {"index_version": "index", "results": [hit]}
    def provider(request):
        body = json.loads(request.content)
        assert request.headers.get('authorization') == 'Bearer fake'
        assert request.url.path.endswith('/chat/completions')
        assert body["reasoning_effort"] == "high"
        assert body["model"] == "alternate-model"
        assert body['messages'][0]['role'] == 'system'
        assert '자연스러운 대화체' in body['messages'][0]['content']
        assert '정보를 찾을 수 없다고' in body['messages'][0]['content']
        assert '해당 근거의 id' in body['messages'][0]['content']
        assert body["messages"][1]["content"] == "대학 계획"
        assert "교육혁신 목표" in body["messages"][-1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "교육혁신입니다. [1]"}, "finish_reason": "stop"}]})
    config = Config(tmp_path, external_processing=True, llm_api_key="fake", llm_models=("alternate-model",))
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        chat = RagChat(config, Retrieval(), client)
        result = chat.invoke(ChatRequest(message="목표는?", model="alternate-model", reasoning_effort="high", history=[ChatMessage(role="user", content="대학 계획")]))
        with pytest.raises(IndexerError) as error:
            chat.invoke(ChatRequest(message="목표는?", model="unlisted"))
        assert error.value.code == "LLM_MODEL_UNAVAILABLE"
    assert result["answer"] == "교육혁신입니다. [1]"
    assert result["sources"] == [hit]
    assert "대학 계획" in calls[0].query


def test_chat_requires_config_before_retrieval(tmp_path):
    chat = RagChat(Config(tmp_path), SimpleNamespace(), None)
    with pytest.raises(IndexerError, match="답변 모델"):
        chat.invoke(ChatRequest(message="질문"))


def test_chat_model_default_omits_reasoning_and_invalid_choice_is_reported(tmp_path):
    hit = {"filename": "보고서.pdf", "source_refs": [{"page": 1}], "search_text": "근거"}
    retrieval = SimpleNamespace(search=lambda _: {"index_version": "index", "results": [hit]})
    def provider(request):
        body = json.loads(request.content)
        if "reasoning_effort" not in body:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "답"}, "finish_reason": "stop"}]})
        return httpx.Response(400, json={"error": {"message": "unsupported"}})
    config = Config(tmp_path, external_processing=True, llm_api_key="fake")
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        chat = RagChat(config, retrieval, client)
        assert chat.invoke(ChatRequest(message="질문"))["answer"] == "답"
        with pytest.raises(IndexerError) as error:
            chat.invoke(ChatRequest(message="질문", reasoning_effort="max"))
        assert error.value.code == "LLM_REQUEST_REJECTED"


def test_chat_rejects_truncated_completion(tmp_path):
    hit = {'filename': '문서', 'source_refs': [], 'search_text': '근거'}
    retrieval = SimpleNamespace(search=lambda _: {'results': [hit], 'index_version': 'v'})
    def provider(_request):
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length',
                              'message': {'role': 'assistant', 'content': '잘린 답변'}}]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        chat = RagChat(Config(tmp_path, external_processing=True, llm_api_key='fake'), retrieval, client)
        with pytest.raises(IndexerError) as error:
            chat.invoke(ChatRequest(message='질문'))
        assert error.value.code == 'LLM_RESPONSE_TRUNCATED'




def test_chat_reports_evidence_budget_to_model_and_client(tmp_path):
    hits = [{"filename": "doc", "source_refs": [], "search_text": "가" * 1000} for _ in range(4)]
    retrieval = SimpleNamespace(search=lambda _: {"results": hits, "index_version": "v"})
    def provider(request):
        content = json.loads(json.loads(request.content)["messages"][-1]["content"])
        assert content["근거_제한"]["evidence_limited"]
        assert content["근거_제한"]["evidence_omitted_count"] >= 2
        assert content["근거_제한"]["evidence_truncated_count"] == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "일부 근거 [1]"}}]})
    config = Config(tmp_path, external_processing=True, llm_api_key="fake", chat_evidence_bytes=4096)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = RagChat(config, retrieval, client).invoke(ChatRequest(message="목록"))
    assert result["search_counts"]["evidence_omitted_count"] >= 2
    assert result["search_counts"]["evidence_truncated_count"] == 1



def test_large_first_evidence_is_utf8_safe_and_cited(tmp_path):
    hit = {"filename": "표.pdf", "source_refs": [], "search_text": "한글\n" * 2000,
           "table_expansion_complete": True}
    retrieval = SimpleNamespace(search=lambda _: {"results": [hit], "index_version": "v"})
    def provider(request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        item = payload["검색된 문서"][0]
        assert item["text"] and hit["search_text"].startswith(item["text"])
        assert item["표의_분할내용_모두_포함"] is False
        assert payload["근거_제한"]["evidence_truncated_count"] == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "근거 일부 [1]"}}]})
    config = Config(tmp_path, external_processing=True, llm_api_key="fake", chat_evidence_bytes=4096)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        result = RagChat(config, retrieval, client).invoke(ChatRequest(message="질문"))
    assert result["sources"][0]["evidence_truncated"]
    assert result["answer"] == "근거 일부 [1]"
    assert result["search_counts"]["evidence_bytes"] <= 4096


def test_chat_passes_inferred_table_relations_and_accepts_legacy_hits(tmp_path):
    relation = {'kind': 'row', 'text': '학생', 'element_id': 'e1', 'row': 2, 'column': 1,
                'origin': 'inferred', 'status': 'unverified'}
    hits = [{'filename': 'doc', 'source_refs': [], 'search_text': '학생 / 설문',
             'table_segments': [{'row': 2, 'column': 2, 'label': '학생', 'label_relations': [relation]}]},
            {'filename': 'old', 'source_refs': [], 'search_text': '이전 청크',
             'table_segments': [{'row': 1, 'column': 1, 'label': '옛 라벨'}]}]
    retrieval = SimpleNamespace(search=lambda _: {'results': hits, 'index_version': 'v'})
    def provider(request):
        body = json.loads(request.content)
        items = json.loads(body['messages'][-1]['content'])['검색된 문서']
        assert items[0]['표_라벨_관계'][0]['relations'][0] == relation
        assert items[1]['표_라벨_관계'][0]['relations'] is None
        assert '정보가 없거나 생략된 사실만으로 관계가 틀렸다고 판단하지 마세요' in body['messages'][0]['content']
        return httpx.Response(200, json={'choices': [{'message': {'content': '답변 [1]'}}]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        RagChat(Config(tmp_path, external_processing=True, llm_api_key='fake'), retrieval, client).invoke(
            ChatRequest(message='질문'))


def test_table_relation_metadata_does_not_displace_ranked_evidence():
    from indexer.chat import _chat_evidence

    relation = {'kind': 'column', 'text': 'A' * 60, 'element_id': 'e1', 'row': 1, 'column': 1,
                'origin': 'inferred', 'status': 'unverified'}
    hit = {'filename': 'doc', 'source_refs': [], 'search_text': 'A' * 2500,
           'table_segments': [{'row': i, 'column': 1, 'label': 'X', 'label_relations': [relation]}
                              for i in range(12)]}
    _, context, counts = _chat_evidence([hit], 20, 4096)
    assert context[0]['text'] == hit['search_text']
    assert context[0]['표_라벨_관계_일부_생략'] is True
    assert counts['evidence_truncated_count'] == 0
    assert counts['evidence_bytes'] <= 4096


def test_table_relation_metadata_does_not_displace_later_evidence():
    from indexer.chat import _chat_evidence

    relation = {'kind': 'column', 'text': 'X' * 100, 'element_id': 'e1', 'row': 1, 'column': 1,
                'origin': 'inferred', 'status': 'unverified'}
    table = {'filename': 'table', 'source_refs': [], 'search_text': 'A' * 1300,
             'table_segments': [{'row': i, 'column': 1, 'label': 'X', 'label_relations': [relation]}
                                for i in range(12)]}
    later = {'filename': 'later', 'source_refs': [], 'search_text': 'B' * 1300}
    _, context, counts = _chat_evidence([table, later], 20, 4096)
    assert [item['text'] for item in context] == [table['search_text'], later['search_text']]
    assert context[0]['표_라벨_관계_일부_생략'] is True
    assert counts['evidence_omitted_count'] == counts['evidence_truncated_count'] == 0


def test_interpretation_context_fits_before_table_relationship_metadata():
    from indexer.chat import _chat_evidence

    relation = {'kind': 'column', 'text': '제목' * 30, 'element_id': 'e1', 'row': 1, 'column': 1,
                'origin': 'inferred', 'status': 'unverified'}
    hit = {'filename': 'doc', 'source_refs': [], 'search_text': 'A' * 1000,
           'context_text': '필요한 앞뒤 문맥 ' * 50,
           'table_segments': [{'row': i, 'column': 1, 'label': '제목', 'label_relations': [relation]}
                              for i in range(12)]}
    _, context, counts = _chat_evidence([hit], 20, 4096)
    assert context[0]['text'] == hit['search_text']
    assert context[0]['context'] == hit['context_text']
    assert context[0]['표_라벨_관계_일부_생략'] is True
    assert counts['context_omitted_count'] == 0
    assert counts['evidence_bytes'] <= 4096
