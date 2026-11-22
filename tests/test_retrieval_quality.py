import sqlite3
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unicodedata import normalize
from uuid import uuid4

import httpx
import pytest
from indexer.bm25_index import BM25Index
from indexer.chat import RagChat
from indexer.chunking import make_chunks, normalize_elements
from indexer.config import Config
from indexer.contracts import ChatMessage, ChatRequest, IndexerError
from indexer.embedding_adaptation import embed_chunks
from indexer.representation_audit import audit_representation


def element(kind, text, page=1, html=None):
    return {"page": page, "category": kind, "content": {"text": text, "html": html or ""}}


def test_selected_input_budget_is_applied_to_korean_chunks():
    elements = normalize_elements([element("paragraph", "가나다 " * 600)])
    large = make_chunks("d", "j", elements, target_chars=4000, max_bytes=12000)
    small = make_chunks("d", "j", elements, target_chars=4000, max_bytes=3800)
    assert len(large) == 1 and len(large[0]["text"].encode()) > 3800
    assert len(small) > 1 and all(len(item["text"].encode()) <= 3800 for item in small)


def test_resplit_paragraphs_link_only_actual_source_and_reconnect_neighbours():
    elements = normalize_elements([element("paragraph", "첫단락 " * 45), element("paragraph", "둘째단락 " * 45)])
    chunks = make_chunks("d", "j", elements, target_chars=1000, max_bytes=12000)
    assert len(chunks) == 1 and chunks[0]["element_ids"] == ["e1", "e2"]
    cache = SimpleNamespace(embed=lambda texts: [[1.0] for _ in texts])
    split, _ = embed_chunks(cache, chunks, 256, 10)
    assert len(split) > 2
    assert all(("e1" in item["element_ids"]) == ("첫단락" in item["body_text"]) for item in split)
    assert all(("e2" in item["element_ids"]) == ("둘째단락" in item["body_text"]) for item in split)
    assert all(a["next_chunk_id"] == b["chunk_id"] and b["previous_chunk_id"] == a["chunk_id"] for a, b in pairwise(split))
    assert not audit_representation(elements, split)["unrepresented_elements"]


def test_caption_and_repeated_header_are_excluded_but_unique_header_remains():
    rows = [element("header", "반복 머리글", 1), element("caption", "표 1 예산", 1),
            element("table", "", 1, "<table><tr><th>항목</th><th>금액</th></tr><tr><td>시설</td><td>12</td></tr></table>"),
            element("header", "반복 머리글", 2), element("header", "고유 내용", 3)]
    elements = normalize_elements(rows)
    chunks = make_chunks("d", "j", elements)
    audit = audit_representation(elements, chunks)
    assert {item["element_id"] for item in audit["excluded"]} == {"e1", "e2", "e4"}
    assert any("고유 내용" in item["text"] for item in chunks)
    assert sum("표 1 예산" in item["text"] for item in chunks) == 1
    assert not audit["missing_table_cells"]


def test_korean_lexical_search_short_tokens_and_unicode(tmp_path):
    index = BM25Index(tmp_path / "new.sqlite")
    index.replace_document("d", [{"chunk_id": "a", "text": "서비스는 대학의 AI 교육입니다"}])
    for query in ("서비스", "대학", "AI", normalize("NFD", "대학")):
        assert [hit.chunk_id for hit in index.search(query, 5)] == ["a"]
    assert index.search("병원", 5) == []


def test_existing_lexical_version_is_not_silently_rebuilt(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text, tokenize='unicode61')")
        db.execute("INSERT INTO chunks VALUES ('a', 'd', '서비스는')")
    index = BM25Index(path)
    assert not index.has_substrings and index.search("서비스", 5) == []
    assert index.search("서비스는", 5)[0].chunk_id == "a"


