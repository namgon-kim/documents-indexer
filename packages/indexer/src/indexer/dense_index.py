"""명시적인 임베딩 프로필에 결합된 Qdrant Dense 색인."""

import math
from dataclasses import dataclass

import httpx
from qdrant_client import models

from indexer.embedding_input import byte_size, embedding_text
from indexer.search_engine import SearchHit


@dataclass(frozen=True)
class EmbeddingProfile:
    profile_id: str
    document_model: str
    query_model: str
    dimensions: int
    max_input_bytes: int = 3800
    batch_size: int = 24


class EmbeddingClient:
    def __init__(self, client: httpx.Client, base_url: str, api_key: str, profile: EmbeddingProfile):
        self.client, self.base_url, self.api_key, self.profile = client, base_url.rstrip("/"), api_key, profile
        if not profile.profile_id or not profile.document_model or not profile.query_model or profile.dimensions < 1 or profile.max_input_bytes < 256 or profile.batch_size < 1:
            raise ValueError("임베딩 프로필 설정을 확인해 주세요")

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        texts = [embedding_text(text) for text in texts]
        if not texts or any(not item.strip() for item in texts):
            raise ValueError("임베딩 입력이 비어 있습니다")
        if any(byte_size(text) > self.profile.max_input_bytes for text in texts):
            raise ValueError("질문이 임베딩 입력 바이트 제한을 넘었습니다. 질문을 줄여 주세요." if query else "임베딩 입력 바이트 제한을 초과했습니다")
        if len(texts) > self.profile.batch_size:
            raise ValueError("임베딩 요청의 입력 수 제한을 초과했습니다")
        model = self.profile.query_model if query else self.profile.document_model
        response = self.client.post(self.base_url + "/embeddings", headers={"Authorization": f"Bearer {self.api_key}"},
                                    json={"model": model, "input": texts}, timeout=90)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("model") != model or not isinstance(payload.get("data"), list) or len(payload["data"]) != len(texts):
            raise ValueError("임베딩 모델 또는 응답 개수가 일치하지 않습니다")
        vectors = [None] * len(texts)
        for item in payload["data"]:
            if not isinstance(item, dict):
                raise ValueError("임베딩 응답 형식이 올바르지 않습니다")  # noqa: TRY004
            index, vector = item.get("index"), item.get("embedding")
            if (type(index) is not int or not 0 <= index < len(texts) or vectors[index] is not None
                or not isinstance(vector, list) or len(vector) != self.profile.dimensions
                or not all(type(n) in (int, float) and math.isfinite(n) for n in vector) or not any(vector)):
                raise ValueError("임베딩 벡터가 유효하지 않습니다")
            vectors[index] = vector
        return vectors


class DenseIndex:
    def __init__(self, client, collection: str, embedder: EmbeddingClient):
        self.client, self.collection, self.embedder = client, collection, embedder

    def build(self, chunks: list[dict], vectors: list[list[float]] | None = None):
        if self.client.collection_exists(self.collection):
            raise ValueError("이미 존재하는 색인입니다")
        if vectors is None:
            vectors = []
            for offset in range(0, len(chunks), self.embedder.profile.batch_size):
                vectors.extend(self.embedder.embed([chunk["text"] for chunk in chunks[offset:offset + self.embedder.profile.batch_size]]))
        if len(vectors) != len(chunks):
            raise ValueError("검색 청크와 임베딩 수가 일치하지 않습니다")
        self.client.create_collection(self.collection, vectors_config=models.VectorParams(size=self.embedder.profile.dimensions, distance=models.Distance.COSINE))
        try:
            points = [models.PointStruct(id=chunk["chunk_id"], vector=vector,
                                        payload={"document_id": chunk["document_id"], "chunk_id": chunk["chunk_id"], "text": chunk["text"]})
                      for chunk, vector in zip(chunks, vectors, strict=True)]
            for offset in range(0, len(points), 100):
                self.client.upsert(self.collection, points[offset:offset + 100], wait=True)
            if self.client.count(self.collection, exact=True).count != len(points):
                raise ValueError("색인된 벡터 수가 일치하지 않습니다")
        except Exception:
            self.client.delete_collection(self.collection)
            raise

    def search(self, query: str, limit: int, document_id: str | None = None) -> list[SearchHit]:
        vector = self.embedder.embed([query], query=True)[0]
        match = models.Filter(must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]) if document_id else None
        response = self.client.query_points(self.collection, query=vector, query_filter=match, limit=limit, with_payload=True)
        return [SearchHit(str(item.payload["chunk_id"]), item.score) for item in response.points]
