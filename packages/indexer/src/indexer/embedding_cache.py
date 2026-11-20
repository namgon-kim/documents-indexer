"""입력과 임베딩 공간이 같은 문서 벡터만 재사용한다."""

import hashlib
import json
import math
import sqlite3
from pathlib import Path


class EmbeddingCache:
    def __init__(self, path: Path, embedder, contract: dict):
        self.path, self.embedder = path, embedder
        self.contract = json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS vectors (cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL)")

    def _key(self, text: str) -> str:
        data = json.dumps([self.contract, "document", text], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def embed(self, texts: list[str]) -> list[list[float]]:
        keys = [self._key(text) for text in texts]
        vectors = {}
        with sqlite3.connect(self.path) as db:
            for key in set(keys):
                row = db.execute("SELECT vector FROM vectors WHERE cache_key=?", (key,)).fetchone()
                if row:
                    try:
                        vector = json.loads(row[0])
                        if (isinstance(vector, list) and len(vector) == self.embedder.profile.dimensions
                            and all(type(n) in (float, int) and math.isfinite(n) for n in vector)
                            and any(vector)):
                            vectors[key] = vector
                    except (TypeError, ValueError):
                        pass
            missing = {key: text for key, text in zip(keys, texts, strict=True) if key not in vectors}
            items = list(missing.items())
            for offset in range(0, len(items), self.embedder.profile.batch_size):
                batch = items[offset:offset + self.embedder.profile.batch_size]
                result = self.embedder.embed([text for _, text in batch])
                if len(result) != len(batch):
                    raise ValueError("임베딩 응답 개수가 일치하지 않습니다")
                for (key, _), vector in zip(batch, result, strict=True):
                    vectors[key] = vector
                    db.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?)", (key, json.dumps(vector, allow_nan=False)))
                db.commit()
        return [vectors[key] for key in keys]
