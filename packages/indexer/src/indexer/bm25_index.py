"""버전별 SQLite FTS5 색인. 신규 색인은 어절과 부분 문자열을 함께 검색한다."""

import re
import sqlite3
from pathlib import Path

from indexer.embedding_input import embedding_text
from indexer.search_engine import SearchHit, reciprocal_rank_fusion


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
        if limit < 1:
            return []
        query = embedding_text(query) if self.has_substrings else query
        terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if not terms:
            return []
        if self.has_substrings:
            terms = list(dict.fromkeys(term.casefold() for term in terms))

        def run(db, table, match):
            sql = f"SELECT chunk_id, text, bm25({table}) AS rank FROM {table} WHERE {table} MATCH ?"
            args = [match]
            if document_id is not None:
                sql += " AND document_id=?"
                args.append(document_id)
            rows = db.execute(sql + " ORDER BY rank, chunk_id, rowid LIMIT ?", [*args, limit])
            return [(SearchHit(chunk_id, -rank), text) for chunk_id, text, rank in rows]

        with sqlite3.connect(self.path) as db:
            exact = run(db, 'chunks', ' OR '.join('"' + term + '"' for term in terms))
            # Preserve the historical index's tokenizer and raw score contract.
            if not self.has_substrings:
                return [hit for hit, _ in exact]
            channels = [exact]
            long = [term for term in terms if len(term) >= 3]
            short = [term for term in terms if len(term) < 3]
            if long:
                channels.append(run(db, 'substrings', ' OR '.join('"' + term + '"' for term in long)))
            if short:
                channels.append(run(db, 'chunks', ' OR '.join('"' + term + '"*' for term in short)))

        # FTS scores from different tokenizers are not comparable. Fuse ranks,
        # then prioritize how much of the query is actually present in each
        # candidate. Length weighting favors longer matched terms over short
        # generic tokens; rank fusion resolves comparable coverage without
        # adding incomparable raw BM25 values. This is a ranking heuristic.
        texts = {hit.chunk_id: text for channel in channels for hit, text in channel}
        if not texts:
            return []
        ranks = reciprocal_rank_fusion([[hit for hit, _ in channel] for channel in channels], len(texts))
        total = sum(len(term) for term in terms)
        scored = []
        for hit in ranks:
            text = embedding_text(texts[hit.chunk_id]).casefold()
            words = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
            covered = sum(len(term) for term in terms if (
                term in text if len(term) >= 3 else any(word.startswith(term) for word in words)))
            score = covered / total + hit.score / 100
            scored.append(SearchHit(hit.chunk_id, score))
        return sorted(scored, key=lambda hit: (-hit.score, hit.chunk_id))[:limit]