def test_long_korean_history_preserves_question_and_respects_byte_budget(tmp_path):
    seen = []
    retrieval = SimpleNamespace(publisher=SimpleNamespace(embedder=SimpleNamespace(profile=SimpleNamespace(max_input_bytes=256))),
                                search=lambda request: seen.append(request) or {"results": [], "index_version": "v"})
    chat = RagChat(Config(Path(tmp_path), external_processing=True, llm_api_key="k"), retrieval, None)
    result = chat.invoke(ChatRequest(message="현재 질문", history=[ChatMessage(role="user", content="이전질문 " * 200)]))
    assert result['answer'] == '질문하신 정보를 찾을 수 없습니다.' and result['sources'] == []
    assert seen[0].query.endswith("현재 질문") and len(seen[0].query.encode()) <= 256
    with pytest.raises(IndexerError, match="질문을 줄여"):
        chat.invoke(ChatRequest(message="가" * 100))
    chat.invoke(ChatRequest(message="가" * 100, method="bm25"))


def test_chat_history_citations_and_invalid_new_citations(tmp_path):
    hit = {"filename": "doc", "source_refs": [{"page": 1, "role": "evidence"}], "search_text": "근거"}
    retrieval = SimpleNamespace(search=lambda _: {"results": [hit], "index_version": "v"})
    def respond(request):
        import json
        body = json.loads(request.content)
        assert body["messages"][1]["content"] == "사용자 [1] 원문"
        assert body["messages"][2]["content"] == "기존 답변 "
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "새 답 [1] 잘못된 [9]"}, "finish_reason": "stop"}]})
    config = Config(Path(tmp_path), external_processing=True, llm_api_key="key")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        chat = RagChat(config, retrieval, client)
        answer = chat.invoke(ChatRequest(message="질문", history=[ChatMessage(role="user", content="사용자 [1] 원문"),
            ChatMessage(role="assistant", content="기존 답변 [1]", citation_numbers=[1])]))
    assert answer["answer"] == "새 답 [1] 잘못된 [근거 확인 불가]"


def test_embedding_progress_separates_current_document_from_corpus():
    cache = SimpleNamespace(embed=lambda texts: [[1.0] for _ in texts])
    chunks = [{"chunk_id": str(uuid4()), "document_id": "old", "text": "기존"},
              {"chunk_id": str(uuid4()), "document_id": "new", "text": "새 문서"}]
    progress = []
    embed_chunks(cache, chunks, 3800, 1, tracked_document="new", detail_progress=lambda *values: progress.append(values))
    assert progress == [(0, 1, 0, 2), (0, 1, 1, 2), (1, 1, 2, 2)]


def test_search_external_errors_are_safe(tmp_path):
    from indexer.contracts import SearchRequest
    from indexer.published_retrieval import PublishedRetrieval
    from qdrant_client.http.exceptions import ResponseHandlingException

    def bad_http(*args):
        request = httpx.Request("POST", "https://secret.example/embeddings")
        raise httpx.HTTPStatusError("secret body", request=request, response=httpx.Response(401, request=request))
    publisher = SimpleNamespace(search=bad_http)
    with pytest.raises(IndexerError) as error:
        PublishedRetrieval(publisher, None).search(SearchRequest(query="질문"))
    assert error.value.code == "SEARCH_PROVIDER_ERROR" and "인증" in error.value.message
    assert "secret" not in error.value.message
    def bad_qdrant(*args):
        raise ResponseHandlingException(RuntimeError("secret detail"))
    publisher.search = bad_qdrant
    with pytest.raises(IndexerError) as error:
        PublishedRetrieval(publisher, None).search(SearchRequest(query="질문"))
    assert error.value.code == "VECTOR_DB_UNAVAILABLE" and "secret" not in error.value.message


