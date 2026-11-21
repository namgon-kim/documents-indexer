"""Carry only explicit parser-provided Office locations, validated against the source."""

import re

from openpyxl.utils import get_column_letter

CELL = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")


def valid_cell(value):
    match = CELL.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return False
    column = 0
    for char in match.group(1):
        column = column * 26 + ord(char) - ord("A") + 1
    return column <= 16384 and int(match.group(2)) <= 1048576


def apply_source_locations(elements, raw_elements, source):
    kind = source.get("format")
    sheets = {item["name"] for item in source.get("sheets", [])}
    slides = {item["number"] for item in source.get("slides", [])}
    for element, raw in zip(elements, raw_elements, strict=True):
        location = raw.get("source_location")
        origin = 'native' if raw.get('_origin') in {'native_extraction', 'native_image', 'visual_analysis', 'visual_description'} else 'parser'
        if not isinstance(location, dict):
            continue
        if kind == "xlsx" and location.get("kind") == "xlsx_cell":
            name, cell = location.get("sheet"), location.get("cell")
            if isinstance(name, str) and name in sheets and isinstance(cell, str) and valid_cell(cell):
                element["office_location"] = {"kind": "xlsx_cell", "sheet": name, "cell": cell,
                                              "origin": origin, "status": "unverified"}
        elif kind == 'xlsx' and location.get('kind') == 'xlsx_range':
            name, start, end = location.get('sheet'), location.get('start'), location.get('end')
            if (isinstance(name, str) and name in sheets and isinstance(start, str) and valid_cell(start)
                and isinstance(end, str) and valid_cell(end)):
                element['office_location'] = {'kind': 'xlsx_range', 'sheet': name, 'start': start, 'end': end,
                                              'origin': origin, 'status': ('verified' if origin == 'native'
                                                  and (raw.get('_layout') or {}).get('boundary_status') == 'verified' else 'unverified')}
        elif kind == "pptx" and location.get("kind") == "pptx_slide":
            slide = location.get("slide")
            if type(slide) is int and slide in slides:
                element["office_location"] = {"kind": "pptx_slide", "slide": slide,
                                              "origin": origin, "status": "unverified"}
        elif kind == 'hwpx' and location.get('kind') == 'hwpx_section':
            section = location.get('section')
            if isinstance(section, str) and re.fullmatch(r'Contents/section\d+\.xml', section):
                element['office_location'] = {'kind': 'hwpx_section', 'section': section,
                                              'origin': 'native', 'status': 'unverified'}
        elif kind == 'docx' and location.get('kind') == 'docx_block':
            block = location.get('block')
            if type(block) is int and block > 0:
                element['office_location'] = {'kind': 'docx_block', 'block': block,
                                              'origin': 'native', 'status': 'unverified'}
        elif kind == 'hwp' and location.get('kind') == 'hwp_paragraph':
            section, paragraph = location.get('section'), location.get('paragraph')
            if type(section) is int and section >= 0 and type(paragraph) is int and paragraph >= 0:
                element['office_location'] = {'kind': 'hwp_paragraph', 'section': section, 'paragraph': paragraph,
                                              'origin': 'native', 'status': 'unverified'}
                source_id = location.get('source_paragraph_id')
                if isinstance(source_id, str) and source_id.isdigit():
                    element['office_location']['source_paragraph_id'] = source_id


def set_xlsx_chunk_evidence(chunk, segments):
    """Use the same fragment coordinates for initial and adapted chunks."""
    ref = chunk['element_refs'][0]
    cell_refs = [{'row': part['row'], 'column': part['column'], 'address': part['address'],
                  'hidden_merged': part['hidden_merged']} for part in segments]
    chunk['table_cells'] = cell_refs
    chunk['table_segments'] = segments
    chunk['source_segments'] = [{'element_id': ref['element_id'], 'order': chunk['first_order'],
                                 'text': part['value'], 'cell': part['address'],
                                 'hidden_merged': part['hidden_merged']} for part in segments]
    rows = [part['row'] for part in cell_refs]
    cols = [part['column'] for part in cell_refs]
    chunk['element_refs'] = [{**ref, 'office_location': {**(ref.get('office_location') or {}),
        'kind': 'xlsx_range', 'sheet': chunk['xlsx_sheet'],
        'start': f'{get_column_letter(min(cols))}{min(rows)}',
        'end': f'{get_column_letter(max(cols))}{max(rows)}',
        'cells': list(dict.fromkeys(part['address'] for part in segments))}}]
    chunk['source_range_quality'] = 'cell'
