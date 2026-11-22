from indexer.answer_audit import audit_answer


def test_citation_identifiers_are_checked_without_claiming_semantic_verification():
    evidence = [{'text': 'A 1, B 2'}]
    result = audit_answer('A는 2입니다. [1]', evidence)
    assert result['status'] == 'not_verified'
    assert result['warnings'] == []
    assert result['method'] == 'citation_identifiers_only'


def test_invalid_citation_is_reported_with_available_references():
    result = audit_answer('주장 [1] [5]', [{'text': '근거'}])
    assert result['status'] == 'review_suggested'
    assert result['warnings'] == [{'code': 'invalid_citation', 'claim': '주장 [1] [5]', 'citations': [1]}]


def test_only_more_than_limit_marks_warnings_truncated():
    evidence = [{'text': '근거'}]
    exact = audit_answer('\n'.join(f'문장 {i} [2]' for i in range(20)), evidence)
    extra = audit_answer('\n'.join(f'문장 {i} [2]' for i in range(21)), evidence)
    assert len(exact['warnings']) == 20 and not exact['warnings_truncated']
    assert len(extra['warnings']) == 20 and extra['warnings_truncated']
