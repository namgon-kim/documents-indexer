"""버전별 SQLite FTS5 색인. 신규 색인은 어절과 부분 문자열을 함께 검색한다."""

import re
import sqlite3
from pathlib import Path
from unicodedata import normalize

from indexer import diagnostics
from indexer.embedding_input import embedding_text
from indexer.search_engine import SearchHit, reciprocal_rank_fusion

# Preserve multi-part identifiers separately from ordinary tokenizer terms.
_IDENTIFIER = re.compile(r'(?<![A-Za-z0-9_.‐‑–-])(?:[A-Za-z]*\d+[A-Za-z0-9]*|[A-Za-z]{1,20})(?:\s*[-‐‑–]\s*[A-Za-z0-9]+|\.[A-Za-z0-9]+)+(?![A-Za-z0-9_]|[-‐‑–]\s*[A-Za-z0-9]|\.[A-Za-z0-9])')
_SEPARATOR = re.compile(r'\s*([-‐‑–.])\s*')


def identifiers(text):
    values = [canonical_identifier(match.group())
              for match in _IDENTIFIER.finditer(normalize('NFKC', embedding_text(text)))]
    # A plain decimal is a quantity, not enough evidence of an identifier.
    return [value for value in values if any(character.isdigit() for character in value)
            and ('-' in value or value.count('.') >= 2 or any(character.isalpha() for character in value))]


def canonical_identifier(value):
    return _SEPARATOR.sub(lambda match: '-' if match.group(1) != '.' else '.', normalize('NFKC', value)).casefold()


def prioritize_identifiers(hits, texts, wanted):
    if not wanted:
        return hits
    wanted = set(wanted)
    return sorted(hits, key=lambda hit: -len(wanted.intersection(identifiers(texts.get(hit.chunk_id, '')))))


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
        codes = identifiers(query)
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

        fallback = []
        with sqlite3.connect(self.path) as db:
            exact = run(db, 'chunks', ' OR '.join('"' + term + '"' for term in terms))
            diagnostics.ranking('bm25_unicode61', [hit for hit, _ in exact])
            distinct_codes = list(dict.fromkeys(codes))
            # Bound only the supplementary query; ordinary term retrieval is unchanged.
            selected_codes = distinct_codes[:64]
            phrases = []
            for code in selected_codes:
                tokens = re.findall(r'[A-Za-z0-9]+', code)
                wide_tokens = [''.join(chr(ord(char) + 0xFEE0) if char.isascii() and char.isalnum() else char
                                       for char in token) for token in tokens]
                phrases.extend('"' + ' '.join(parts) + '"' for parts in (tokens, wide_tokens))
            phrase_hits = run(db, 'chunks', ' OR '.join(phrases)) if phrases else []
            if phrases:
                diagnostics.ranking('bm25_identifier_tokens', [hit for hit, _ in phrase_hits])
            # Preserve the historical index's tokenizer and raw score contract.
            if not self.has_substrings:
                combined = {hit.chunk_id: (hit, text) for hit, text in exact}
                for hit, text in phrase_hits:
                    combined.setdefault(hit.chunk_id, (hit, text))
                result = prioritize_identifiers([hit for hit, _ in combined.values()],
                                                {hit.chunk_id: text for hit, text in combined.values()}, codes)[:limit]
                diagnostics.ranking('bm25_final', result)
                return result
            channels = [exact]
            if phrase_hits:
                channels.append(phrase_hits)
            # Trigram can retrieve the full identifier even when shared numeric
            # terms crowd the ordinary token channel. No new index is required.
            if codes:
                for code in selected_codes:
                    if len(code) >= 3:
                        wide_alnum = ''.join(chr(ord(char) + 0xFEE0) if char.isascii() and char.isalnum() else char
                                             for char in code)
                        wide_all = wide_alnum.translate(str.maketrans({'-': '－', '.': '．'}))
                        variants = list(dict.fromkeys((code, wide_alnum, wide_all)))
                        channels.append(run(db, 'substrings', ' OR '.join('"' + value + '"' for value in variants)))
                        diagnostics.ranking('bm25_identifier', [hit for hit, _ in channels[-1]])
            long = [term for term in terms if len(term) >= 3]
            short = [term for term in terms if len(term) < 3]
            if long:
                channels.append(run(db, 'substrings', ' OR '.join('"' + term + '"' for term in long)))
                diagnostics.ranking('bm25_trigram', [hit for hit, _ in channels[-1]])
            if short:
                channels.append(run(db, 'chunks', ' OR '.join('"' + term + '"*' for term in short)))
                diagnostics.ranking('bm25_prefix', [hit for hit, _ in channels[-1]])
            # Korean partial evidence is a supplementary candidate source, not
            # stemming: keep exact candidates first and require multiple trigrams.
            groups = [{term[index:index + 3] for index in range(len(term) - 2)}
                      for term in terms if re.fullmatch(r'[가-힣]{4,}', term)]
            grams = list(dict.fromkeys(gram for term in terms if re.fullmatch(r'[가-힣]{4,}', term)
                                       for gram in (term[index:index + 3] for index in range(len(term) - 2))))
            if len(grams) > 64:
                grams = [grams[index * (len(grams) - 1) // 63] for index in range(64)]
            if len(grams) >= 2:
                fallback = run(db, 'substrings', ' OR '.join('"' + gram + '"' for gram in grams))
                selected = set(grams)
                fallback = [(hit, text) for hit, text in fallback if any(
                    sum(gram in embedding_text(text) for gram in group & selected) >= 2 for group in groups)]
                diagnostics.ranking('bm25_korean_partial', [hit for hit, _ in fallback])

        # FTS scores from different tokenizers are not comparable. Fuse ranks,
        # then prioritize how much of the query is actually present in each
        # candidate. Length weighting favors longer matched terms over short
        # generic tokens; rank fusion resolves comparable coverage without
        # adding incomparable raw BM25 values. This is a ranking heuristic.
        texts = {hit.chunk_id: text for channel in channels for hit, text in channel}
        if not texts and not fallback:
            diagnostics.ranking('bm25_final', [])
            return []
        ranks = reciprocal_rank_fusion([[hit for hit, _ in channel] for channel in channels], len(texts)) if texts else []
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
        result = sorted(scored, key=lambda hit: (-hit.score, hit.chunk_id))
        seen = {hit.chunk_id for hit in result}
        additions = []
        for hit, text in fallback:
            if hit.chunk_id not in seen:
                additions.append(SearchHit(hit.chunk_id, 0.0))
                texts[hit.chunk_id] = text
                seen.add(hit.chunk_id)
        result.extend(additions)
        result = prioritize_identifiers(result, texts, codes)[:limit]
        diagnostics.ranking('bm25_final', result)
        return result
