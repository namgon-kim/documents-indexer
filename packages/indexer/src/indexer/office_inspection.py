"""Office 원본의 시트·슬라이드 정보를 파서 페이지와 독립적으로 기록한다."""

import io
import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

from indexer.contracts import IndexerError

MAX_XML_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_EXPANDED_BYTES = 500 * 1024 * 1024
MAX_EXCEL_ROWS = 1048576
MAX_EXCEL_COLUMNS = 16384
MERGE_INDEX_CELLS = 500_000
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PRESENTATION = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
CELL_ADDRESS = re.compile(r"[A-Za-z]{1,3}[1-9][0-9]{0,6}")


@dataclass(frozen=True)
class SheetLimits:
    """Bounds for direct XLSX extraction, checked before a workbook is loaded."""
    rows: int = 5000
    columns: int = 100
    cells: int = 25000
    # Cell elements include formatting-only cells, which the workbook loader
    # materializes even though they carry no content.
    cell_elements: int = 100_000
    merges: int = 25_000
    workbook_cell_elements: int = 400_000

    def exceeds(self, max_row, max_col):
        return max_row > self.rows or max_col > self.columns or max_row * max_col > self.cells


NATIVE_SHEET_LIMITS = SheetLimits()


def table_limit_error():
    return IndexerError('NATIVE_TABLE_LIMIT', '시트 크기가 직접 추출 범위를 넘습니다. 다른 파서를 사용해 주세요.', 413)


def merge_lookup(boxes):
    """Return a lookup from (row, column) to the merged range that covers it."""
    area = sum((right - left + 1) * (bottom - top + 1) for _, (left, top, right, bottom) in boxes)
    if area <= MERGE_INDEX_CELLS:
        owner = {}
        for ref, (left, top, right, bottom) in boxes:
            for row in range(top, bottom + 1):
                for col in range(left, right + 1):
                    owner.setdefault((row, col), ref)
        return lambda row, col: owner.get((row, col))
    return lambda row, col: next((ref for ref, (left, top, right, bottom) in boxes
                                  if left <= col <= right and top <= row <= bottom), None)


def _read_part(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError("원본 구조 파일이 너무 큽니다")
    data = archive.read(info)
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("원본 구조 파일에 지원하지 않는 선언이 있습니다")
    return data


def _xml(archive, name):
    return ElementTree.fromstring(_read_part(archive, name))


def _row_number(text):
    try:
        number = int(text)
    except ValueError:
        value = float(text)
        if not value.is_integer():
            raise ValueError("행 번호가 올바르지 않습니다") from None
        number = int(value)
    if not 1 <= number <= MAX_EXCEL_ROWS:
        raise ValueError("행 번호가 올바르지 않습니다")
    return number


def _sheet_outline(data):
    """Read sheet-level metadata. Merge ranges follow the cell data in the XML."""
    outline = {"tag": None, "merges": [], "hyperlinks": [], "drawings": []}
    sheet_data = None
    for event, element in ElementTree.iterparse(io.BytesIO(data), events=("start", "end")):
        tag = element.tag
        if event == "start":
            if outline["tag"] is None:
                outline["tag"] = tag
            elif tag == f"{MAIN}sheetData":
                sheet_data = element
            continue
        if tag == f"{MAIN}mergeCell":
            outline["merges"].append(element.get("ref", ""))
        elif tag == f"{MAIN}hyperlink":
            outline["hyperlinks"].append(dict(element.attrib))
        elif tag == f"{MAIN}drawing":
            outline["drawings"].append(dict(element.attrib))
        elif tag == f"{MAIN}row":
            element.clear()
            if sheet_data is not None:
                sheet_data.remove(element)
    return outline


def _sheet_cells(data):
    """Yield (row, column, element) using the same positional rules as openpyxl.

    `r` is optional on rows and cells. A row without `r` follows the previous row,
    and a cell without `r` follows the previous cell of the same row.
    """
    row_counter, col_counter, row_explicit = 0, 0, False
    sheet_data, seen = None, set()
    for event, element in ElementTree.iterparse(io.BytesIO(data), events=("start", "end")):
        tag = element.tag
        if event == "start":
            if tag == f"{MAIN}sheetData":
                sheet_data = element
            elif tag == f"{MAIN}row":
                if element.get("r") is not None:
                    row_counter, row_explicit = _row_number(element.get("r")), True
                else:
                    row_counter, row_explicit = row_counter + 1, False
                    if row_counter > MAX_EXCEL_ROWS:
                        raise ValueError("행 번호가 올바르지 않습니다")
                col_counter = 0
            continue
        if tag == f"{MAIN}c":
            address = element.get("r")
            if address:
                if not CELL_ADDRESS.fullmatch(address):
                    raise ValueError("셀 주소가 올바르지 않습니다")
                row, col = coordinate_to_tuple(address.upper())
                if row_explicit and row != row_counter:
                    raise ValueError("셀 주소가 행 위치와 맞지 않습니다")
            else:
                if row_counter < 1:
                    raise ValueError("셀의 행 위치를 확인할 수 없습니다")
                row, col = row_counter, col_counter + 1
            if not (1 <= col <= MAX_EXCEL_COLUMNS and 1 <= row <= MAX_EXCEL_ROWS) or (row, col) in seen:
                raise ValueError("셀 주소가 올바르지 않거나 중복됩니다")
            seen.add((row, col))
            col_counter = col
            yield row, col, element
            element.clear()
        elif tag == f"{MAIN}row":
            element.clear()
            if sheet_data is not None:
                sheet_data.remove(element)


def _relationships(archive, name, base):
    root = _xml(archive, name)
    result = {}
    for rel in root.findall(PKG_REL):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))
        if path.startswith(("../", "/")) or "\\" in path:
            raise ValueError("잘못된 문서 내부 경로입니다")
        result[rel.get("Id")] = path
    return result


