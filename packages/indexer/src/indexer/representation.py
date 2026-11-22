from bisect import bisect_right
from collections import defaultdict
from itertools import groupby

from indexer.embedding_input import MAX_INPUT_BYTES, byte_size, context_prefix, render_embedding, split_text
from indexer.table_continuity import column_headers
from indexer.table_evidence import split_cell_source


def inferred_column_headers(cells, infer_first_row=False):
    """Infer local column labels only inside a structurally delimited row group."""
    result = {}
    # A plain first row may be a heading, but is kept as data and marked inferred.
    if infer_first_row and cells and not any(cell.header for cell in cells):
        first = min(cell.row for cell in cells)
        candidates = [cell for cell in cells if cell.row == first and cell.rows == cell.columns == 1 and cell.search_text and len(cell.search_text) <= 30]
        following = [cell for cell in cells if cell.row > first and cell.search_text]
        if (len(candidates) >= 2 and len({cell.search_text for cell in candidates}) == len(candidates)
                and len({cell.row for cell in following}) >= 1
                and all(any(c.column == h.column and c.columns == 1 for c in following) for h in candidates)):
            for cell in following:
                match = next((h for h in candidates if h.column == cell.column and cell.columns == 1), None)
                if match:
                    result[(cell.row, cell.column)] = match
    for parent in cells:
        if parent.columns != 1 or parent.rows < 2 or not parent.search_text:
            continue
        header_row = parent.row
        candidates = [c for c in cells if c.row == header_row and c.column > parent.column
                      and c.rows == 1 and c.columns == 1 and c.search_text and len(c.search_text) <= 60]
        if len(candidates) < 2 or len({c.search_text for c in candidates}) != len(candidates):
            continue
        for cell in cells:
            if not (header_row < cell.row < parent.row + parent.rows and cell.column > parent.column):
                continue
            matching = [h for h in candidates if h.column == cell.column and h.columns == cell.columns]
            if len(matching) == 1:
                result[(cell.row, cell.column)] = matching[0]
    return result


