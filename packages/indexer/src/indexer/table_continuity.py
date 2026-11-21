import re

TABLE_NUMBER = re.compile(r"^(?:표|table)\s*\d+(?:[.-]\d+)*", re.IGNORECASE)


def column_headers(cells):
    first_data_row = min((cell.row for cell in cells if not cell.header), default=0)
    return [
        cell
        for cell in cells
        if cell.header
        and cell.search_text
        and (cell.scope in {"col", "colgroup"} or (not cell.scope and cell.row < first_data_row))
    ]


def header_signature(headers):
    return [(cell.column, cell.columns, cell.search_text.casefold()) for cell in headers]