def inspect_office(path, *, native_cells=False, limits=None):
    """Inspect Office package structure. `limits` rejects oversized sheets early."""
    kind = path.suffix.lower()
    if kind not in {".xlsx", ".pptx"}:
        return None
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(entries) > MAX_ENTRIES or len(set(names)) != len(names)
                or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES):
                raise ValueError("원본 문서의 압축 범위가 허용 범위를 넘었습니다")
            if any(item.flag_bits & 1 or PurePosixPath(item.filename).is_absolute()
                   or ".." in PurePosixPath(item.filename).parts or "\\" in item.filename for item in entries):
                raise ValueError("원본 문서 내부 파일을 확인할 수 없습니다")
            return _spreadsheet(archive, native_cells, limits) if kind == ".xlsx" else _presentation(archive)
    except (BadZipFile, KeyError, ValueError, ElementTree.ParseError) as error:
        raise IndexerError("INVALID_DOCUMENT", "문서 내부 구조를 확인할 수 없습니다.", 415) from error


def _spreadsheet(archive, native_cells, limits=None):
    workbook = _xml(archive, "xl/workbook.xml")
    workbook_elements = 0
    relations = _relationships(archive, "xl/_rels/workbook.xml.rels", "xl")
    shared = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        root = _xml(archive, 'xl/sharedStrings.xml')
        shared = [''.join(node.text or '' for node in [*item.findall(f'{MAIN}t'), *item.findall(f'{MAIN}r/{MAIN}t')]) for item in root.findall(f'{MAIN}si')]
    sheets = []
    for node in workbook.findall(f"{MAIN}sheets/{MAIN}sheet"):
        name, rel_id = node.get("name"), node.get(REL)
        if not name or rel_id not in relations:
            raise ValueError("시트 정보를 확인할 수 없습니다")
        target = relations[rel_id]
        summary = {"name": name, "visibility": node.get("state", "visible"), "source_path": target,
                   "formulas": [], "formula_scan_complete": True}
        try:
            data = _read_part(archive, target)
        except ValueError as error:
            if str(error) != "원본 구조 파일이 너무 큽니다":
                raise
            summary["formula_scan_complete"] = False
            sheets.append(summary)
            continue
        # Stream the worksheet twice instead of building the whole XML tree.
        outline = _sheet_outline(data)
        if outline["tag"] not in {f"{MAIN}worksheet", f"{MAIN}chartsheet"}:
            raise ValueError("지원하지 않는 시트 구조입니다")
        summary["sheet_kind"] = "chart" if outline["tag"] == f"{MAIN}chartsheet" else "worksheet"
        merges = [(ref, range_boundaries(ref)) for ref in outline["merges"]]
        max_row = max((bottom for _, (_, _, _, bottom) in merges), default=0)
        max_col = max((right for _, (_, _, right, _) in merges), default=0)
        if limits and (len(merges) > limits.merges or (merges and limits.exceeds(max_row, max_col))):
            raise table_limit_error()
        owner_of = merge_lookup(merges)
        anchors = {ref: (top, left) for ref, (left, top, _, _) in merges}
        summary['hidden_merged_cells'] = []
        if native_cells:
            summary['native_cells'] = {}
            summary['native_links'] = _sheet_links(archive, target, outline["hyperlinks"])
            summary['native_charts'] = _sheet_charts(archive, target, outline["drawings"])
        elements = 0
        for row, col, cell in _sheet_cells(data):
            elements += 1
            workbook_elements += 1
            if limits and (elements > limits.cell_elements or workbook_elements > limits.workbook_cell_elements):
                raise table_limit_error()
            address = f"{get_column_letter(col)}{row}"
            ref = owner_of(row, col)
            owner = ref if ref and anchors[ref] != (row, col) else None
            value = cell.find(f'{MAIN}v')
            inline = cell.find(f'{MAIN}is')
            formula = cell.find(f'{MAIN}f')
            raw = value.text if value is not None else None
            # A present string cache can legitimately contain an empty string.
            if formula is not None and value is not None and raw is None and cell.get('t') == 'str':
                raw = ''
            content = raw is not None or inline is not None or formula is not None
            if content and (row > max_row or col > max_col):
                max_row, max_col = max(max_row, row), max(max_col, col)
                if limits and limits.exceeds(max_row, max_col):
                    raise table_limit_error()
            style = int(cell.get('s', '0'))
            if native_cells and (content or style):
                # Unstyled empty placeholders carry neither content nor formatting.
                summary['native_cells'][address] = {'raw_value': raw, 'xml_type': cell.get('t') or 'n',
                    'source_style_id': style,
                    'inline_text': ''.join(node.text or '' for node in [*inline.findall(f'{MAIN}t'),
                                                                       *inline.findall(f'{MAIN}r/{MAIN}t')])
                    if inline is not None else None, 'inline_present': inline is not None}
            if owner and content:
                text = ''.join(node.text or '' for node in [*inline.findall(f'{MAIN}t'), *inline.findall(f'{MAIN}r/{MAIN}t')]) if inline is not None else raw
                if cell.get('t') == 's' and raw is not None:
                    index = int(raw)
                    if not 0 <= index < len(shared):
                        raise ValueError('공유 문자열을 확인할 수 없습니다')
                    text = shared[index]
                summary['hidden_merged_cells'].append({'cell': address.upper(), 'merged_range': owner,
                    'value': text, 'raw_value': raw, 'cell_type': cell.get('t') or 'n',
                    'style_id': int(cell.get('s', '0')), 'formula': formula.text if formula is not None else None,
                    'formula_attributes': dict(formula.attrib) if formula is not None else None})
            if formula is None:
                continue
            summary["formulas"].append({"cell": address, "formula": formula.text,
                                        "formula_attributes": dict(formula.attrib),
                                        "cached_value": raw,
                                        "cell_type": cell.get("t")})
        sheets.append(summary)
    return {"format": "xlsx", "sheets": sheets, "parser_page_mapping": "unknown",
            "hidden_content_policy": "provider_dependent"}