def test_existing_contract_remains_bm25_searchable_and_dense_blocked(tmp_path):
    import json

    from indexer.dense_index import EmbeddingClient, EmbeddingProfile
    from indexer.index_publication import IndexPublisher
    from qdrant_client import QdrantClient

    def provider(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(body["input"]))]})
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        publisher = IndexPublisher(tmp_path, QdrantClient(":memory:"),
            EmbeddingClient(http, "https://example.test/v1", "key", EmbeddingProfile("p", "doc", "query", 2)))
        manifest = publisher.publish("d", [{"chunk_id": str(uuid4()), "document_id": "d", "text": "교육 계획"}])
        manifest["embedding_profile"].pop("preprocessing")
        (publisher.root / manifest["version"] / "manifest.json").write_text(json.dumps(manifest))
        (publisher.root / "active.json").write_text(json.dumps(manifest))
        assert publisher.search("교육", "bm25")["results"]
        with pytest.raises(ValueError, match="일치하지 않습니다"):
            publisher.search("교육", "dense")
        from indexer.artifact_store import ArtifactStore
        from indexer.ingestion_service import IngestionService
        source = tmp_path / "new.pdf"
        source.write_bytes(b"%PDF-test")
        service = IngestionService(ArtifactStore(tmp_path), object(), publisher)
        with pytest.raises(IndexerError) as error:
            service.enqueue(source)
        assert error.value.code == "INDEX_REBUILD_REQUIRED"
        assert service.storage.list_documents() == []
        from fastapi.testclient import TestClient

        from apps.api.main import create_app
        with TestClient(create_app(Config(tmp_path, access_key="example", external_processing=True), ingestion_service=service),
                        base_url="https://testserver") as client:
            login = client.post("/v1/auth/login", json={"access_key": "example"}, headers={"Origin": "https://testserver"})
            client.headers.update({"Origin": "https://testserver", "X-CSRF-Token": login.json()["csrf_token"]})
            versions = client.get("/v1/ingest-index-versions?profile_id=p").json()["versions"]
            assert versions[0]["dense_available"] is False
        rebuilt = publisher.rebuild_with_current_profile()
        assert rebuilt["embedding_profile"]["preprocessing"]
        assert publisher.search("교육", "dense")["results"]


def test_flat_parser_headings_only_infer_anchored_numeric_levels():
    rows = [element("heading1", "I 대학 중장기 발전전략"), element("heading1", "1. 대학의 비전 및 목표"),
            element("heading1", "1.2 대학의 비전 및 중장기 발전 목표"), element("paragraph", "교육 혁신"),
            element("heading1", "새로운 제목"), element("paragraph", "본문")]
    elements = normalize_elements(rows)
    assert elements[3]["heading_path"] == ["I 대학 중장기 발전전략", "1. 대학의 비전 및 목표", "1.2 대학의 비전 및 중장기 발전 목표"]
    assert elements[2]["heading_level_origin"] == "inferred"
    assert elements[5]["heading_path"] == ["I 대학 중장기 발전전략", "1. 대학의 비전 및 목표", "1.2 대학의 비전 및 중장기 발전 목표", "새로운 제목"]
    assert elements[4]["heading_level_origin"] == "inferred"


def test_parser_multilevel_headings_are_not_overridden():
    rows = [element("heading1", "I 대학"), element("heading2", "2. 실행"), element("heading1", "3. 다른 장")]
    elements = normalize_elements(rows)
    assert elements[2]["heading_path"] == ["3. 다른 장"]
    assert all(item["heading_level_origin"] == "parser" for item in elements)


@pytest.mark.parametrize(("query", "expected"), [
    ("서비스", "service"), ("서비스는", "service"), ("대학", "university"),
    ("예산", "budget"), ("2026", "budget"), ("AI", "ai"), ("교육혁신", "innovation"),
    ("시설", "facility"), ("과제", "project"), ("RAG", "rag"),
])
def test_korean_search_evaluation_cases(tmp_path, query, expected):
    examples = {"service": "서비스는 학생을 지원합니다", "university": "대학의 중장기 계획",
                "budget": "2026년 예산은 12억원", "ai": "AI 기반 도구를 도입합니다",
                "innovation": "교육혁신을 통한 학생 성공", "facility": "시설에 투자합니다",
                "project": "과제별 추진계획", "rag": "RAG 검색 설계"}
    index = BM25Index(tmp_path / "eval.sqlite")
    for key, text in examples.items():
        index.replace_document(key, [{"chunk_id": key, "text": text}])
    assert expected in [hit.chunk_id for hit in index.search(query, 5)]


