"""검색 방식별 후보와 결합을 분리한 검색 계약."""

from dataclasses import dataclass
from typing import Protocol

MAX_RETRIEVAL_TOP_N = 100
MAX_RESULT_LIMIT = 20
DEFAULT_RETRIEVAL_TOP_N = 25
DEFAULT_RERANK_TOP_N = 5


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    score: float


class Retriever(Protocol):
    def search(self, query: str, limit: int, document_id: str | None = None) -> list[SearchHit]: ...


class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[SearchHit], limit: int) -> list[SearchHit]: ...


def reciprocal_rank_fusion(rankings: list[list[SearchHit]], limit: int, k: int = 60) -> list[SearchHit]:
    if limit < 1 or k < 1:
        raise ValueError("검색 결과 수와 RRF k는 양수여야 합니다")
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for rank, hit in enumerate(ranking, 1):
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + 1 / (k + rank)
    return [SearchHit(key, score) for key, score in
            sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]]


class SearchService:
    def __init__(self, dense: Retriever, bm25: Retriever, reranker: Reranker | None = None):
        self.dense = dense
        self.bm25 = bm25
        self.reranker = reranker

    def search(self, query: str, method: str, limit: int = 5, document_id: str | None = None,
               rerank: bool = False, *, retrieval_top_n: int = DEFAULT_RETRIEVAL_TOP_N,
               rerank_top_n: int = DEFAULT_RERANK_TOP_N, counts: dict | None = None) -> list[SearchHit]:
        query = query.strip()
        if not query or type(limit) is not int or not 1 <= limit <= MAX_RETRIEVAL_TOP_N:
            raise ValueError("검색어 또는 결과 수를 확인해 주세요")
        if type(retrieval_top_n) is not int or not 1 <= retrieval_top_n <= MAX_RETRIEVAL_TOP_N:
            raise ValueError("검색 후보 수를 확인해 주세요")
        if type(rerank_top_n) is not int or not 1 <= rerank_top_n <= retrieval_top_n:
            raise ValueError("리랭킹 결과 수는 검색 후보 수 이하여야 합니다")
        if method not in {"dense", "bm25", "hybrid"}:
            raise ValueError("지원하지 않는 검색 방식입니다")
        if rerank and self.reranker is None:
            raise ValueError("reranker가 설정되지 않았습니다")

        if method == "hybrid":
            dense = self.dense.search(query, retrieval_top_n, document_id)
            sparse = self.bm25.search(query, retrieval_top_n, document_id)
            hits = reciprocal_rank_fusion([dense[:retrieval_top_n], sparse[:retrieval_top_n]], retrieval_top_n)
        else:
            retriever = self.dense if method == "dense" else self.bm25
            candidates = retriever.search(query, retrieval_top_n, document_id)
            unique = {}
            for hit in candidates[:retrieval_top_n]:
                unique.setdefault(hit.chunk_id, hit)
            hits = list(unique.values())
        retrieved_count = len(hits)
        rerank_input_count = retrieved_count if rerank else 0
        reranked_count = 0
        if rerank and hits:
            take = min(rerank_top_n, retrieved_count)
            result = self.reranker.rerank(query, hits, take)
            original = {hit.chunk_id for hit in hits}
            if len(result) > take or len({hit.chunk_id for hit in result}) != len(result) or any(hit.chunk_id not in original for hit in result):
                raise ValueError("reranker 응답에 유효하지 않은 후보가 있습니다")
            hits = result
            reranked_count = len(hits)

        result_limit = min(limit, rerank_top_n if rerank else retrieval_top_n)
        if counts is not None:
            counts.update(retrieval_top_n=retrieval_top_n, rerank_top_n=rerank_top_n if rerank else None,
                          result_limit=result_limit, retrieved_count=retrieved_count,
                          rerank_input_count=rerank_input_count, reranked_count=reranked_count)
        return hits[:result_limit]
