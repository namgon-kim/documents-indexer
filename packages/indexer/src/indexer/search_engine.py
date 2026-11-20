"""검색 방식별 후보와 결합을 분리한 검색 계약."""

from dataclasses import dataclass
from typing import Protocol


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
               rerank: bool = False) -> list[SearchHit]:
        query = query.strip()
        if not query or not 1 <= limit <= 100:
            raise ValueError("검색어 또는 결과 수를 확인해 주세요")
        if method not in {"dense", "bm25", "hybrid"}:
            raise ValueError("지원하지 않는 검색 방식입니다")
        if rerank and self.reranker is None:
            raise ValueError("reranker가 설정되지 않았습니다")
        candidates = min(limit * 5, 100)
        if method == "hybrid":
            dense = self.dense.search(query, candidates, document_id)
            sparse = self.bm25.search(query, candidates, document_id)
            hits = reciprocal_rank_fusion([dense, sparse], candidates)
        else:
            retriever = self.dense if method == "dense" else self.bm25
            hits = retriever.search(query, candidates, document_id)
        if rerank:
            result = self.reranker.rerank(query, hits, limit)
            original = {hit.chunk_id for hit in hits}
            if len(result) > limit or len({hit.chunk_id for hit in result}) != len(result) or any(hit.chunk_id not in original for hit in result):
                raise ValueError("reranker 응답에 유효하지 않은 후보가 있습니다")
            return result
        return hits[:limit]