def test_unmatched_caption_is_not_removed():
    rows = [element("caption", "설명만 있는 캡션"), element("paragraph", "본문")]
    elements = normalize_elements(rows)
    chunks = make_chunks("d", "j", elements)
    assert any("e1" in item["element_ids"] for item in chunks)
    assert not elements[0].get("search_exclusion")


def test_fallback_split_regroups_table_cells_within_budget():
    from indexer.embedding_adaptation import split_chunk
    segments = [{"row": 1, "column": i, "label": "열", "value": "짧은값", "text": "열 / 짧은값"} for i in range(1, 11)]
    original = {"chunk_id": str(uuid4()), "document_id": "d", "text": "표\n" + "\n".join(item["text"] for item in segments),
                "table_segments": segments, "element_ids": ["e1"]}
    parts = split_chunk(original, 100)
    assert len(parts) < len(segments)
    assert sum(len(item["table_cells"]) for item in parts) == len(segments)
    assert all(len(item["text"].encode()) <= 100 for item in parts)


def test_reranker_proxy_without_key_omits_authorization():
    from indexer.reranker import CohereReranker
    from indexer.search_engine import SearchHit
    def respond(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reranker = CohereReranker(client, "http://internal/v2", "", "model", lambda _: "문서")
        assert reranker.rerank("질문", [SearchHit("chunk", 1.0)], 1)[0].chunk_id == "chunk"


def test_repeated_provider_splits_keep_precise_paragraph_references():
    elements = normalize_elements([element("paragraph", "첫단락 " * 60), element("paragraph", "둘째단락 " * 60)])
    chunks = make_chunks("d", "j", elements, target_chars=1000, max_bytes=12000)
    def embed(texts):
        if any(len(text.encode()) > 100 for text in texts):
            req = httpx.Request("POST", "https://example.test")
            raise httpx.HTTPStatusError("too long", request=req,
                response=httpx.Response(400, request=req, json={"error": {"code": "context_length_exceeded"}}))
        return [[1.0] for _ in texts]
    split, _ = embed_chunks(SimpleNamespace(embed=embed), chunks, 512, 10)
    assert len(split) > 4
    assert all(("e1" in item["element_ids"]) == ("첫단락" in item["body_text"]) for item in split)
    assert all(("e2" in item["element_ids"]) == ("둘째단락" in item["body_text"]) for item in split)
    assert all(item.get("source_range_quality") == "element" for item in split)


def test_caption_is_not_excluded_when_table_context_truncates_it():
    caption = "표 1 " + "중요한설명 " * 60
    rows = [element("caption", caption),
            element("table", "", 1, "<table><tr><th>구분</th><th>값</th></tr><tr><td>시설</td><td>12</td></tr></table>")]
    elements = normalize_elements(rows)
    chunks = make_chunks("d", "j", elements, target_chars=400, overlap_chars=0, max_bytes=512)
    assert not elements[0].get("search_exclusion")
    assert any(item["element_ids"] == ["e1"] for item in chunks)


def test_heading_is_retained_when_next_element_is_excluded():
    rows = [element("heading1", "독립 제목", 1), element("header", "반복", 1), element("header", "반복", 2)]
    elements = normalize_elements(rows)
    chunks = make_chunks("d", "j", elements)
    assert any(item["element_ids"] == ["e1"] and "독립 제목" in item["text"] for item in chunks)
    assert not audit_representation(elements, chunks)["unrepresented_elements"]


def test_audit_records_truncated_heading_context():
    rows = [element("heading1", "긴제목" * 200), element("paragraph", "본문")]
    elements = normalize_elements(rows)
    chunks = make_chunks("d", "j", elements, target_chars=400, overlap_chars=0, max_bytes=512)
    audit = audit_representation(elements, chunks)
    assert audit["shortened_heading_chunks"]
    assert any("본문" in item["text"] for item in chunks)


def test_lexical_fusion_evaluation_compares_original_candidate_selection(tmp_path):
    """Trace old append-and-truncate against fusion on a fixed corpus."""
    index = BM25Index(tmp_path / 'fusion.sqlite')
    chunks = [{'chunk_id': f'noise-{i:02}', 'text': f'AI 서비스 운영 자료 {i}'} for i in range(12)]
    chunks += [{'chunk_id': 'learning', 'text': '학습지원시스템을 구축합니다'},
               {'chunk_id': 'quota', 'text': 'Provider Allowlist를 설정합니다'},
               {'chunk_id': 'korean', 'text': '교육혁신을 지원합니다'}]
    index.replace_document('d', chunks)
    cases = [('AI 학습지원시스템', 'learning'), ('AI Allowlist', 'quota'), ('AI 교육혁신', 'korean')]
    old_recall = new_recall = 0
    with sqlite3.connect(index.path) as db:
        for query, expected in cases:
            import re
            terms = re.findall(r'[^\W_]+', query, flags=re.UNICODE)
            match = ' OR '.join('"' + term + '"' for term in terms)
            old = [row[0] for row in db.execute('SELECT chunk_id FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks), chunk_id LIMIT 5', (match,))]
            substring = ' OR '.join('"' + term + '"' for term in terms if len(term) >= 3)
            extra = [row[0] for row in db.execute('SELECT chunk_id FROM substrings WHERE substrings MATCH ? ORDER BY bm25(substrings), chunk_id LIMIT 5', (substring,))]
            old = (old + [key for key in extra if key not in old])[:5]
            new = [hit.chunk_id for hit in index.search(query, 5)]
            old_recall += expected in old
            new_recall += expected in new
            assert expected in extra and expected in new
            assert len(new) <= 5 and len(set(new)) == len(new)
    assert old_recall == 0 and new_recall == len(cases)


def test_lexical_fusion_is_stable_deduplicated_and_document_scoped(tmp_path):
    index = BM25Index(tmp_path / 'stable.sqlite')
    index.replace_document('a', [{'chunk_id': key, 'text': 'AI 학습지원시스템 서비스는 2026년'} for key in ('z', 'b', 'a')])
    index.replace_document('b', [{'chunk_id': 'other', 'text': 'AI 학습지원시스템 서비스는 2026년'}])
    for query in ('AI 학습지원시스템', '서비스 AI', '2026 AI', normalize('NFD', '서비스 AI')):
        hits = index.search(query, 3, 'a')
        assert [hit.chunk_id for hit in hits] == ['a', 'b', 'z']
        assert len({hit.chunk_id for hit in hits}) == 3
        assert hits == index.search(query, 3, 'a')
    assert index.search('학습지원시스템', 3, 'missing') == []


def test_fusion_prefers_more_query_coverage_and_handles_mixed_terms(tmp_path):
    index = BM25Index(tmp_path / 'coverage.sqlite')
    index.replace_document('d', [
        {'chunk_id': 'all', 'text': 'RAG 기반 서비스 운영과 2026년 계획'},
        {'chunk_id': 'partial', 'text': 'RAG 기반 플랫폼 개선'},
        {'chunk_id': 'other', 'text': '서비스 운영 문서'},
    ])
    for query in ('RAG 서비스 2026', normalize('NFD', 'RAG 서비스 2026'), 'rag 서비스 2026'):
        hits = index.search(query, 3)
        assert hits[0].chunk_id == 'all'
        assert len(hits) == len({hit.chunk_id for hit in hits})
    assert index.search('RAG 서비스 2026', 0) == []


def test_unicode61_exact_matches_receive_coverage_without_changing_source(tmp_path):
    index = BM25Index(tmp_path / 'unicode.sqlite')
    index.replace_document('d', [
        {'chunk_id': 'plain', 'text': 'resume'},
        {'chunk_id': 'accented', 'text': 'Résumé'},
        {'chunk_id': 'mixed', 'text': 'RÉSUMÉ 지원 2026'},
        {'chunk_id': 'unrelated', 'text': 'summer 2026'},
    ])
    scores = {hit.chunk_id: hit.score for hit in index.search('resume', 5)}
    assert {'plain', 'accented', 'mixed'} <= scores.keys()
    assert min(scores[key] for key in ('plain', 'accented', 'mixed')) >= 1
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT text FROM chunks WHERE chunk_id='accented'").fetchone()[0] == 'Résumé'
    mixed = index.search('resume 지원 2026', 5)
    assert mixed[0].chunk_id == 'mixed'
    assert index.search(normalize('NFD', 'Résumé'), 5)


def test_historical_unicode61_index_retains_rank_and_raw_scores(tmp_path):
    path = tmp_path / 'historic.sqlite'
    with sqlite3.connect(path) as db:
        db.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text, tokenize='unicode61')")
        db.executemany('INSERT INTO chunks VALUES (?, ?, ?)', [
            ('a', 'd', 'Résumé and education'), ('b', 'd', 'resume resume example'),
            ('c', 'other', 'resume other')])
        expected = [(key, -score) for key, score in db.execute(
            "SELECT chunk_id, bm25(chunks) FROM chunks WHERE chunks MATCH ? AND document_id=? ORDER BY bm25(chunks), chunk_id, rowid LIMIT ?",
            ('"resume"', 'd', 5))]
    index = BM25Index(path)
    assert not index.has_substrings
    actual = index.search('resume', 5, 'd')
    assert [(hit.chunk_id, hit.score) for hit in actual] == expected
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='substrings'").fetchone()


