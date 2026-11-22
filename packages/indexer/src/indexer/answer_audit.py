"""Conservative, local checks of generated claims against their cited evidence.

These checks identify review candidates, not entailment or a completeness proof.
Only text actually sent to the answer model is used as evidence.
"""

import re
from decimal import Decimal, InvalidOperation

CITATION = re.compile(r'\[(\d+)\]')
NUMBER = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
PAIR = re.compile(rf'(?<![\w.])({NUMBER})\s*(?:명|개|건|회|%)?\s*(?:→|->|⇒|에서)\s*({NUMBER})')
QUANTITY = re.compile(rf'(?<![\w.])({NUMBER})\s*(억원|만원|천원|원|퍼센트|%|명|건|개|개월|년|일|회)(?![A-Za-z0-9])')
ABSENCE = re.compile(r'(?:문서|전체|모든\s*(?:시트|문서|표|항목))[^\n.!?]{0,65}(?:없(?:다|습니다|음)|미기재|찾을\s*수\s*없|존재하지\s*않|기재(?:되어|돼)?\s*있지\s*않)')
ENTITY = re.compile(r'([가-힣]{2,})\s+([A-Za-z][0-9A-Za-z_-]{0,15})(?![0-9A-Za-z_-])')
DIRECTION = re.compile(r'증가|상승|늘어|늘었|늘어난|확대|감소|하락|줄어|줄었|줄어든|축소')
LIST_ITEM = re.compile(r'(?m)^\s*(?:[-*•]|\d+[.)])\s+(.{2,100}?)\s*$')
EXHAUSTIVE = re.compile(r'전체|전부|모든|빠짐없이|\b(?:all|every)\b', re.IGNORECASE)
CURRENCY = {'원': 1, '천원': 1000, '만원': 10000, '억원': 100000000}



def _number(value):
    try:
        return Decimal(value.replace(',', ''))
    except InvalidOperation:
        return None


def _pairs(text):
    return {(_number(a), _number(b)) for a, b in PAIR.findall(text) if _number(a) != _number(b)}


def _quantities(text):
    return [(_number(value), unit) for value, unit in QUANTITY.findall(text)]


def _equivalent(left, right):
    a, ua = left
    b, ub = right
    if ua in CURRENCY and ub in CURRENCY:
        return a * CURRENCY[ua] == b * CURRENCY[ub]
    return a == b and (ua == ub or {ua, ub} == {'%', '퍼센트'})


def _direction(text):
    # Negations and mixed-direction descriptions are ambiguous; do not infer a polarity.
    if re.search(r'(?:증가|상승|늘|확대|감소|하락|줄|축소)[^.!?]{0,8}(?:않|없)', text):
        return None
    signs = {1 if word in {'증가', '상승', '늘어', '늘었', '늘어난', '확대'} else -1
             for word in DIRECTION.findall(text)}
    return next(iter(signs)) if len(signs) == 1 else None


def _entities(text):
    result = {}
    for category, label in ENTITY.findall(text):
        result.setdefault(category, set()).add(label.casefold())
    return result


def audit_answer(answer, evidence):
    """Return bounded review signals; neither absence of signals nor citations mean verified."""
    warnings = []
    # Treat citation after a period as belonging to the preceding sentence.
    parts = re.split(r'\n+|(?<=[.!?。])\s+(?!\[)|(?<=\])\s+(?=[가-힣A-Za-z])', answer)
    for raw in parts:
        claim = raw.strip()
        if not claim:
            continue
        refs = sorted({int(n) for n in CITATION.findall(claim) if 1 <= int(n) <= len(evidence)})
        body = CITATION.sub('', claim)
        cited = '\n'.join(evidence[index - 1].get('text', '') for index in refs)
        checks = []
        if ABSENCE.search(body) and not re.search(r'알\s*수\s*없|확인(?:할\s*수\s*없|되지\s*않)', body):
            checks.append('document_absence_scope_unverified')
        if any(not 1 <= int(number) <= len(evidence) for number in CITATION.findall(claim)):
            checks.append('invalid_citation')
        if not refs and (QUANTITY.search(body) or PAIR.search(body)):
            checks.append('uncited_quantitative_claim')
        elif not refs and ENTITY.search(body) and re.search(r'(?:이다|입니다|한다|합니다|있다|있습니다)', body):
            checks.append('uncited_named_claim')
        if refs:
            pairs, cited_pairs = _pairs(body), _pairs(cited)
            if any((b, a) in cited_pairs and (a, b) not in cited_pairs for a, b in pairs):
                checks.append('possible_reversed_direction')
            claim_direction = _direction(body)
            cited_direction = _direction(cited)
            if (pairs & cited_pairs and claim_direction and cited_direction
                    and claim_direction != cited_direction):
                checks.append('possible_direction_description_conflict')
            claimed_entities, cited_entities = _entities(body), _entities(cited)
            if any(labels.isdisjoint(cited_entities[category]) for category, labels in claimed_entities.items()
                   if category in cited_entities and len(labels) == len(cited_entities[category]) == 1):
                checks.append('possible_subject_mismatch')
            cited_quantities = _quantities(cited)
            for quantity in _quantities(body):
                if any(_equivalent(quantity, other) for other in cited_quantities):
                    continue
                if any(quantity[0] == other[0] and quantity[1] != other[1] for other in cited_quantities):
                    checks.append('quantity_unit_mismatch')
                elif quantity[0] not in {_number(v) for v in re.findall(NUMBER, cited)}:
                    checks.append('quantity_not_literal_in_citation')
                break
            if EXHAUSTIVE.search(body):
                listed = [item.strip() for item in LIST_ITEM.findall(cited)]
                if len(listed) >= 2 and any(item.casefold() not in answer.casefold() for item in listed):
                    checks.append('list_items_not_literal_in_answer')
        for code in checks:
            warnings.append({'code': code, 'claim': claim[:300], 'citations': refs})
            if len(warnings) > 20:
                return {'status': 'review_suggested', 'warnings': warnings[:20], 'warnings_truncated': True,
                        'method': 'local_partial_checks'}
    return {'status': 'review_suggested' if warnings else 'not_verified', 'warnings': warnings,
            'warnings_truncated': False, 'method': 'local_partial_checks'}
