"""버전별 SQLite FTS5 색인. 신규 색인은 어절과 부분 문자열을 함께 검색한다."""

import re
import sqlite3
from pathlib import Path

from indexer import diagnostics
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
            diagnostics.ranking('bm25_final', [])
            return []
        if self.has_substrings:
            terms = list(dict.fromkeys(terms))

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
            diagnostics.ranking('bm25_unicode61', [hit for hit, _ in exact])
            # Preserve the historical index's tokenizer and raw score contract.
            if not self.has_substrings:
                result = [hit for hit, _ in exact]
                diagnostics.ranking('bm25_final', result)
                return result
            channels = [exact]
            long = [term for term in terms if len(term) >= 3]
            short = [term for term in terms if len(term) < 3]
            if long:
                channels.append(run(db, 'substrings', ' OR '.join('"' + term + '"' for term in long)))
                diagnostics.ranking('bm25_trigram', [hit for hit, _ in channels[-1]])
            if short:
                channels.append(run(db, 'chunks', ' OR '.join('"' + term + '"*' for term in short)))
                diagnostics.ranking('bm25_prefix', [hit for hit, _ in channels[-1]])

        # FTS scores from different tokenizers are not comparable. Fuse ranks,
        # then prioritize how much of the query is actually present in each
        # candidate. Length weighting favors longer matched terms over short
        # generic tokens; rank fusion resolves comparable coverage without
        # adding incomparable raw BM25 values. This is a ranking heuristic.
        texts = {hit.chunk_id: text for channel in channels for hit, text in channel}
        if not texts:
            diagnostics.ranking('bm25_final', [])
            return []
        ranks = reciprocal_rank_fusion([[hit for hit, _ in channel] for channel in channels], len(texts))
        total = sum(len(term) for term in terms)
        covered_terms = {hit.chunk_id: set() for hit in ranks}
        # Reuse the actual index tokenizers for bounded candidate texts. This
        # avoids both false negatives and false positives from Python's Unicode
        # folding, without rewriting source text or changing stored indexes.
        with sqlite3.connect(':memory:') as db:
            db.execute("CREATE VIRTUAL TABLE candidates USING fts5(chunk_id UNINDEXED, text, tokenize='unicode61')")
            db.executemany('INSERT INTO candidates(chunk_id, text) VALUES (?, ?)', texts.items())
            if long:
                db.execute("CREATE VIRTUAL TABLE candidate_substrings USING fts5(chunk_id UNINDEXED, text, tokenize='trigram')")
                db.executemany('INSERT INTO candidate_substrings(chunk_id, text) VALUES (?, ?)', texts.items())
            for index, term in enumerate(terms):
                match = '"' + term + '"' + ('*' if len(term) < 3 else '')
                for (chunk_id,) in db.execute('SELECT chunk_id FROM candidates WHERE candidates MATCH ?', (match,)):
                    covered_terms[chunk_id].add(index)
                if len(term) >= 3:
                    for (chunk_id,) in db.execute('SELECT chunk_id FROM candidate_substrings WHERE candidate_substrings MATCH ?', (match,)):
                        covered_terms[chunk_id].add(index)
        scored = []
        for hit in ranks:
            covered = sum(len(terms[index]) for index in covered_terms[hit.chunk_id])
            score = covered / total + hit.score / 100
            scored.append(SearchHit(hit.chunk_id, score))
        result = sorted(scored, key=lambda hit: (-hit.score, hit.chunk_id))[:limit]
        diagnostics.ranking('bm25_final', result)
        return result
