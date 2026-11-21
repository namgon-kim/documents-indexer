"""Small, review-oriented projections of native XLSX artifacts."""

from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import TokenizerError
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_to_tuple

from indexer.xlsx_structure import confirmed, occupied, verification


def region_summary(region):
    return {**region, 'verification': verification(region),
            'status': 'verified' if confirmed(region) else 'unverified'}


def sheet_summary(sheet):
    return {key: sheet[key] for key in ('name', 'bounds', 'visibility')}


def sheet_detail(sheet):
    detail = {key: sheet.get(key) for key in ('excluded', 'boundary_candidates', 'analysis_status', 'merged_ranges')}
    detail['regions'] = [region_summary(item) for item in sheet['regions']]
    detail['charts'] = [{key: item.get(key) for key in ('title', 'type', 'start', 'end')} for item in sheet['charts']]
    if suggestion := sheet.get('suggested_review'):
        detail['suggested_review'] = {**suggestion, 'regions': [region_summary(item) for item in suggestion['regions']]}
    return {**sheet_summary(sheet), **detail}


def cross_sheet_references(cell, sheet_name, sheet_names):
    formula_data = cell.get('formula') or {}
    formula = formula_data.get('text') or formula_data.get('raw_text')
    if not formula or '!' not in formula:
        return []
    try:
        tokens = Tokenizer(formula if formula.startswith('=') else '=' + formula).items
    except TokenizerError:
        return []
    refs = []
    for token in tokens:
        if token.type != 'OPERAND' or token.subtype != 'RANGE' or '!' not in token.value or '[' in token.value:
            continue
        try:
            target, bounds = range_to_tuple(token.value)
        except ValueError:
            continue
        target = sheet_names.get(target.replace("''", "'").casefold())
        if target is None or target == sheet_name or None in bounds:
            continue
        left, top, right, bottom = bounds
        item = {'sheet': target, 'start': f'{get_column_letter(left)}{top}', 'end': f'{get_column_letter(right)}{bottom}'}
        if item not in refs:
            refs.append(item)
    return refs


def cell_window(raw, sheet, row, column, rows, columns, *, exact=None):
    keys = ('address', 'display_text', 'hidden_merged', 'merged_range', 'cache_status')
    sheet_names = {item['name'].casefold(): item['name'] for item in raw['xlsx_sheets']}
    selected = []
    cells = occupied(sheet)
    for key, cell in cells.items():
        r, c = coordinate_to_tuple(key)
        if exact and key != exact:
            continue
        if not exact and not (row <= r < row + rows and column <= c < column + columns):
            continue
        item = {name: cell[name] for name in keys}
        if exact:
            item = cell.copy()
            item['formula_references'] = cross_sheet_references(cell, sheet['name'], sheet_names)
        else:
            item.update(display_text=cell['display_text'][:200], truncated=len(cell['display_text']) > 200)
        selected.append(item)
    _, _, right, bottom = sheet['bounds']
    return {'cells': selected, 'row': row, 'column': column,
            'last_row': min(bottom, row + rows - 1), 'last_column': min(right, column + columns - 1)}
