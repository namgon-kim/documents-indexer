"""버전별 SQLite FTS5 색인. 신규 색인은 어절과 부분 문자열을 함께 검색한다."""

import re
import sqlite3
from pathlib import Path

from indexer.embedding_input import embedding_text
from indexer.search_engine import SearchHit


class BM25Index:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_index = not path.exists()
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text, tokenize='unicode61')")
            if new_index:
                db.execute("CREATE VIRTUAL TABLE substrings USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, text, tokenize='trigram')")
            self.has_substrings = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='substrings'").fetchone())

    def replace_document(self, document_id: str, chunks: list[dict]):
        with sqlite3.connect(self.path) as db:
            for table in (['chunks', 'substrings'] if self.has_substrings else ['chunks']):
                db.execute(f"DELETE FROM {table} WHERE document_id=?", (document_id,))
                db.executemany(f"INSERT INTO {table}(chunk_id, document_id, text) VALUES (?, ?, ?)",
                    [(item["chunk_id"], document_id, embedding_text(item["text"]) if self.has_substrings else item["text"]) for item in chunks])

    def search(self, query: str, limit: int, document_id: str | None = None) -> list[SearchHit]:
        query = embedding_text(query) if self.has_substrings else query
        terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if not terms:
            return []
        def run(db, table, match):
            sql = f"SELECT chunk_id, bm25({table}) AS rank FROM {table} WHERE {table} MATCH ?"
            args = [match]
            if document_id is not None:
                sql += " AND document_id=?"
                args.append(document_id)
            return [SearchHit(chunk_id, -rank) for chunk_id, rank in db.execute(sql + " ORDER BY rank LIMIT ?", [*args, limit])]
        with sqlite3.connect(self.path) as db:
            hits = run(db, 'chunks', ' OR '.join('"' + term + '"' for term in terms))
            if self.has_substrings:
                seen = {item.chunk_id for item in hits}
                long = [term for term in terms if len(term) >= 3]
                short = [term for term in terms if len(term) < 3]
                extra = []
                if long:
                    extra += run(db, 'substrings', ' OR '.join('"' + term + '"' for term in long))
                if short:
                    extra += run(db, 'chunks', ' OR '.join('"' + term + '"*' for term in short))
                hits.extend(hit for hit in extra if hit.chunk_id not in seen and not seen.add(hit.chunk_id))
            return hits[:limit]