@pytest.mark.parametrize(('query', 'expected'), [
    ('quota', 'quota'), ('provider quota policy', 'exact'), ('access-control', 'access'),
    ('서비스 예산', 'service'), ('2026', 'year'),
])
def test_existing_exact_search_ranks_are_preserved_on_fixed_corpus(tmp_path, query, expected):
    import re

    index = BM25Index(tmp_path / 'baseline.sqlite')
    index.replace_document('d', [
        {'chunk_id': 'quota', 'text': 'quota limit'},
        {'chunk_id': 'exact', 'text': 'provider quota policy'},
        {'chunk_id': 'access', 'text': 'access control'},
        {'chunk_id': 'service', 'text': '서비스 예산'},
        {'chunk_id': 'year', 'text': '2026'},
        {'chunk_id': 'other', 'text': '다른 문서 보고서'},
    ])
    terms = re.findall(r'[^\W_]+', query, flags=re.UNICODE)
    match = ' OR '.join('"' + term + '"' for term in terms)
    with sqlite3.connect(index.path) as db:
        old = [row[0] for row in db.execute(
            'SELECT chunk_id FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks), chunk_id LIMIT 5', (match,))]
    new = [hit.chunk_id for hit in index.search(query, 5)]
    assert expected in old and expected in new
    assert new.index(expected) <= old.index(expected)


def test_fusion_does_not_add_python_only_unicode_matches(tmp_path):
    index = BM25Index(tmp_path / 'unicode-distinct.sqlite')
    index.replace_document('d', [{'chunk_id': 'ss', 'text': 'AI strasse'},
                                 {'chunk_id': 'sharp-s', 'text': 'AI straße'}])
    scores = {hit.chunk_id: hit.score for hit in index.search('AI strasse', 5)}
    assert scores['ss'] > 1 and scores['sharp-s'] < 0.3
    with sqlite3.connect(index.path) as db:
        assert db.execute('SELECT COUNT(*) FROM chunks WHERE chunks MATCH ?', ('"strasse"',)).fetchone()[0] == 1


def test_exact_and_substring_matches_compete_without_losing_exact_result(tmp_path):
    index = BM25Index(tmp_path / 'exact-substring.sqlite')
    index.replace_document('d', [{'chunk_id': 'exact', 'text': 'budget planning'},
                                 {'chunk_id': 'partial', 'text': 'budgetary planning'},
                                 {'chunk_id': 'unrelated', 'text': 'planning'}])
    hits = index.search('budget planning', 3)
    assert [hit.chunk_id for hit in hits][:2] == ['exact', 'partial']
