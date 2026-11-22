"""Native cell values, presentation metadata and raw chart definitions."""

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from xml.etree.ElementTree import tostring

from openpyxl.cell import Cell
from openpyxl.styles.numbers import is_date_format, is_timedelta_format
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from openpyxl.utils.datetime import from_excel, from_ISO8601
from openpyxl.xml.functions import tostring as excel_xml

from indexer.office_inspection import merge_lookup
from indexer.parsing.xlsx_format import render, value_category

# Style-only cells are kept as compressed rectangles, not as content records.
STYLE_RANGE_LIMIT = 5000


def xml_text(value):
    return excel_xml(value).decode('utf-8')


def typed_value(value, cell_type=None):
    if isinstance(value, timedelta):
        return value.total_seconds(), 'duration'
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(), type(value).__name__
    if isinstance(value, float) and not math.isfinite(value):
        return '#NUM!', 'error'
    if cell_type == 'e' and value is not None:
        return value, 'error'
    return value, {type(None): 'null', bool: 'boolean', str: 'string',
                   int: 'integer', float: 'number'}[type(value)]


def decode_raw(value, kind, number_format, epoch):
    if value is None:
        return None
    if kind == 'b':
        return value == '1'
    if kind == 'd':
        return from_ISO8601(value)
    if kind == 'n':
        number = float(value) if any(token in value.lower() for token in ('.', 'e')) else int(value)
        if is_date_format(number_format):
            try:
                return from_excel(number, epoch, timedelta=is_timedelta_format(number_format))
            except (OverflowError, ValueError):
                return '#VALUE!'
        return number
    return value



def number_format_id(cell):
    return getattr(getattr(cell, '_style', None), 'numFmtId', None)


def cell_record(cell, value, formula, cached, merge, *, cache_xml_type=None, hidden=None):
    data_type = hidden['cell_type'] if hidden else cell.data_type
    value, value_type = typed_value(value, data_type)
    cached, cache_type = typed_value(cached, cache_xml_type)
    shown, shown_type = (cached, cache_type) if formula else (value, value_type)
    text, display_kind, applied = render(shown, shown_type, cell.number_format, number_format_id(cell))
    if formula and cached is None:
        text, display_kind, applied = '계산값 없음', 'missing_cache', None
    link = cell.hyperlink
    return {'address': cell.coordinate, 'type': data_type, 'value': value, 'value_type': value_type,
            'formula': formula, 'cached_value': cached, 'cached_value_type': cache_type,
            'cache_status': ('missing' if cached is None else 'stored') if formula else None,
            'display_text': text, 'display_kind': display_kind, 'display_format': applied,
            'value_category': value_category(cell.number_format, shown_type),
            'number_format': cell.number_format, 'date_format': cell.number_format if is_date_format(cell.number_format) else None,
            'bold': bool(cell.font.bold), 'style_id': cell.style_id,
            'alignment': dict(cell.alignment.to_tree().attrib),
            'hyperlink': {key: getattr(link, key) for key in ('target', 'location', 'display', 'tooltip')} if link else None,
            'merged_range': merge, 'hidden_merged': bool(hidden), 'has_content': bool(formula or value is not None or hidden)}


def table_totals(table):
    """Interpret Excel totals-row metadata without inventing a totals row.

    `totalsRowShown` only says whether a totals row was ever shown; the number of
    totals rows inside the table range is `totalsRowCount`.
    """
    _, top, _, bottom = range_boundaries(table.ref)
    header_rows = 1 if table.headerRowCount is None else int(table.headerRowCount)
    count = int(table.totalsRowCount or 0)
    body = (bottom - top + 1) - max(0, header_rows)
    if count < 0 or count > max(0, body):
        return 0, 'inconsistent'
    if count:
        return count, 'declared'
    return 0, 'shown_without_count' if table.totalsRowShown else 'none'


def style_ranges(points, limit=STYLE_RANGE_LIMIT):
    """Compress style-only cells into equal-style rectangles for structure hints."""
    by_row = defaultdict(list)
    for row, col, style in points:
        by_row[row].append((col, style))
    runs = []
    for row in sorted(by_row):
        current = None
        for col, style in sorted(by_row[row]):
            if current and current[2] == style and current[1] == col - 1:
                current[1] = col
            else:
                current = [col, col, style]
                runs.append((row, current))
    open_boxes, boxes = {}, []
    for row, (start, end, style) in runs:
        box = open_boxes.get((start, end, style))
        if box and box[3] == row - 1:
            box[3] = row
        else:
            box = [start, row, end, row, style]
            open_boxes[(start, end, style)] = box
            boxes.append(box)
    ranges = [{'start': f'{get_column_letter(left)}{top}', 'end': f'{get_column_letter(right)}{bottom}',
               'style_id': style} for left, top, right, bottom, style in boxes[:limit]]
    return ranges, len(boxes) > limit


