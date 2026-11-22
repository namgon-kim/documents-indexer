"""Conservative hierarchy inference for parsers returning only flat headings.

The parser's heading classification is retained. Inferred ancestry is never used
as proof of a document's semantic structure.
"""
import re
from dataclasses import dataclass

# A bounded Roman grammar prevents words such as LLM or IDC being treated as numerals.
_ROMAN = r'(?:XII|XI|IX|VIII|VII|VI|IV|III|II|I|X|V|Ⅻ|Ⅺ|Ⅹ|Ⅸ|Ⅷ|Ⅶ|Ⅵ|Ⅴ|Ⅳ|Ⅲ|Ⅱ|Ⅰ)'
_ROMAN_HEADING = re.compile(rf'^\s*{_ROMAN}(?:[.)]\s*|\s+)\S')
_CHAPTER_HEADING = re.compile(r'^\s*제\s*\d+\s*[장편부](?:\s|[.)]|$)')
_YEAR_TITLE = re.compile(r'^\s*(?:19|20)\d{2}\s+\S')
_NUMBERED = re.compile(r'^\s*(\d{1,3}(?:\.\d{1,3})*)(?:\.(?=\s|$)|(?=\s|$))')
_HYPHEN_NUMBERED = re.compile(r'^\s*(\d{1,3}(?:-\d{1,3})+)(?:[.)](?=\s|$)|(?=\s|$))')
_SUBITEM = re.compile(r'^\s*(?:(\d+)\)|([가-힣])\.)(?:\s|$)')


@dataclass(frozen=True)
class HeadingMarker:
    kind: str
    key: str | None = None


def heading_marker(text, *, is_first=False, parser_label=None, hyphen_keys=()):
    """Classify only structural markers; unmatched text remains ambiguous."""
    if parser_label == 'doc_title':
        return HeadingMarker('root')
    if _CHAPTER_HEADING.match(text) or _ROMAN_HEADING.match(text):
        return HeadingMarker('root')
    if is_first and _YEAR_TITLE.match(text):
        return HeadingMarker('root')
    if match := _SUBITEM.match(text):
        return HeadingMarker('subitem', 'number' if match.group(1) else 'korean')
    if match := _HYPHEN_NUMBERED.match(text):
        key = match.group(1)
        if key in hyphen_keys:
            return HeadingMarker('number', key.replace('-', '.'))
    if match := _NUMBERED.match(text):
        return HeadingMarker('number', match.group(1))
    return HeadingMarker('ambiguous')


def apply_flat_hierarchy(elements, *, preserve_explicit=False):
    """Infer paths from explicit markers while keeping ambiguous labels local.

    The stack contains (level, text, id, numeric key, marker). A later numbered
    sibling discards ambiguous descendants and resumes from its numbered parent.
    """
    stack = []
    numbers = {match.group(1) for item in elements if item.get('kind') == 'heading'
               if (match := _HYPHEN_NUMBERED.match(item.get('text', '')))}
    hyphen_keys = set()
    for number in numbers:
        parent = number.rpartition('-')[0]
        if number.count('-') >= 2 and (parent in numbers or any(
                other != number and other.rpartition('-')[0] == parent for other in numbers)):
            hyphen_keys.add(number)
            if parent in numbers:
                hyphen_keys.add(parent)
    first_heading = True
    for item in elements:
        if item['kind'] == 'heading':
            marker = heading_marker(item['text'], is_first=first_heading, parser_label=item.get('parser_label'),
                                    hyphen_keys=hyphen_keys)
            first_heading = False
            level = item.get('heading_level', 1)
            number_key = marker.key if marker.kind == 'number' else None
            fixed = preserve_explicit and (level > 1 or item.get('heading_level_explicit'))
            if fixed:
                pass
            elif marker.kind == 'root':
                level = 1
            elif marker.kind == 'subitem' and stack:
                sibling = next((entry for entry in reversed(stack) if entry[4] == marker), None)
                level = sibling[0] if sibling else min(6, stack[-1][0] + 1)
            elif marker.kind == 'number':
                parent_key = number_key.rpartition('.')[0]
                parent = next((entry for entry in reversed(stack) if entry[3] == parent_key), None) if parent_key else None
                if parent:
                    level = min(6, parent[0] + 1)
                elif stack and stack[0][4].kind == 'root':
                    level = 2
            elif marker.kind == 'ambiguous' and stack and (any(entry[3] for entry in stack) or stack[0][4].kind == 'root'):
                level = min(6, stack[-1][0] + 1)
            if level != item.get('heading_level', 1):
                item['heading_level'], item['heading_level_origin'] = level, 'inferred'
            stack = [entry for entry in stack if entry[0] < level]
            if item['text']:
                stack.append((level, item['text'], item['element_id'], number_key, marker))
        item['heading_path'] = [entry[1] for entry in stack]
        item['heading_element_ids'] = [entry[2] for entry in stack]


def promote_chapter_headers(elements):
    """Promote unique top-of-page chapter markers followed by a heading."""
    pages = {}
    header_pages = {}
    for item in elements:
        if page := item.get('page'):
            pages.setdefault(page, []).append(item)
            if item['kind'] in {'header', 'page_header'}:
                header_pages.setdefault(item['text'].strip().casefold(), set()).add(page)
    promoted = False
    for page, items in pages.items():
        for index, item in enumerate(items):
            if item['kind'] not in {'header', 'page_header'}:
                continue
            if len(header_pages.get(item['text'].strip().casefold(), ())) != 1:
                continue
            if heading_marker(item['text']).kind != 'root':
                continue
            coordinates = item.get('coordinates') or []
            if not coordinates or any(not isinstance(point, dict) or not isinstance(point.get('y'), (int, float))
                                      for point in coordinates):
                continue
            if min(point['y'] for point in coordinates) > 0.12 or max(point['y'] for point in coordinates) > 0.2:
                continue
            if any(other['kind'] not in {'header', 'page_header', 'page_number'} for other in items[:index]):
                continue
            following = next((other for other in items[index + 1:]
                              if other['kind'] not in {'header', 'page_header', 'page_number'}), None)
            if not following or following['kind'] != 'heading':
                continue
            item.update(kind='heading', heading_level=1, heading_level_origin='inferred', original_kind=item['kind'])
            promoted = True
    return promoted
