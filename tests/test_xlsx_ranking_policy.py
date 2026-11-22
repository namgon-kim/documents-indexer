"""XLSX-specific ranking must not change other published search paths."""
import json
from uuid import uuid4

import pytest

from indexer.contracts import SearchRequest
from indexer.published_retrieval import PublishedRetrieval
from indexer.search_engine import SearchHit, SearchService


class _Publisher:
    def __init__(self, root):
        self.root = root
        self.calls = []
        folder = root / "version"
        folder.mkdir(parents=True)
        (folder / "chunks.json").write_text("[]", encoding="utf-8")

    def search(self, query, method, limit, document_id, rerank, version, **ranking):
        self.calls.append({
            "query": query, "method": method, "limit": limit,
            "document_id": document_id, "rerank": rerank,
            "version": version, "ranking": ranking,
        })
        return {"index_version": "version", "results": [], "search_counts": {}}


class _Storage:
    def __init__(self, fmt):
        self.fmt = fmt
        self.lookups = []

    def load_document(self, document_id):
        self.lookups.append(document_id)
        if self.fmt == "missing":
            raise FileNotFoundError(document_id)
        return {"document_id": document_id, "format": self.fmt}


@pytest.mark.parametrize(
    ("fmt", "scoped", "expected"),
    [
        ("xlsx", True, (50, 10)),
        ("pdf", True, (25, 5)),
        ("hwp", True, (25, 5)),
        ("docx", True, (25, 5)),
        ("missing", True, (25, 5)),
        ("xlsx", False, (25, 5)),
    ],
)
def test_ranking_only_changes_for_explicit_xlsx(tmp_path, fmt, scoped, expected):
    publisher = _Publisher(tmp_path)
    storage = _Storage(fmt)
    common_ranking = {"retrieval_top_n": 25, "rerank_top_n": 5}
    retrieval = PublishedRetrieval(publisher, storage, ranking=common_ranking)

    document_id = uuid4() if scoped else None
    result = retrieval.search(SearchRequest(
        query="기능 설명", method="hybrid", document_id=document_id, rerank=True,
    ))

    call = publisher.calls[0]
    assert (call["ranking"]["retrieval_top_n"], call["ranking"]["rerank_top_n"]) == expected
    assert call["limit"] == 20  # Default chat top_k=None must allow ten results.
    assert call["document_id"] == (str(document_id) if document_id else None)
    assert result["results"] == []
    assert storage.lookups == ([str(document_id)] if scoped else [])
    assert common_ranking == {"retrieval_top_n": 25, "rerank_top_n": 5}


def test_xlsx_explicit_result_limit_and_reranker_disabled(tmp_path):
    publisher = _Publisher(tmp_path)
    retrieval = PublishedRetrieval(publisher, _Storage("xlsx"),
                                   ranking={"retrieval_top_n": 25, "rerank_top_n": 5})
    doc = uuid4()
    retrieval.search(SearchRequest(query="기능", document_id=doc, top_k=5, rerank=True))
    retrieval.search(SearchRequest(query="기능", document_id=doc, top_k=5, rerank=False))
    assert [call["limit"] for call in publisher.calls] == [5, 5]
    assert [call["rerank"] for call in publisher.calls] == [True, False]
    assert all(call["ranking"] == {"retrieval_top_n": 50, "rerank_top_n": 10}
               for call in publisher.calls)


class _Retriever:
    def search(self, query, limit, document_id=None):
        return [SearchHit(f"chunk-{i:02d}", float(limit - i))
                for i in range(limit)]


class _Reranker:
    def __init__(self):
        self.calls = []

    def rerank(self, query, candidates, limit):
        self.calls.append((len(candidates), limit))
        return candidates[:limit]


def test_search_engine_50_10_can_return_ten_and_preserves_explicit_limit():
    reranker = _Reranker()
    engine = SearchService(_Retriever(), _Retriever(), reranker)
    counts = {}
    result = engine.search("기능", "hybrid", limit=20, rerank=True,
                           retrieval_top_n=50, rerank_top_n=10, counts=counts)
    assert len(result) == 10
    assert reranker.calls[-1] == (50, 10)
    assert counts["retrieval_top_n"] == 50
    assert counts["rerank_top_n"] == 10
    assert counts["result_limit"] == 10

    result = engine.search("기능", "hybrid", limit=5, rerank=True,
                           retrieval_top_n=50, rerank_top_n=10)
    assert len(result) == 5
    assert reranker.calls[-1] == (50, 10)
