"""Measure scoped PDF search, including temporary FTS construction (no timing assertions).

Run: python scripts/benchmark_pdf_page_search.py --sizes 100 1000 10000 --repeats 3
The synthetic workload cannot substitute for measurements using production PDFs.
"""
import argparse
import json
import resource
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from indexer.contracts import SearchRequest
from indexer.published_retrieval import PublishedRetrieval


def benchmark(size, repeats, workers):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / 'v').mkdir()
        (root / 'document.json').write_text(json.dumps({'format': 'pdf', 'filename': 'synthetic.pdf'}))
        chunks = [{'chunk_id': str(i), 'document_id': 'doc', 'job_id': 'job', 'page': 1,
                   'kind': 'text', 'element_ids': [], 'first_order': i,
                   'text': f'PDF 검색 성능 측정 문서 항목 {i} example search evidence'} for i in range(size)]
        (root / 'v' / 'chunks.json').write_text(json.dumps(chunks, ensure_ascii=False))
        publisher = SimpleNamespace(root=root, search_lock=threading.RLock(),
                                    get_version=lambda _: {'version': 'v', 'document_ids': ['doc']},
                                    deleted_documents=lambda: set())
        storage = SimpleNamespace(document_dir=lambda _: root)
        retrieval = PublishedRetrieval(publisher, storage)
        request = SearchRequest(query='검색 evidence', method='bm25', top_k=5)
        def run(_):
            start = time.perf_counter()
            result = retrieval.search_pdf_pages(request, [1])
            assert len(result['results']) == min(5, size)
            return time.perf_counter() - start
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            elapsed = list(executor.map(run, range(repeats)))
        return {'chunks': size, 'requests': repeats, 'workers': workers,
                'wall_seconds': round(time.perf_counter() - start, 3),
                'median_request_seconds': round(statistics.median(elapsed), 3),
                'max_request_seconds': round(max(elapsed), 3),
                'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'peak_rss_increase_kib': max(0, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', type=int, nargs='+', default=[100, 1000, 10000])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()
    if any(size < 1 or size > 10000 for size in args.sizes) or args.repeats < 1 or args.workers < 1:
        parser.error('sizes must be 1..10000; repeats and workers must be positive')
    for size in args.sizes:
        print(json.dumps(benchmark(size, args.repeats, args.workers)))


if __name__ == '__main__':
    main()
