"""검색 표현에서 원본 요소와 표 셀이 연결되었는지 보수적으로 점검한다."""
import re

from indexer.embedding_input import embedding_text
from indexer.parsing.normalization import cells_from, parse_table_html


def audit_representation(elements, chunks):
    by_id = {element["element_id"]: element for element in elements}
    referenced = {eid for chunk in chunks for eid in [*chunk.get("element_ids", []),
                  *chunk.get("heading_element_ids", []), *chunk.get("context_element_ids", [])]}
    unknown = sorted(referenced - by_id.keys())
    if unknown:
        raise ValueError("검색 청크의 원본 요소 참조를 확인할 수 없습니다")
    excluded = [{"element_id": e["element_id"], "page": e.get("page"), "reason": e["search_exclusion"]}
                for e in elements if e.get("search_exclusion")]
    unrepresented = [e["element_id"] for e in elements if e.get("text") and not e.get("search_exclusion")
                     and e["element_id"] not in referenced]
    missing_cells = []
    missing_text_terms = []
    shortened_headings = [chunk["chunk_id"] for chunk in chunks if chunk.get("heading_text")
                          and embedding_text(" > ".join(chunk.get("heading_path", []))) != chunk["heading_text"]]
    shortened_context = sorted({eid for chunk in chunks for eid in chunk.get("context_element_ids", [])
                                if eid in by_id and by_id[eid].get("kind") == "caption"
                                and embedding_text(by_id[eid].get("text", "")) not in chunk.get("text", "")})
    for element in elements:
        if element.get('search_exclusion'):
            continue
        if element.get("kind") in {"paragraph", "text", "list", "list_item", "figure", "chart", "image"} and element.get("text"):
            original = set(re.findall(r"[^\W_]+", embedding_text(element["text"]).casefold()))
            represented_text = " ".join(embedding_text(chunk.get("body_text") or chunk["text"]).casefold()
                                       for chunk in chunks if element["element_id"] in chunk.get("element_ids", []))
            missing = [term for term in original if term not in represented_text]
            if missing:
                missing_text_terms.append({"element_id": element["element_id"], "missing_term_count": len(missing)})
        table = parse_table_html(element.get("html") or "").find("table")
        if not table or table.find("table"):
            continue
        present = {(cell["row"], cell["column"]) for chunk in chunks
                   if element["element_id"] in chunk.get("element_ids", []) for cell in chunk.get("table_cells", [])}
        layout = element.get('layout') or {}
        row_offset = layout.get('row_offset', 0) if layout.get('space') == 'xlsx_region' else 0
        column_offset = layout.get('column_offset', 0) if layout.get('space') == 'xlsx_region' else 0
        if region := element.get('xlsx_region'):
            if region['kind'] == 'title' and any(element['element_id'] in chunk.get('context_element_ids', []) for chunk in chunks):
                continue
            missing_cells.extend({'element_id': element['element_id'], 'row': cell['row'], 'column': cell['column']}
                                 for cell in region['cells'] if cell['text'].strip()
                                 and (cell['row'], cell['column']) not in present)
            continue
        for cell in cells_from(table):
            row, column = cell.row + 1 + row_offset, cell.column + 1 + column_offset
            if cell.search_text and (row, column) not in present:
                missing_cells.append({"element_id": element["element_id"], "row": row, "column": column})
    return {"excluded": excluded, "unrepresented_elements": unrepresented, "missing_table_cells": missing_cells,
            "missing_text_terms": missing_text_terms, "shortened_heading_chunks": shortened_headings,
            "shortened_context_elements": shortened_context}