def table_parts(cells, inherited_headers=(), *, infer_first_row=False):
    rows = sorted({cell.row for cell in cells if not cell.header})
    headers = [cell for cell in cells if cell.header and cell.search_text]
    local_columns = column_headers(cells)
    inferred_columns = inferred_column_headers(cells, infer_first_row)
    if not rows:
        return [], False
    first_data_row = rows[0]
    # A single complete unmerged header row has an unambiguous column mapping
    # even when the author omitted HTML's optional scope attribute.
    simple_header = (first_data_row == 1 and bool(headers)
                     and all(h.row == 0 and h.rows == h.columns == 1
                             and h.scope in {'', 'col'} for h in headers)
                     and len({h.column for h in headers}) == len(headers)
                     and {h.column for h in headers} == {c.column for c in cells if not c.header}
                     and all(c.rows == c.columns == 1 for c in cells))
    # A complete short row between longer data rows can introduce a new set of
    # local headings (a common HTML rendering of a diagram). Keep it as data;
    # the relationship to the following row is only a candidate.
    by_row = defaultdict(list)
    for item in cells:
        if item.search_text:
            by_row[item.row].append(item)
    transitions = {}
    for row in rows[1:]:
        previous, current, following = by_row.get(row - 1, []), by_row[row], by_row.get(row + 1, [])
        if (any(c.columns > 1 for c in local_columns)
                and not any(c.scope in {'col', 'colgroup'} and c.columns == 1 for c in local_columns)
                and len(current) >= 2 and len(current) == len(previous) == len(following)
                and all(not c.header and c.rows == c.columns == 1 and len(c.search_text) <= 30 for c in current)
                and {c.column for c in current} == {c.column for c in previous} == {c.column for c in following}
                and any(len(c.search_text) > 30 for c in previous)
                and any(len(c.search_text) > 30 for c in following)):
            transitions[row] = {c.column: c for c in current}
    transition_rows = sorted(transitions)
    row_values = {
        row: cell
        for cell in cells
        if cell.column == 0 and not cell.header and cell.search_text
        for row in range(cell.row, cell.row + cell.rows)
    }
    output = []
    uncertain = False
    for cell in (value for value in cells if value.search_text and (not value.header or (
        value.row >= first_data_row and value.scope not in {"row", "rowgroup", "col", "colgroup"}
        and value.column > 0
    ))):
        row_headers = [
            item
            for item in headers
            if item.row <= cell.row < item.row + item.rows
            and (
                item.scope in {"row", "rowgroup"}
                or (
                    not item.scope
                    and item.row >= first_data_row
                    and item.column == 0
                    and item.column < cell.column
                )
            )
        ]
        columns = [
            item
            for item in local_columns
            if item.row < cell.row
            and item.column <= cell.column
            and cell.column + cell.columns <= item.column + item.columns
        ]
        if not columns:
            columns = [
                item
                for item in inherited_headers
                if item.column <= cell.column and cell.column + cell.columns <= item.column + item.columns
            ]
        inferred_column = inferred_columns.get((cell.row, cell.column)) if not columns else None
        if inferred_column:
            columns = [inferred_column]
            uncertain = True
        index = bisect_right(transition_rows, cell.row)
        transition = transition_rows[index - 1] if index else None
        if transition is not None:
            columns = [c for c in columns if c.columns > 1]
            if cell.row > transition and cell.column in transitions[transition]:
                columns.append(transitions[transition][cell.column])
            uncertain = True
        if (not row_headers and cell.column > 0 and cell.row in row_values
                and transition is None and (len(row_values[cell.row].search_text) <= 60
                                            or not any(c.columns > 1 for c in local_columns))):
            # Keep the literal first value from this source row alongside later values.
            row_headers = [row_values[cell.row]]
            uncertain = True
        if not columns:
            uncertain = True
        relation_items = []
        for kind, items in (("row", row_headers), ("column", columns)):
            for source in items:
                inherited = any(source is item for item in inherited_headers)
                inferred = (not source.header or inherited or (
                    source.scope not in {"row", "rowgroup", "col", "colgroup"}
                    and not (kind == 'column' and simple_header and source in local_columns)))
                relation_items.append({"kind": kind, "text": source.search_text, "row": source.row + 1,
                                       "column": source.column + 1, "inherited": inherited,
                                       "origin": "inferred" if inferred else "parser",
                                       "status": "unverified" if inferred else "explicit"})
        if any(item['status'] == 'unverified' for item in relation_items):
            uncertain = True
        output.append(
            {
                "label": " / ".join(dict.fromkeys(item["text"] for item in relation_items)),
                "text": cell.search_text,
                "row": cell.row + 1,
                "column": cell.column + 1,
                "label_origin": "inferred" if any(item['origin'] == 'inferred' for item in relation_items) else "parser",
                "label_relations": relation_items,
            }
        )
    return output, uncertain


def full_label(part):
    """A cell label with its row group context, as used before chunk boundaries are known."""
    if 'row_context' not in part:
        return part.get('label', '')
    return ' / '.join(filter(None, [part['row_context'], part.get('column_label', '')]))


def compose_row_labels(parts, max_bytes=MAX_INPUT_BYTES):
    """Write a row's group context once per chunk; every cell keeps its group relation."""
    seen, result = set(), []
    for part in parts:
        if 'row_context' in part:
            key = part['row'], part['row_context']
            label = full_label(part) if key not in seen else part.get('column_label', '')
            seen.add(key)
            part = {**part, 'label': context_prefix(label, min(800, max_bytes // 3))}
        result.append(part)
    return result


def table_groups(context, parts, target, max_bytes=MAX_INPUT_BYTES):
    def fits(group):
        text = render_embedding(context, group)
        return len(text) <= target and byte_size(text) <= max_bytes

    pending = []
    for _, row in groupby(parts, key=lambda part: part["row"]):
        fragments = []
        for part in row:
            item = {**part, "label": context_prefix(part.get("label", ""), min(800, max_bytes // 3))}
            prefix = len(context) + len(item["label"]) + 5
            budget = max_bytes - byte_size(context) - byte_size(item["label"]) - 5
            chars = max(96, target - prefix)
            if "source_text" in item:
                pieces = split_cell_source(item["source_text"], chars=chars, max_bytes=budget,
                                           offset=item.get("source_start", 0))
            else:
                pieces = ({"text": piece} for piece in split_text(item["text"], chars=chars, max_bytes=budget))
            fragments.extend({**item, **piece} for piece in pieces)
        if pending and not fits([*pending, *fragments]):
            yield pending
            pending = []
        for fragment in fragments:
            if pending and not fits([*pending, fragment]):
                yield pending
                pending = []
            pending.append(fragment)
    if pending:
        yield pending
