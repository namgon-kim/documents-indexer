from itertools import groupby

from indexer.embedding_input import MAX_INPUT_BYTES, byte_size, context_prefix, render_embedding, split_text
from indexer.table_continuity import column_headers


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
                    result[(cell.row, cell.column)] = match.search_text
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
                result[(cell.row, cell.column)] = matching[0].text
    return result


def table_parts(cells, inherited_headers=(), *, infer_first_row=False):
    rows = sorted({cell.row for cell in cells if not cell.header})
    headers = [cell for cell in cells if cell.header and cell.search_text]
    local_columns = column_headers(cells)
    inferred_columns = inferred_column_headers(cells, infer_first_row)
    if not rows:
        return [], False
    first_data_row = rows[0]
    row_values = {
        row: cell.search_text
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
            item.search_text
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
            item.search_text
            for item in local_columns
            if item.row < cell.row
            and item.column <= cell.column
            and cell.column + cell.columns <= item.column + item.columns
        ]
        if not columns:
            columns = [
                item.search_text
                for item in inherited_headers
                if item.column <= cell.column and cell.column + cell.columns <= item.column + item.columns
            ]
        inferred_column = inferred_columns.get((cell.row, cell.column)) if not columns else None
        if inferred_column:
            columns = [inferred_column]
            uncertain = True
        if not row_headers and cell.column > 0 and cell.row in row_values:
            # Keep the literal first value from this source row alongside later values.
            row_headers = [row_values[cell.row]]
        if not columns:
            uncertain = True
        output.append(
            {
                "label": " / ".join(dict.fromkeys(row_headers + columns)),
                "text": cell.search_text,
                "row": cell.row + 1,
                "column": cell.column + 1,
                "label_origin": "inferred" if inferred_column else "parser",
            }
        )
    return output, uncertain


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
            fragments.extend(
                {**item, "text": piece}
                for piece in split_text(item["text"], chars=max(96, target - prefix), max_bytes=budget)
            )
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
