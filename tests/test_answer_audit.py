from indexer.answer_audit import audit_answer


def test_cited_reversed_numeric_direction_is_flagged_but_matching_is_not():
    evidence = [{'text': '참여자는 5명에서 7명으로 증가했습니다.'}]
    found = audit_answer('참여자는 7명에서 5명으로 감소했습니다. [1]', evidence)
    assert [w['code'] for w in found['warnings']] == ['possible_reversed_direction']
    assert found['warnings'][0]['citations'] == [1]
    assert audit_answer('참여자는 5명에서 7명으로 증가했습니다. [1]', evidence)['warnings'] == []


def test_direction_check_uses_only_cited_evidence_and_is_uncertain():
    evidence = [{'text': 'A: 2→4'}, {'text': 'B: 4→2'}]
    assert audit_answer('A는 4→2입니다. [1]', evidence)['warnings'][0]['code'] == 'possible_reversed_direction'
    assert not audit_answer('B는 4→2입니다. [2]', evidence)['warnings']
    assert not audit_answer('합하면 6입니다. [1] [2]', evidence)['warnings']
    assert audit_answer('A는 4→2입니다.', evidence)['warnings'][0]['code'] == 'uncited_quantitative_claim'


def test_missing_quantity_and_document_absence_are_review_not_refusal():
    evidence = [{'text': '첫 분기 12명, 둘째 분기 18명'}]
    result = audit_answer('합계는 30명입니다. [1]\n문서 전체에는 다른 항목이 없습니다.', evidence)
    assert [item['code'] for item in result['warnings']] == [
        'quantity_not_literal_in_citation', 'document_absence_scope_unverified']
    assert result['status'] == 'review_suggested'
    assert not audit_answer('확인한 셀은 비어 있습니다. [1]', evidence)['warnings']
    assert not audit_answer('전체 기간의 값은 확인된 근거에서 알 수 없습니다.', evidence)['warnings']


def test_citations_and_number_formats_are_handled_without_false_confirmation():
    evidence = [{'text': '예산 1,500원 및 비율 2.5%'}]
    assert audit_answer('예산은 1500원입니다. [1]\n비율은 2.5%입니다. [1]', evidence)['warnings'] == []
    assert audit_answer('값은 10명입니다. [3]', evidence)['warnings'][0]['code'] == 'invalid_citation'


def test_multiple_sentences_preserve_their_own_citations():
    evidence = [{'text': 'A 1→2'}, {'text': 'B 9→8'}]
    result = audit_answer('A는 2→1입니다. [1] B는 9→8입니다. [2]', evidence)
    assert len(result['warnings']) == 1
    assert result['warnings'][0]['citations'] == [1]


def _codes(answer, text):
    return {item['code'] for item in audit_answer(answer, [{'text': text}])['warnings']}


def test_currency_units_and_conversion_are_compared():
    assert 'quantity_unit_mismatch' in _codes('예산은 10억원입니다. [1]', '예산은 10만원입니다.')
    assert not _codes('예산은 1억원입니다. [1]', '예산은 10000만원입니다.')
    assert not _codes('예산은 10000원입니다. [1]', '예산은 1만원입니다.')
    assert 'quantity_not_literal_in_citation' in _codes('인원은 3명입니다. [1]', '인원은 2명입니다.')


def test_same_pair_opposite_description_and_ambiguous_negation():
    assert 'possible_direction_description_conflict' in _codes('인원 5명→7명 감소했습니다. [1]', '인원 5명→7명 증가했습니다.')
    assert not _codes('인원 5명→7명 증가했습니다. [1]', '인원 5명→7명 증가했습니다.')
    assert 'possible_direction_description_conflict' not in _codes('인원 5명→7명 감소했습니다. [1]', '인원 5명→7명 증가하지 않았습니다.')


def test_explicit_labeled_subject_mismatch_is_only_a_review_cue():
    assert 'possible_subject_mismatch' in _codes('시스템 B 사용자는 7명입니다. [1]', '시스템 A 사용자는 7명입니다.')
    assert not _codes('시스템 A 사용자는 7명입니다. [1]', '시스템 A 사용자는 7명입니다.')
    assert 'possible_subject_mismatch' not in _codes('시스템 B 사용자는 7명입니다. [1]', '시스템 A와 시스템 B 사용자는 7명입니다.')


def test_uncited_and_invalid_citations_are_distinct():
    assert 'uncited_quantitative_claim' in _codes('사용자는 7명입니다.', '사용자는 7명입니다.')
    result = audit_answer('사용자는 7명입니다. [1] [3]', [{'text': '사용자는 7명입니다.'}])
    assert 'invalid_citation' in {item['code'] for item in result['warnings']}
    assert 'uncited_quantitative_claim' not in {item['code'] for item in result['warnings']}


def test_absence_wording_and_explicit_list_coverage():
    assert 'document_absence_scope_unverified' in _codes('문서에는 해당 내용이 기재되어 있지 않습니다. [1]', '일부 근거')
    assert 'document_absence_scope_unverified' not in _codes('확인한 셀은 비어 있습니다. [1]', '해당 셀은 빈 값')
    items = '- 알파 업무\n- 베타 업무\n- 감마 업무'
    assert 'list_items_not_literal_in_answer' in _codes('전체 항목은 알파 업무, 베타 업무입니다. [1]', items)
    assert 'list_items_not_literal_in_answer' not in _codes('전체 항목은 알파 업무, 베타 업무, 감마 업무입니다. [1]', items)
    assert 'list_items_not_literal_in_answer' not in _codes('주요 항목은 알파 업무입니다. [1]', items)


def test_uncited_named_factual_claim_is_a_review_signal():
    evidence = [{'text': '시스템 A가 해당 기능을 제공합니다.'}]
    result = audit_answer('시스템 B가 해당 기능을 제공합니다.', evidence)
    assert result['warnings'][0]['code'] == 'uncited_named_claim'
    assert not audit_answer('더 자세히 확인해 주세요.', evidence)['warnings']


def test_full_list_claim_can_flag_missing_items_without_calling_paraphrases_wrong():
    items = [f'항목 {letter}' for letter in 'ABCDEF']
    evidence = '\n'.join(f'- {item}' for item in items)
    partial = '전체 항목은 ' + ', '.join(items[:3]) + '입니다. [1]'
    complete = '전체 항목은 ' + ', '.join(items) + '입니다. [1]'
    assert 'list_items_not_literal_in_answer' in _codes(partial, evidence)
    assert not _codes(complete, evidence)
