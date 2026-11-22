"""Parse and normalize citation IDs without interpreting their meaning."""
import re

# Require a digit before interpreting brackets as a citation. Textual and
# Markdown checkbox brackets remain text; malformed numeric lists are rejected.
CITATION = re.compile(r'\[((?=[\d,\-\s]*\d)[\d,\-\s]+)\]')
MAX_CITATIONS = 100


def parse_citation(value):
    numbers = []
    for part in value.split(','):
        match = re.fullmatch(r'\s*(\d{1,6})(?:\s*-\s*(\d{1,6}))?\s*', part)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        if start < 1 or end < start or end - start + len(numbers) + 1 > MAX_CITATIONS:
            return None
        numbers.extend(range(start, end + 1))
    return list(dict.fromkeys(numbers))


def citation_numbers(text):
    return [number for match in CITATION.finditer(text) if (numbers := parse_citation(match.group(1))) is not None
            for number in numbers]


def render_citations(text, limit):
    def render(match):
        numbers = parse_citation(match.group(1))
        if numbers is None:
            return '[근거 확인 불가]'
        return ''.join(f'[{number}]' if number <= limit else '[근거 확인 불가]' for number in numbers)
    return CITATION.sub(render, text)


def sanitize_history_citations(text, known):
    """Remove known historical citations and retain other bracketed numbers as text."""
    def replace(match):
        numbers = parse_citation(match.group(1))
        if numbers is None:
            return '［' + match.group(1) + '］'
        return ''.join(f'［{number}］' for number in numbers if number not in known)
    return CITATION.sub(replace, text)