def sheet_record(book, sheet, inspected, worksheet):
    charts = []
    originals = inspected.get('native_charts', [])
    matched_sources = len(originals) == len(sheet._charts)
    for index, chart in enumerate(sheet._charts):
        anchor = getattr(chart.anchor, '_from', None)
        end = getattr(chart.anchor, 'to', None)
        def point(marker):
            return {'row': marker.row + 1, 'column': marker.col + 1,
                    'row_offset_emu': marker.rowOff, 'column_offset_emu': marker.colOff} if marker else None
        charts.append({'kind': 'chart', 'type': type(chart).__name__, 'start': point(anchor), 'end': point(end),
                       **(originals[index] if matched_sources else {'definition_xml': xml_text(chart._write())}),
                       'title': ''.join(span.t for para in chart.title.tx.rich.p for span in (para.r or []) if span.t)
                       if chart.title and chart.title.tx and chart.title.tx.rich else None})
    if not matched_sources:
        charts.extend({'kind': 'chart', 'type': 'OOXML', 'title': None, **source} for source in originals)
    result = {'name': sheet.title, 'visibility': sheet.sheet_state, 'cells': [], 'hidden_merged_cells': [],
              'charts': charts, 'merged_ranges': [], 'tables': [], 'hidden_rows': [], 'hidden_columns': [],
              'hidden_column_ranges': [], 'hyperlinks': inspected.get('native_links', {}), 'bounds': [1, 1, 1, 1],
              'style_ranges': [], 'style_ranges_truncated': False, 'style_only_cells': 0}
    if not worksheet:
        return result
    result.update(
        merged_ranges=sorted(str(item) for item in sheet.merged_cells.ranges),
        hidden_rows=sorted(index for index, dim in sheet.row_dimensions.items() if dim.hidden),
        hidden_columns=sorted(index for index, dim in sheet.column_dimensions.items() if dim.hidden),
        hidden_column_ranges=[{'start': dim.min, 'end': dim.max} for dim in sheet.column_dimensions.values() if dim.hidden],
    )
    for table in sheet.tables.values():
        left, top, right, _ = range_boundaries(table.ref)
        totals, basis = table_totals(table)
        result['tables'].append({'name': table.name, 'ref': table.ref,
                                 'headers': [sheet.cell(top, col).coordinate for col in range(left, right + 1)]
                                 if table.headerRowCount else [],
                                 'totals_rows': totals, 'totals_rows_basis': basis,
                                 'totals_row_shown': bool(table.totalsRowShown)})
    merge_boxes = [(ref, range_boundaries(ref)) for ref in result['merged_ranges']]
    anchors = {(top, left): ref for ref, (left, top, _, _) in merge_boxes}
    owner = merge_lookup(merge_boxes)
    formulas = {item['cell'].upper(): item for item in inspected.get('formulas', [])}
    raw_cells = inspected.get('native_cells', {})
    links = inspected.get('native_links', {})
    hidden_values = {item['cell'] for item in inspected.get('hidden_merged_cells', [])}
    styled, known = [], set()
    for cell in sorted(sheet._cells.values(), key=lambda item: (item.row, item.column)):
        if not isinstance(cell, Cell):
            continue
        known.add(cell.coordinate)
        raw = raw_cells.get(cell.coordinate, {})
        value = cell.value
        if value is None and raw.get('inline_present'):
            value = raw['inline_text']
        hyperlink = links.get(cell.coordinate) or cell.hyperlink
        if value is None and cell.data_type != 'f' and not hyperlink:
            # Formatting without a value is a structure hint, not source content.
            if cell.has_style:
                styled.append((cell.row, cell.column, cell.style_id))
            continue
        details = formulas.get(cell.coordinate, {})
        formula = None
        cached = None
        if cell.data_type == 'f':
            formula = {'text': value if isinstance(value, str) else getattr(value, 'text', None),
                       'raw_text': details.get('formula'), 'attributes': details.get('formula_attributes', {})}
            # The worksheet XML keeps Excel's stored result next to the formula.
            raw_cache = details.get('cached_value')
            if raw_cache is not None:
                cached = decode_raw(raw_cache, details.get('cell_type') or 'n', cell.number_format, book.epoch)
        record = cell_record(cell, None if formula else value, formula, cached,
                             anchors.get((cell.row, cell.column)), cache_xml_type=details.get('cell_type'))
        record.update(raw_value=raw.get('raw_value'), raw_xml_type=raw.get('xml_type'),
                      source_style_id=raw.get('source_style_id'), hyperlink=links.get(cell.coordinate, record['hyperlink']))
        if formula:
            record['raw_cached_value'] = details.get('cached_value')
        result['cells'].append(record)
    # Raw XML cells the workbook loader did not keep, such as cells covered by a merge.
    for key in raw_cells.keys() - known - hidden_values:
        raw = raw_cells[key]
        row, col = coordinate_to_tuple(key)
        if raw['raw_value'] is None and not raw.get('inline_present') and not links.get(key):
            if raw['source_style_id']:
                styled.append((row, col, raw['source_style_id']))
            continue
        merge = owner(row, col)
        cell = Cell(sheet, row=row, column=col, style_array=book._cell_styles[raw['source_style_id']])
        record = cell_record(cell, None, None, None, merge)
        record.update(raw_value=raw['raw_value'], raw_xml_type=raw['xml_type'],
                      source_style_id=raw['source_style_id'], hyperlink=links.get(key), hidden_merged=bool(merge))
        result['cells'].append(record)
    result['cells'].sort(key=lambda item: coordinate_to_tuple(item['address']))
    result['style_ranges'], result['style_ranges_truncated'] = style_ranges(styled)
    result['style_only_cells'] = len(styled)
    for hidden in inspected.get('hidden_merged_cells', []):
        row, col = coordinate_to_tuple(hidden['cell'])
        cell = Cell(sheet, row=row, column=col, style_array=book._cell_styles[hidden['style_id']])
        formula = ({'text': '=' + hidden['formula'] if hidden['formula'] else None,
                    'raw_text': hidden['formula'], 'attributes': hidden['formula_attributes']}
                   if hidden['formula_attributes'] is not None else None)
        value = decode_raw(hidden['value'], hidden['cell_type'], cell.number_format, book.epoch)
        record = cell_record(cell, None if formula else value, formula, value if formula else None,
                             hidden['merged_range'], cache_xml_type=hidden['cell_type'] if formula else None, hidden=hidden)
        record.update(raw_value=hidden['raw_value'], raw_xml_type=hidden['cell_type'],
                      source_style_id=hidden['style_id'], hyperlink=links.get(hidden['cell']))
        if formula:
            record['raw_cached_value'] = hidden['raw_value']
        result['hidden_merged_cells'].append({'cell': hidden['cell'], **record})
    extents = [(cell['address'], cell['address']) for cell in [*result['cells'], *result['hidden_merged_cells']]
               if cell['has_content']]
    extents.extend(tuple(ref.split(':', 1)) if ':' in ref else (ref, ref)
                   for ref in [*result['merged_ranges'], *(table['ref'] for table in result['tables'])])
    if extents:
        corners = [coordinate_to_tuple(key) for pair in extents for key in pair]
        rows, cols = zip(*corners, strict=True)
        result['bounds'] = [min(cols), min(rows), max(cols), max(rows)]
    return result


