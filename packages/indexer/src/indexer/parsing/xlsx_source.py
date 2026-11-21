"""Native cell values, presentation metadata and raw chart definitions."""

import math
import re
from datetime import date, datetime, time, timedelta
from xml.etree.ElementTree import tostring

from openpyxl.cell import Cell
from openpyxl.styles.numbers import is_date_format, is_timedelta_format
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from openpyxl.utils.datetime import from_excel, from_ISO8601
from openpyxl.xml.functions import tostring as excel_xml


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


def cell_record(cell, value, formula, cached, merge, *, hidden=None):
    data_type = hidden['cell_type'] if hidden else cell.data_type
    value, value_type = typed_value(value, data_type)
    cached, cache_type = typed_value(cached, data_type)
    display = cached if formula else value
    text = ('계산값 없음' if formula else '') if display is None else str(display)
    display_kind = 'canonical'
    if type(display) in {int, float} and re.fullmatch(r'0+', cell.number_format):
        text = f'{display:.0f}'.zfill(len(cell.number_format) + int(display < 0))
        display_kind = 'formatted'
    link = cell.hyperlink
    return {'address': cell.coordinate, 'type': data_type, 'value': value, 'value_type': value_type,
            'formula': formula, 'cached_value': cached, 'cached_value_type': cache_type,
            'cache_status': ('missing' if cached is None else 'stored') if formula else None,
            'display_text': text, 'display_kind': display_kind,
            'number_format': cell.number_format, 'date_format': cell.number_format if is_date_format(cell.number_format) else None,
            'bold': bool(cell.font.bold), 'style_id': cell.style_id,
            'alignment': dict(cell.alignment.to_tree().attrib),
            'hyperlink': {key: getattr(link, key) for key in ('target', 'location', 'display', 'tooltip')} if link else None,
            'merged_range': merge, 'hidden_merged': bool(hidden), 'has_content': bool(formula or value is not None or hidden)}


def sheet_record(book, sheet, inspected, values):
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
              'hidden_column_ranges': [], 'hyperlinks': inspected.get('native_links', {}), 'bounds': [1, 1, 1, 1]}
    if values is None:
        return result
    result.update(
        merged_ranges=sorted(str(item) for item in sheet.merged_cells.ranges),
        hidden_rows=sorted(index for index, dim in sheet.row_dimensions.items() if dim.hidden),
        hidden_columns=sorted(index for index, dim in sheet.column_dimensions.items() if dim.hidden),
        hidden_column_ranges=[{'start': dim.min, 'end': dim.max} for dim in sheet.column_dimensions.values() if dim.hidden],
    )
    for table in sheet.tables.values():
        left, top, right, _ = range_boundaries(table.ref)
        result['tables'].append({'name': table.name, 'ref': table.ref,
                                 'headers': [sheet.cell(top, col).coordinate for col in range(left, right + 1)]
                                 if table.headerRowCount else [],
                                 'totals_rows': table.totalsRowCount or int(bool(table.totalsRowShown))})
    merge_boxes = [(ref, range_boundaries(ref)) for ref in result['merged_ranges']]
    formulas = {item['cell'].upper(): item for item in inspected.get('formulas', [])}
    raw_cells = inspected.get('native_cells', {})
    links = inspected.get('native_links', {})
    # Accessing a cell in the cached workbook can materialize blank cells, so take
    # the original worksheet entries from the formula workbook.
    hidden_values = {item['cell'] for item in inspected.get('hidden_merged_cells', [])}
    for cell in sorted(sheet._cells.values(), key=lambda item: (item.row, item.column)):
        if not isinstance(cell, Cell) or (cell.value is None and not cell.has_style
                                          and not cell.hyperlink and cell.coordinate not in raw_cells):
            continue
        details = formulas.get(cell.coordinate, {})
        formula = None
        cached = None
        if cell.data_type == 'f':
            value = cell.value
            formula = {'text': value if isinstance(value, str) else getattr(value, 'text', None),
                       'raw_text': details.get('formula'), 'attributes': details.get('formula_attributes', {})}
            cached = values[cell.coordinate].value
            raw_cache = details.get('cached_value')
            if cached is None and raw_cache is not None:
                cached = decode_raw(raw_cache, details.get('cell_type') or 'n', cell.number_format, book.epoch)
        merge = next((ref for ref, (left, top, _, _) in merge_boxes
                      if (cell.row, cell.column) == (top, left)), None)
        raw = raw_cells.get(cell.coordinate, {})
        value = cell.value
        if value is None and raw.get('inline_present'):
            value = raw['inline_text']
        record = cell_record(cell, None if formula else value, formula, cached, merge)
        record.update(raw_value=raw.get('raw_value'), raw_xml_type=raw.get('xml_type'),
                      source_style_id=raw.get('source_style_id'), hyperlink=links.get(cell.coordinate, record['hyperlink']))
        if formula:
            record['raw_cached_value'] = details.get('cached_value')
            record['cached_value'], record['cached_value_type'] = typed_value(cached, details.get('cell_type'))
        result['cells'].append(record)
    visible = {item['address'] for item in result['cells']}
    for key in raw_cells.keys() - visible - hidden_values:
        raw = raw_cells[key]
        row, col = coordinate_to_tuple(key)
        merge = next((ref for ref, (a, b, c, d) in merge_boxes if a <= col <= c and b <= row <= d), None)
        cell = Cell(sheet, row=row, column=col, style_array=book._cell_styles[raw['source_style_id']])
        record = cell_record(cell, None, None, None, merge)
        record.update(raw_value=raw['raw_value'], raw_xml_type=raw['xml_type'],
                      source_style_id=raw['source_style_id'], hyperlink=links.get(key), hidden_merged=bool(merge))
        result['cells'].append(record)
    result['cells'].sort(key=lambda item: coordinate_to_tuple(item['address']))
    for hidden in inspected.get('hidden_merged_cells', []):
        row, col = coordinate_to_tuple(hidden['cell'])
        cell = Cell(sheet, row=row, column=col, style_array=book._cell_styles[hidden['style_id']])
        formula = ({'text': '=' + hidden['formula'] if hidden['formula'] else None,
                    'raw_text': hidden['formula'], 'attributes': hidden['formula_attributes']}
                   if hidden['formula_attributes'] is not None else None)
        value = decode_raw(hidden['value'], hidden['cell_type'], cell.number_format, book.epoch)
        record = cell_record(cell, None if formula else value, formula, value if formula else None,
                             hidden['merged_range'], hidden=hidden)
        record.update(raw_value=hidden['raw_value'], raw_xml_type=hidden['cell_type'],
                      source_style_id=hidden['style_id'], hyperlink=links.get(hidden['cell']))
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


def search_value(cell):
    text = cell['display_text']
    if cell['formula']:
        formula = cell['formula'].get('text') or cell['formula'].get('raw_text')
        text = ' / '.join(filter(None, (text, f'수식 {formula}' if formula else '수식')))
    if cell['hidden_merged']:
        text = f'병합 내부 숨은 값 {cell["address"]} / {text}'
    return text
