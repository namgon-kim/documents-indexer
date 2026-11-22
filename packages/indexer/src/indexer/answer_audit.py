"""Check citation identifiers; this does not assess semantic support."""

import re

from indexer.citations import CITATION, parse_citation


def audit_answer(answer, evidence):
    warnings = []
    for raw in re.split(r'\n+|(?<=[.!?。])\s+(?!\[)|(?<=\])\s+(?=[가-힣A-Za-z])', answer):
        claim = raw.strip()
        tokens = [parse_citation(match.group(1)) for match in CITATION.finditer(claim)]
        numbers = [number for token in tokens if token is not None for number in token]
        if any(token is None for token in tokens) or any(number > len(evidence) for number in numbers):
            warnings.append({'code': 'invalid_citation', 'claim': claim[:300],
                             'citations': sorted({number for number in numbers if 1 <= number <= len(evidence)})})
            if len(warnings) > 20:
                return {'status': 'review_suggested', 'warnings': warnings[:20], 'warnings_truncated': True,
                        'method': 'citation_identifiers_only'}
    return {'status': 'review_suggested' if warnings else 'not_verified', 'warnings': warnings,
            'warnings_truncated': False, 'method': 'citation_identifiers_only'}
