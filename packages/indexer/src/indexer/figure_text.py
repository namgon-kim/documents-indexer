"""Combine alternate figure renderings without dropping unique source content."""

import re
from html import unescape

from indexer.embedding_input import embedding_text
from indexer.parsing.normalization import readable

# This is the parser's synthetic marker, not a reference to an extracted asset.
PLACEHOLDER = re.compile(r'!\[[^\]]*\]\(/image/placeholder(?:\?[^)]*)?\)', re.IGNORECASE)
LIST_MARKER = re.compile(r'(?m)^[ \t]*[-*+][ \t]+')


def _provider_text(value):
    text = PLACEHOLDER.sub('', unescape(value or ''))
    return embedding_text(text)


def _comparison_text(value):
    # Bullet glyphs are presentation; numeric markers may convey order or year.
    value = LIST_MARKER.sub('', value)
    value = re.sub(r'(?<!\w)\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\w)', r'\1', value)
    return ' '.join(embedding_text(value).split())


def _sentences(line):
    start = 0
    for boundary in re.finditer(r'(?<=[.!?。！？])(?=\s)', line):
        # A number followed by a dot may be an ordered item or a year, not
        # the end of a sentence. Keep it attached to the following text.
        if re.search(r'(?<!\w)\d+\.$', line[start:boundary.start()]):
            continue
        yield line[start:boundary.start()]
        start = boundary.start()
    yield line[start:]


def figure_search_text(soup, provider):
    html = embedding_text(PLACEHOLDER.sub('', readable(soup)))
    provider = _provider_text(provider)
    # Compare complete provider lines with the HTML text. Keeping unmatched
    # lines intact prevents removing the subject of a changed value or claim.
    normalized_html = ' ' + _comparison_text(html) + ' '
    extra_lines = []
    for line in provider.splitlines():
        for sentence in _sentences(line):
            sentence = sentence.strip()
            if sentence and ' ' + _comparison_text(sentence) + ' ' not in normalized_html:
                extra_lines.append(sentence)
    extra = '\n'.join(extra_lines)
    alternatives = [str(node.get('alt', '')).strip() for node in soup.find_all('img')]
    result = '\n'.join(part for part in (html, extra) if part)
    for alt in alternatives:
        if alt and ' ' + _comparison_text(alt) + ' ' not in ' ' + _comparison_text(result) + ' ':
            result = '\n'.join(filter(None, (result, alt)))
    return result
