"""Check citation identifiers; this does not assess semantic support."""

import re

CITATION = re.compile(r'\[(\d+)\]')


def audit_answer(answer, evidence):
    warnings = []
    for raw in re.split(r'\n+|(?<=[.!?。])\s+(?!\[)|(?<=\])\s+(?=[가-힣A-Za-z])', answer):
        claim = raw.strip()
        numbers = [int(number) for number in CITATION.findall(claim)]
        if any(number < 1 or number > len(evidence) for number in numbers):
            warnings.append({'code': 'invalid_citation', 'claim': claim[:300],
                             'citations': sorted({number for number in numbers if 1 <= number <= len(evidence)})})
            if len(warnings) > 20:
                return {'status': 'review_suggested', 'warnings': warnings[:20], 'warnings_truncated': True,
                        'method': 'citation_identifiers_only'}
    return {'status': 'review_suggested' if warnings else 'not_verified', 'warnings': warnings,
            'warnings_truncated': False, 'method': 'citation_identifiers_only'}
