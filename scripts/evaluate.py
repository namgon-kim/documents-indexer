"""Measure retrieval of annotated document pages against a running DocumentIndexer."""

import argparse
import json
import math
import os
import re
import time
from pathlib import Path

import httpx
from indexer.embedding_input import embedding_text


def require_ok(response):
    if response.is_error:
        raise RuntimeError(f"{response.request.url.path}: HTTP {response.status_code}")
    return response.json()


def matches_terms(terms, line):
    line = embedding_text(line)
    for term in terms:
        if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)*", term):
            if not re.search(r"(?<![\d.,+\-])" + re.escape(term) + r"(?![\d.,])", line):
                return False
        elif term not in line:
            return False
    return True


def evidence_matches(expected, result):
    if expected["document_id"] != result["document_id"]:
        return False
    row = expected.get("row")
    column = expected.get("column")
    terms = [embedding_text(term) for term in expected.get("contains_line", [])]
    page_refs = [ref for ref in result["source_refs"] if ref.get("page") == expected["page"]]
    if not page_refs:
        return False
    if row is None and column is None:
        return not terms or any(matches_terms(terms, line) for line in result["search_text"].splitlines())
    return any(
        (row is None or segment["row"] == row)
        and (column is None or segment["column"] == column)
        and matches_terms(terms, segment["text"])
        for segment in result.get("table_segments", [])
    )


def evaluate(client, cases, top_k, method="hybrid", index_version=None, profile_id=None, *,
             trace_mode=None, rerank=False):
    report = []
    versions = set()
    for case in cases:
        expected = list({json.dumps(ref, sort_keys=True): ref for ref in case["evidence"]}.values())
        if not expected:
            raise ValueError("Every case must include at least one annotated document page")
        start = time.perf_counter()
        body = {"query": case["query"], "limit": top_k, "method": method}
        if index_version:
            body["index_version"] = index_version
        if profile_id:
            body["profile_id"] = profile_id
        if rerank:
            body['rerank'] = True
        if trace_mode:
            if trace_mode not in {'search', 'chat'}:
                raise ValueError('Unsupported diagnostic mode')
            request = dict(body)
            request['probes'] = expected
            if trace_mode == 'chat':
                request['message'] = request.pop('query')
                request['top_k'] = request.pop('limit')
            response = require_ok(client.post('/v1/diagnostics/' + ('chat' if trace_mode == 'chat' else 'retrieve'),
                                              json=request))
        else:
            response = require_ok(client.post("/v1/retrieve", json=body))
        elapsed = (time.perf_counter() - start) * 1000
        versions.add(response["index_version"])
        index_version = index_version or response["index_version"]
        if len(versions) > 1:
            raise RuntimeError("The active index changed during the experiment")
        found = set()
        first_rank = None
        if trace_mode:
            trace = response['trace']
            stage = trace['stages'].get('model_evidence' if trace_mode == 'chat' else 'retrieval_returned', [])
            if trace_mode == 'chat':
                stage = sorted((item for item in stage if item.get('citation_id') is not None),
                               key=lambda item: item['citation_id'])
            for rank, item in enumerate(stage, 1):
                overlap = {index for index, probe in enumerate(trace['probes'])
                           if item['chunk_id'] in probe['returned_chunk_ids']}
                if overlap and first_rank is None:
                    first_rank = rank
                found |= overlap
        else:
            for rank, result in enumerate(response["results"], 1):
                overlap = {i for i, ref in enumerate(expected) if evidence_matches(ref, result)}
                if overlap and first_rank is None:
                    first_rank = rank
                found |= overlap
        row = {"id": case["id"], "group": case.get("group", "general"), "hit": bool(first_rank),
               "recall": len(found) / len(expected), "reciprocal_rank": 1 / first_rank if first_rank else 0,
               "latency_ms": round(elapsed, 2)}
        if trace_mode:
            row['diagnostic_trace'] = trace
            if trace_mode == 'chat':
                row['recall_scope'] = 'confirmed_citable_source_lower_bound'
                probes = trace['probes']
                unknown = any(probe.get('model_input_status') != 'captured' or (
                    probe.get('truncated_cell_match_unverified_chunk_ids') and not probe.get('model_input_matches'))
                    for probe in probes)
                if unknown:
                    row['model_input_recall'] = None
                    row['model_input_recall_status'] = 'unknown'
                else:
                    row['model_input_recall'] = sum(bool(probe.get('model_input_matches')) for probe in probes) / len(probes)
                    row['model_input_recall_status'] = 'literal_source_fields_only'
        report.append(row)
    if not report:
        raise ValueError("Provide at least one evaluation case")
    latency = sorted(case["latency_ms"] for case in report)
    return {
        "index_version": next(iter(versions)),
        "top_k": top_k,
        "method": method,
        "profile_id": profile_id,
        "rerank": rerank, "trace_mode": trace_mode,
        "count": len(report),
        "hit_rate": sum(case["hit"] for case in report) / len(report),
        "recall": sum(case["recall"] for case in report) / len(report),
        "mrr": sum(case["reciprocal_rank"] for case in report) / len(report),
        "p95_ms": latency[math.ceil(0.95 * len(latency)) - 1],
        "p50_ms": latency[math.ceil(0.5 * len(latency)) - 1],
        "by_group": {
            group: {
                "count": len(items),
                "hit_rate": sum(item["hit"] for item in items) / len(items),
                "recall": sum(item["recall"] for item in items) / len(items),
                "mrr": sum(item["reciprocal_rank"] for item in items) / len(items),
            }
            for group in sorted({item["group"] for item in report})
            if (items := [item for item in report if item["group"] == group])
        },
        "cases": report,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "cases",
        type=Path,
        help="JSON cases with id, query, group (optional) and evidence [{document_id, page, row?, column?, contains_line?}]",
    )
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument("--top-k", type=int, default=5, choices=range(1, 21))
    parser.add_argument("--method", choices=["bm25", "dense", "hybrid", "all"], default="all")
    parser.add_argument("--index-version", help="검색할 색인 버전 (생략하면 실행 시 현재 버전)")
    parser.add_argument("--profile-id", help="검색할 임베딩 모델 ID (생략하면 서버 기본 모델)")
    parser.add_argument('--rerank', action='store_true', help='평가에 서버 재순위 설정 적용')
    parser.add_argument('--trace', choices=['search', 'chat'], help='관리자 진단 모드; chat은 답변 모델 호출 비용이 발생합니다')
    options = parser.parse_args()
    cases = json.loads(options.cases.read_text(encoding="utf-8"))
    base = options.origin.rstrip("/")
    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=120) as client:
        session = require_ok(
            client.post("/v1/auth/login", json={"access_key": os.getenv("INDEXER_ACCESS_KEY", "")})
        )
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        methods = ["bm25", "dense", "hybrid"] if options.method == "all" else [options.method]
        reports = []
        version = options.index_version
        for method in methods:
            report = evaluate(client, cases, options.top_k, method=method, index_version=version,
                              profile_id=options.profile_id, trace_mode=options.trace, rerank=options.rerank)
            version = report["index_version"]
            reports.append(report)
        result = {"index_version": version, "reports": reports}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
