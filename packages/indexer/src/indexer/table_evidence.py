"""Map normalized HTML table fragments back to the parsed cell text."""

import re

from indexer.embedding_input import byte_size, embedding_text
from indexer.parsing.normalization import cells_from, clean, parse_table_html

HTML_CELL_RANGE = "table_cell"
MAX_CELL_RANGES = 100
CELL_RANGE = re.compile(r"([1-9][0-9]{0,3}):([1-9][0-9]{0,2}):([0-9]{1,9}):([0-9]{1,9})")


def _split_word(raw, chars, max_bytes):
    """Split an oversized token at source character boundaries."""
    cursor = 0
    while cursor < len(raw):
        left, right = 1, min(chars, max_bytes, len(raw) - cursor)
        best = None
        while left <= right:
            size = (left + right) // 2
            piece = embedding_text(raw[cursor:cursor + size])
            if len(piece) <= chars and byte_size(piece) <= max_bytes:
                best = size, piece
                left = size + 1
            else:
                right = size - 1
        if best is None:
            raise ValueError("Insufficient input budget for one table character")
        size, piece = best
        cursor += size
        yield piece, cursor


def _search_fragments(source, chars, max_bytes):
    value, end = "", 0
    for token in re.finditer(r"\S+", source):
        word = embedding_text(token.group())
        candidate = f"{value} {word}" if value else word
        if len(candidate) <= chars and byte_size(candidate) <= max_bytes:
            value, end = candidate, token.end()
            continue
        if value:
            yield value, end
        if len(word) <= chars and byte_size(word) <= max_bytes:
            value, end = word, token.end()
            continue
        for piece, relative_end in _split_word(token.group(), chars, max_bytes):
            value, end = piece, token.start() + relative_end
            if end < token.end():
                yield value, end
    if value:
        yield value, len(source)


def split_cell_source(source, *, chars, max_bytes, offset=0):
    search = embedding_text(clean(source))
    if not search:
        return
    pieces = ([(search, len(source))] if len(search) <= chars and byte_size(search) <= max_bytes
              else _search_fragments(source, chars, max_bytes))
    start = 0
    for value, end in pieces:
        yield {"text": value, "source_text": source[start:end],
               "source_start": offset + start, "source_end": offset + end}
        start = end


def set_html_table_evidence(chunk, group):
    element_id = chunk["element_ids"][0]
    chunk["source_range_quality"] = HTML_CELL_RANGE
    chunk["source_segments"] = [
        {"element_id": element_id, "order": chunk["first_order"],
         "row": part["row"], "column": part["column"],
         "start": part["source_start"], "end": part["source_end"], "text": part["source_text"]}
        for part in group
    ]


def html_table_evidence_urls(base, segments):
    ranges = [f'{item["row"]}:{item["column"]}:{item["start"]}:{item["end"]}' for item in segments]
    return [base + "?table_cells=" + ",".join(ranges[index:index + MAX_CELL_RANGES])
            for index in range(0, len(ranges), MAX_CELL_RANGES)]


def read_html_table_evidence(html, encoded):
    ranges = encoded.split(",")
    if len(ranges) > MAX_CELL_RANGES:
        raise ValueError("too many table cell ranges")
    table = parse_table_html(html).find("table")
    if table is None or table.find("table"):
        raise ValueError("missing table cell geometry")
    cells = {(cell.row + 1, cell.column + 1): cell for cell in cells_from(table)}
    result = []
    for selection in ranges:
        match = CELL_RANGE.fullmatch(selection)
        if not match:
            raise ValueError("invalid table cell range")
        row, column, start, end = map(int, match.groups())
        cell = cells.get((row, column))
        if cell is None or not 0 <= start <= end <= len(cell.text):
            raise ValueError("table cell range outside source")
        result.append({"row": row, "column": column, "start": start, "end": end,
                       "rowspan": cell.rows, "colspan": cell.columns, "text": cell.text[start:end]})
    return result
