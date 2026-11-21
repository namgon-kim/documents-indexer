"""Detect observable text loss without claiming semantic equivalence."""

import re
from collections import Counter, defaultdict
from unicodedata import normalize

from bs4 import BeautifulSoup
from openpyxl.utils.cell import coordinate_to_tuple

from indexer.parsing.normalization import cells_from


def normalized(value):
    return " ".join(normalize("NFKC", value).casefold().split())


def inventory(elements):
    words, cells, rows = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    xlsx_rows = defaultdict(dict)
    for element in elements:
        page = element.get("page")
        table = BeautifulSoup(element.get("html") or "", "html.parser").find("table")
        table_cells = cells_from(table) if table and not table.find("table") else []
        location = element.get('office_location') or {}
        if location.get('kind') == 'xlsx_range' and (table_cells or element.get('xlsx_region')):
            region = element.get('xlsx_region')
            if region:
                parts = [(cell['row'], cell['column'], cell['text']) for cell in region['cells']]
            else:
                top, left = coordinate_to_tuple(location['start'])
                parts = [(cell.row + top, cell.column + left, cell.text) for cell in table_cells]
            for row, column, text in parts:
                if value := normalized(text):
                    key = location['sheet'], row, column
                    cells[page][(*key, value)] += 1
                    xlsx_rows[page, location['sheet'], row][column] = value
            continue
        if table_cells:
            caption = table.find("caption")
            if caption:
                words[page].update(re.findall(r"[^\W_]+", normalized(caption.get_text(" ", strip=True)), re.UNICODE))
            cells[page].update(normalized(cell.text) for cell in table_cells if normalized(cell.text))
            grid = defaultdict(dict)
            for cell in table_cells:
                for row in range(cell.row, cell.row + cell.rows):
                    for column in range(cell.column, cell.column + cell.columns):
                        grid[row][column] = normalized(cell.text)
            for columns in grid.values():
                signature = tuple(columns.get(i, "") for i in range(max(columns) + 1))
                if any(signature):
                    rows[page][signature] += 1
        else:
            words[page].update(re.findall(r"[^\W_]+", normalized(element.get("text") or ""), re.UNICODE))
    for (page, sheet, row), values in xlsx_rows.items():
        rows[page][(sheet, row, tuple(sorted(values.items())))] += 1
    return words, cells, rows


def compare(previous, current):
    old_words, old_cells, old_rows = inventory(previous)
    new_words, new_cells, new_rows = inventory(current)
    changes = []
    for page in sorted(old_words.keys() | old_cells.keys(), key=lambda value: (value is None, value or 0)):
        missing_words = sum((old_words[page] - new_words[page]).values())
        missing_cells = sum((old_cells[page] - new_cells[page]).values())
        missing_rows = sum((old_rows[page] - new_rows[page]).values())
        if missing_words or missing_cells or missing_rows:
            changes.append({"page": page, "missing_word_count": missing_words, "missing_cell_count": missing_cells, "changed_table_row_count": missing_rows})
    return changes