def workbook_styles(book, sheets):
    used = {key for sheet in sheets for cell in [*sheet['cells'], *sheet['hidden_merged_cells']]
            for key in (cell['style_id'], cell['source_style_id']) if key is not None}
    used.update(item['style_id'] for sheet in sheets for item in sheet.get('style_ranges', []))
    styles = {}
    for key in sorted(used):
        style = book._cell_styles[key]
        styles[key] = {'font': xml_text(book._fonts[style.fontId].to_tree()),
                       'fill': xml_text(book._fills[style.fillId].to_tree()),
                       'border': xml_text(book._borders[style.borderId].to_tree())}
    theme = book.loaded_theme
    if theme and not isinstance(theme, bytes):
        theme = tostring(theme)
    return {'styles': styles, 'theme_xml': theme.decode('utf-8-sig') if theme else None, 'indexed_colors': list(book._colors),
            'date_epoch': book.epoch.isoformat()}



def formula_label(cell):
    formula = cell['formula']
    if not formula:
        return ''
    text = formula.get('text') or formula.get('raw_text')
    return f'수식 {text}' if text else '수식'


def source_value(cell):
    """Keep the displayed value's whitespace and show the formula on its own line."""
    text = cell['display_text']
    label = formula_label(cell)
    if cell['hidden_merged']:
        text = f'병합 내부 숨은 값\n{text}'
    return f'{text}\n{label}' if label else text

def search_value(cell):
    text = ' / '.join(filter(None, (cell['display_text'], formula_label(cell))))
    if cell['hidden_merged']:
        text = f'병합 내부 숨은 값 {cell["address"]} / {text}'
    return text
