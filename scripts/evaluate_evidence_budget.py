"""Replay exported chunks through current BM25 and chat budget, without remote calls.

This is an offline simulation, not the original hybrid/rerank/model request.
The corpus is a JSON array of exported chunk objects; cases contain query and
expected_chunk_ids. Output contains identifiers and counts, never source text.
"""

import argparse
import json
import tempfile
from pathlib import Path

from indexer.bm25_index import BM25Index
from indexer.chat import _chat_evidence
from indexer.search_engine import MAX_RESULT_LIMIT, MAX_RETRIEVAL_TOP_N


def evaluate(chunks, cases, candidate_limit, result_limit, budget):
    if (not 1 <= result_limit <= min(candidate_limit, MAX_RESULT_LIMIT)
            or not 1 <= candidate_limit <= MAX_RETRIEVAL_TOP_N or budget < 1):
        raise ValueError('Check candidate limit, result limit and budget')
    by_id = {chunk['chunk_id']: chunk for chunk in chunks}
    if len(by_id) != len(chunks):
        raise ValueError('Chunk identifiers must be unique')
    document_ids = {chunk['document_id'] for chunk in chunks}
    if len(document_ids) != 1:
        raise ValueError('Provide chunks from exactly one document')
    with tempfile.TemporaryDirectory() as directory:
        index = BM25Index(Path(directory) / 'bm25.sqlite')
        index.replace_document(next(iter(document_ids)), chunks)
        reports = []
        for case in cases:
            expected = case['expected_chunk_ids']
            if not expected or any(key not in by_id for key in expected):
                raise ValueError('Expected chunk identifiers must exist in the corpus')
            candidates = index.search(case['query'], candidate_limit)
            selected = candidates[:result_limit]
            hits = [{**by_id[hit.chunk_id], 'search_text': by_id[hit.chunk_id]['text'],
                     'filename': 'offline', 'source_refs': [], 'unit_id': hit.chunk_id} for hit in selected]
            sources, _, counts = _chat_evidence(hits, result_limit, budget)
            candidate_ranks = {hit.chunk_id: rank for rank, hit in enumerate(candidates, 1)}
            sent = {source['unit_id']: source for source in sources}
            findings = []
            for key in expected:
                rank = candidate_ranks.get(key)
                source = sent.get(key)
                state = ('not_retrieved' if rank is None else 'not_selected' if rank > result_limit else
                         'omitted' if source is None else 'truncated' if source.get('evidence_truncated') else 'included')
                findings.append({'chunk_id': key, 'bm25_rank': rank, 'input_state': state})
            reports.append({'id': case['id'], 'expected': findings, 'counts': counts})
    return {'method': 'offline_bm25_simulation',
            'limitations': ['Rebuilt BM25; not the historical index or hybrid/reranker output',
                            'Placeholder filename and empty source references; actual payload size may differ',
                            'No table/image expansion, context, model call, or answer validation',
                            'Included chunk does not prove the answer is present or correct'],
            'candidate_limit': candidate_limit,
            'result_limit': result_limit, 'budget_bytes': budget, 'cases': reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('chunks', type=Path, help='JSON array of exported chunks')
    parser.add_argument('cases', type=Path, help='JSON array with id, query and expected_chunk_ids')
    parser.add_argument('--candidate-limit', type=int, default=25)
    parser.add_argument('--result-limit', type=int, default=5)
    parser.add_argument('--budget', type=int, default=32768)
    args = parser.parse_args()
    result = evaluate(json.loads(args.chunks.read_text(encoding='utf-8')),
                      json.loads(args.cases.read_text(encoding='utf-8')),
                      args.candidate_limit, args.result_limit, args.budget)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
