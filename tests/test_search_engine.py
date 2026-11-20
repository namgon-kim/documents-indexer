import pytest
from indexer.bm25_index import BM25Index
from indexer.search_engine import SearchHit, SearchService, reciprocal_rank_fusion


def test_rrf_merges_rankings_and_deduplicates():
    result = reciprocal_rank_fusion([[SearchHit("a", 1), SearchHit("b", 0.5)],
                                     [SearchHit("b", 3), SearchHit("c", 2)]], 3)
    assert [hit.chunk_id for hit in result] == ["b", "a", "c"]


def test_search_selects_retrievers_and_requires_reranker():
    class Backend:
        def __init__(self, name):
            self.name = name
            self.calls = 0
        def search(self, query, limit, document_id=None):
            self.calls += 1
            return [SearchHit(self.name, 1)]
    dense, bm25 = Backend("dense"), Backend("bm25")
    service = SearchService(dense, bm25)
    assert service.search("질문", "bm25")[0].chunk_id == "bm25"
    assert dense.calls == 0 and bm25.calls == 1
    assert len(service.search("질문", "hybrid")) == 2
    with pytest.raises(ValueError):
        service.search("질문", "dense", rerank=True)


def test_bm25_indexes_and_replaces_document(tmp_path):
    index = BM25Index(tmp_path / "version.sqlite")
    index.replace_document("doc", [{"chunk_id": "a", "text": "대학 교육혁신 전략"}])
    assert index.search("교육혁신", 5, "doc")[0].chunk_id == "a"
    assert index.search("교육혁신", 5, "other") == []
    index.replace_document("doc", [{"chunk_id": "b", "text": "산학협력"}])
    assert index.search("교육혁신", 5) == []
    assert index.search("산학협력", 5)[0].chunk_id == "b"