def _relationship_part(part):
    directory, filename = posixpath.split(part)
    return posixpath.join(directory, '_rels', filename + '.rels'), directory


def _sheet_links(archive, part, hyperlinks):
    path, _ = _relationship_part(part)
    external = {}
    if path in archive.namelist():
        external = {rel.get('Id'): rel.get('Target') for rel in _xml(archive, path).findall(PKG_REL)
                    if rel.get('TargetMode') == 'External' and rel.get('Type', '').endswith('/hyperlink')}
    links = {}
    for link in hyperlinks:
        links[link.get('ref')] = {'target': external.get(link.get(REL)),
                                 **{key: link.get(key) for key in ('location', 'display', 'tooltip')}}
    return links


def _sheet_charts(archive, part, drawing_nodes):
    namespace = '{http://schemas.openxmlformats.org/drawingml/2006/chart}'
    path, directory = _relationship_part(part)
    if not drawing_nodes or path not in archive.namelist():
        return []
    sheet_relations = _relationships(archive, path, directory)
    charts = []
    for node in drawing_nodes:
        drawing_part = sheet_relations.get(node.get(REL))
        if not drawing_part:
            continue
        drawing = _xml(archive, drawing_part)
        path, directory = _relationship_part(drawing_part)
        if path not in archive.namelist():
            continue
        drawing_relations = _relationships(archive, path, directory)
        for chart in drawing.iter(f'{namespace}chart'):
            source = drawing_relations.get(chart.get(REL))
            if source:
                chart_xml = _xml(archive, source)
                data = archive.read(source)
                try:
                    definition = data.decode('utf-8-sig')
                except UnicodeError:
                    definition = ElementTree.tostring(chart_xml, encoding='unicode')
                charts.append({'source_path': source, 'definition_xml': definition})
    return charts

def _presentation(archive):
    presentation = _xml(archive, "ppt/presentation.xml")
    relations = _relationships(archive, "ppt/_rels/presentation.xml.rels", "ppt")
    names = set(archive.namelist())
    slides = []
    for number, node in enumerate(presentation.findall(f"{PRESENTATION}sldIdLst/{PRESENTATION}sldId"), 1):
        target = relations.get(node.get(REL))
        if not target:
            raise ValueError("슬라이드 정보를 확인할 수 없습니다")
        slide = _xml(archive, target)
        if slide.tag != f"{PRESENTATION}sld":
            raise ValueError("슬라이드 구조를 확인할 수 없습니다")
        directory, filename = posixpath.split(target)
        rel_path = posixpath.join(directory, "_rels", filename + ".rels")
        related = _relationships(archive, rel_path, directory) if rel_path in names else {}
        notes = [value for value in related.values() if value.startswith("ppt/notesSlides/")]
        slides.append({"number": number, "source_path": target, "hidden": slide.get("show") in {"0", "false"},
                       "notes_present": None if notes and not all(value in names for value in notes) else bool(notes)})
    return {"format": "pptx", "slides": slides, "parser_page_mapping": "unknown",
            "hidden_content_policy": "provider_dependent", "notes_policy": "provider_dependent"}
