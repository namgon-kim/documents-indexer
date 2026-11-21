"""Office 원본의 시트·슬라이드 정보를 파서 페이지와 독립적으로 기록한다."""

import posixpath
import re
from pathlib import PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

from indexer.contracts import IndexerError

MAX_XML_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_EXPANDED_BYTES = 500 * 1024 * 1024
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PRESENTATION = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _xml(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError("원본 구조 파일이 너무 큽니다")
    data = archive.read(info)
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("원본 구조 파일에 지원하지 않는 선언이 있습니다")
    return ElementTree.fromstring(data)


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


def inspect_office(path):
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
            return _spreadsheet(archive) if kind == ".xlsx" else _presentation(archive)
    except (BadZipFile, KeyError, ValueError, ElementTree.ParseError) as error:
        raise IndexerError("INVALID_DOCUMENT", "문서 내부 구조를 확인할 수 없습니다.", 415) from error


def _spreadsheet(archive):
    workbook = _xml(archive, "xl/workbook.xml")
    relations = _relationships(archive, "xl/_rels/workbook.xml.rels", "xl")
    shared = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        root = _xml(archive, 'xl/sharedStrings.xml')
        shared = [''.join(node.text or '' for node in item.iter(f'{MAIN}t')) for item in root.findall(f'{MAIN}si')]
    sheets = []
    for node in workbook.findall(f"{MAIN}sheets/{MAIN}sheet"):
        name, rel_id = node.get("name"), node.get(REL)
        if not name or rel_id not in relations:
            raise ValueError("시트 정보를 확인할 수 없습니다")
        target = relations[rel_id]
        summary = {"name": name, "visibility": node.get("state", "visible"), "source_path": target,
                   "formulas": [], "formula_scan_complete": True}
        try:
            sheet = _xml(archive, target)
        except ValueError as error:
            if str(error) != "원본 구조 파일이 너무 큽니다":
                raise
            summary["formula_scan_complete"] = False
            sheets.append(summary)
            continue
        if sheet.tag not in {f"{MAIN}worksheet", f"{MAIN}chartsheet"}:
            raise ValueError("지원하지 않는 시트 구조입니다")
        summary["sheet_kind"] = "chart" if sheet.tag == f"{MAIN}chartsheet" else "worksheet"
        merges = []
        for item in sheet.findall(f'{MAIN}mergeCells/{MAIN}mergeCell'):
            bounds = range_boundaries(item.get('ref', ''))
            merges.append((item.get('ref'), bounds))
        summary['hidden_merged_cells'] = []
        for cell in sheet.iter(f"{MAIN}c"):
            address = cell.get('r', '')
            if not re.fullmatch(r"[A-Za-z]{1,3}[1-9][0-9]{0,6}", address):
                raise ValueError("셀 주소가 올바르지 않습니다")
            row, col = coordinate_to_tuple(address.upper())
            owner = next((ref for ref, (left, top, right, bottom) in merges
                          if left <= col <= right and top <= row <= bottom and (col, row) != (left, top)), None)
            value = cell.find(f'{MAIN}v')
            inline = cell.find(f'{MAIN}is')
            formula = cell.find(f'{MAIN}f')
            raw = value.text if value is not None else None
            if owner and (raw is not None or inline is not None or formula is not None):
                text = ''.join(node.text or '' for node in inline.iter(f'{MAIN}t')) if inline is not None else raw
                if cell.get('t') == 's' and raw is not None:
                    index = int(raw)
                    if not 0 <= index < len(shared):
                        raise ValueError('공유 문자열을 확인할 수 없습니다')
                    text = shared[index]
                summary['hidden_merged_cells'].append({'cell': address.upper(), 'merged_range': owner,
                    'value': text, 'cell_type': cell.get('t'), 'formula': formula.text if formula is not None else None})
            if formula is None:
                continue
            value = cell.find(f"{MAIN}v")
            summary["formulas"].append({"cell": address, "formula": formula.text,
                                        "formula_attributes": dict(formula.attrib),
                                        "cached_value": value.text if value is not None and value.text else None,
                                        "cell_type": cell.get("t")})
        sheets.append(summary)
    return {"format": "xlsx", "sheets": sheets, "parser_page_mapping": "unknown",
            "hidden_content_policy": "provider_dependent"}


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
