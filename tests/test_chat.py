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


