"""Conservative, local checks of generated claims against their cited evidence.

These checks identify review candidates, not entailment or a completeness proof.
Only text actually sent to the answer model is used as evidence.
"""

import re
from decimal import Decimal, InvalidOperation

CITATION = re.compile(r'\[(\d+)\]')
NUMBER = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
UNIT = r'억원|만원|천원|원|퍼센트|%|명|건|개|개월|년|일|회'
PAIR = re.compile(rf'(?<![\w.])({NUMBER})\s*({UNIT})?\s*(?:→|->|⇒|에서)\s*({NUMBER})\s*({UNIT})?')
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
    result = set()
    for a, unit_a, b, unit_b in PAIR.findall(text):
        unit_a, unit_b = unit_a or unit_b, unit_b or unit_a
        if unit_a in CURRENCY and unit_b in CURRENCY:
            pair = ((_number(a) * CURRENCY[unit_a], '원'), (_number(b) * CURRENCY[unit_b], '원'))
        elif unit_a == unit_b or {unit_a, unit_b} == {'%', '퍼센트'}:
            dimension = '%' if unit_a in {'%', '퍼센트'} else unit_a
            pair = ((_number(a), dimension), (_number(b), dimension))
        else:
            continue
        if pair[0] != pair[1]:
            result.add(pair)
    return result


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


def _entity_quantities(text):
    """Associate a single explicit quantity with each labeled entity in its own segment."""
    entities = list(ENTITY.finditer(text))
    grouped = {}
    for index, entity in enumerate(entities):
        end = entities[index + 1].start() if index + 1 < len(entities) else len(text)
        segment = re.split(r'[\n;。!?]', text[entity.end():end], maxsplit=1)[0]
        quantities = _quantities(segment)
        key = (entity.group(1), entity.group(2).casefold())
        if len(quantities) == 1:
            grouped.setdefault(key, []).append(quantities[0])
    return grouped


def _subject_quantity_conflict(claim, cited):
    claimed = _entity_quantities(claim)
    for evidence in cited:
        data = _entity_quantities(evidence)
        categories = {}
        for category, label in data:
            categories.setdefault(category, set()).add(label)
        for key, values in claimed.items():
            if len(categories.get(key[0], ())) < 2 or len(values) != 1 or len(data.get(key, ())) != 1:
                continue
            if not _equivalent(values[0], data[key][0]):
                return True
    return False


def _table_list_warning(question, sources, context, answer):
    """Review explicit requested columns in a single complete table, not guessed continuations."""
    if not question or not re.search(r'전체|전부|모든|빠짐없이|목록|나열|정리|\b(?:all|every|list)\b', question, re.IGNORECASE):
        return None
    tables = {}
    for index, (source, item) in enumerate(zip(sources, context, strict=True), 1):
        ids = source.get('element_ids', [])
        if (source.get('kind') != 'table' or len(ids) != 1
                or not source.get('document_id') or not source.get('job_id')):
            continue
        key = (source.get('document_id'), source.get('job_id'), ids[0])
        tables.setdefault(key, []).append((index, source, item))
    if len(tables) != 1:
        return None
    rows = next(iter(tables.values()))
    if any(source.get('evidence_truncated') or not source.get('model_element_scopes')
           or not all(scope.get('all_indexed_chunks_in_model') for scope in source['model_element_scopes'])
           for _, source, _ in rows):
        return None
    columns = {}
    for index, source, item in rows:
        for segment in source.get('table_segments', []):
            value = segment.get('value', '')
            if not value or len(value) > 100 or value not in item.get('text', ''):
                continue
            for rel in segment.get('label_relations', []):
                label = rel.get('text', '').strip()
                if (rel.get('kind') == 'column' and rel.get('status') == 'explicit' and len(label) >= 2
                        and label in question):
                    columns.setdefault((segment.get('column'), label), set()).add(value)
    columns = {key: values for key, values in columns.items() if 2 <= len(values) <= 100}
    if len(columns) != 1:
        return None
    values = next(iter(columns.values()))
    if any(value.casefold() not in answer.casefold() for value in values):
        return {'code': 'table_items_not_literal_in_answer', 'claim': question[:300],
                'citations': [index for index, _, _ in rows]}
    return None


def audit_answer(answer, evidence, *, question=None, sources=None):
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
            if _subject_quantity_conflict(body, [evidence[index - 1].get('text', '') for index in refs]):
                checks.append('possible_subject_quantity_mismatch')
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
    if sources is not None and (warning := _table_list_warning(question, sources, evidence, answer)):
        if len(warnings) >= 20:
            return {'status': 'review_suggested', 'warnings': warnings, 'warnings_truncated': True,
                    'method': 'local_partial_checks'}
        warnings.append(warning)
    return {'status': 'review_suggested' if warnings else 'not_verified', 'warnings': warnings,
            'warnings_truncated': False, 'method': 'local_partial_checks'}
