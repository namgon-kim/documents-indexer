import pytest

from scripts.evaluate_evidence_budget import evaluate


def test_offline_evaluation_reports_selection_and_budget_without_source_text():
    chunks = [{'chunk_id': 'first', 'document_id': 'd', 'text': '희소검색어 ' + '가' * 3000},
              {'chunk_id': 'target', 'document_id': 'd', 'text': '희소검색어 target'}]
    cases = [{'id': 'case', 'query': '희소검색어', 'expected_chunk_ids': ['first', 'target']}]
    report = evaluate(chunks, cases, candidate_limit=2, result_limit=1, budget=4096)
    findings = report['cases'][0]['expected']
    assert {item['input_state'] for item in findings} == {'truncated', 'not_selected'}
    assert '희소검색어' not in str(report)
    assert report['cases'][0]['counts']['evidence_truncated_count'] == 1


def test_offline_evaluation_rejects_ambiguous_corpora_and_unknown_targets():
    chunks = [{'chunk_id': 'a', 'document_id': 'd', 'text': '자료'}]
    with pytest.raises(ValueError, match='Expected chunk'):
        evaluate(chunks, [{'id': 'case', 'query': '자료', 'expected_chunk_ids': ['b']}], 5, 5, 4096)
    with pytest.raises(ValueError, match='exactly one document'):
        evaluate([*chunks, {'chunk_id': 'b', 'document_id': 'other', 'text': '자료'}], [], 5, 5, 4096)
