"""Cohere v2 rerank 어댑터."""

import math
from collections.abc import Callable

import httpx

from indexer.search_engine import SearchHit


class CohereReranker:
    def __init__(self, client: httpx.Client, base_url: str, api_key: str, model: str,
                 load_text: Callable[[str], str]):
        self.client, self.base_url, self.api_key, self.model = client, base_url.rstrip("/"), api_key, model
        self.load_text = load_text

    def rerank(self, query: str, candidates: list[SearchHit], limit: int) -> list[SearchHit]:
        if not candidates:
            return []
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = self.client.post(self.base_url + "/rerank", headers=headers,
                                    json={"model": self.model, "query": query,
                                          "documents": [self.load_text(hit.chunk_id) for hit in candidates],
                                          "top_n": min(limit, len(candidates))}, timeout=90)
        response.raise_for_status()
        results = response.json().get("results")
        if not isinstance(results, list) or len(results) > min(limit, len(candidates)):
            raise ValueError("reranker 응답이 유효하지 않습니다")
        seen, hits = set(), []
        for item in results:
            index, score = item.get("index"), item.get("relevance_score")
            if type(index) is not int or not 0 <= index < len(candidates) or index in seen or type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError("reranker 응답 항목이 유효하지 않습니다")
            seen.add(index)
            hits.append(SearchHit(candidates[index].chunk_id, float(score)))
        return hits
