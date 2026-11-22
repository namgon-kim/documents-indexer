"""Conservative, local checks of generated claims against their cited evidence.

These checks identify review candidates, not entailment or a completeness proof.
Only text actually sent to the answer model is used as evidence.
"""

import re
from decimal import Decimal, InvalidOperation

CITATION = re.compile(r'\[(\d+)\]')
NUMBER = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
PAIR = re.compile(rf'(?<![\w.])({NUMBER})\s*(?:명|개|건|회|%)?\s*(?:→|->|⇒|에서)\s*({NUMBER})')
QUANTITY = re.compile(rf'(?<![\w.])({NUMBER})\s*(%|퍼센트|명|건|개|원|억원|만원|년|개월|일|회)(?![A-Za-z0-9])')
ABSENCE = re.compile(r'(?:문서|전체|모든\s*(?:시트|문서|표|항목))[^\n.!?]{0,45}(?:없(?:다|습니다|음)|미기재|찾을\s*수\s*없|존재하지\s*않)')


def _number(value):
    try:
        return Decimal(value.replace(',', ''))
    except InvalidOperation:
        return None


def _pairs(text):
    return {(_number(a), _number(b)) for a, b in PAIR.findall(text) if _number(a) != _number(b)}


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
        if refs:
            pairs, cited_pairs = _pairs(body), _pairs(cited)
            if any((b, a) in cited_pairs and (a, b) not in cited_pairs for a, b in pairs):
                checks.append('possible_reversed_direction')
            # A missing literal quantity may be a valid derived value, so it is a review cue only.
            for value, _unit in QUANTITY.findall(body):
                number = _number(value)
                if number is not None and number not in {_number(v) for v in re.findall(NUMBER, cited)}:
                    checks.append('quantity_not_literal_in_citation')
                    break
        for code in checks:
            warnings.append({'code': code, 'claim': claim[:300], 'citations': refs})
            if len(warnings) > 20:
                return {'status': 'review_suggested', 'warnings': warnings[:20], 'warnings_truncated': True,
                        'method': 'local_partial_checks'}
    return {'status': 'review_suggested' if warnings else 'not_verified', 'warnings': warnings,
            'warnings_truncated': False, 'method': 'local_partial_checks'}
