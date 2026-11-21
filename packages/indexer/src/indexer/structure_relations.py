"""Conservative, explicitly marked relationships between nearby table elements."""

import re
from itertools import pairwise

from indexer.element_scope import same_scope
from indexer.parsing.normalization import cells_from, parse_table_html
from indexer.table_continuity import TABLE_NUMBER, column_headers, header_signature

CAPTION = re.compile(r"^[<\[(]?\s*(?:표|table)(?=\s|\d|[.:])", re.IGNORECASE)
CONTINUED = re.compile(r"(?:계속|이어짐|continued)", re.IGNORECASE)


def table_relationships(elements):
    relations = {}
    tables = {}
    for index, element in enumerate(elements):
        table = parse_table_html(element["html"]).find("table")
        if not table or table.find("table"):
            continue
        cells = cells_from(table)
        caption_tag = table.find("caption")
        caption = caption_tag.get_text(" ", strip=True) if caption_tag else ""
        caption_id = None
        if not caption:
            nearby = [elements[i] for i in (index - 1, index + 1) if 0 <= i < len(elements)
                      and elements[i]["kind"] == "caption" and same_scope(elements[i], element)
                      and CAPTION.match(elements[i]["text"])]
            if len(nearby) == 1:
                other_tables = [elements[i] for i in (index - 2, index + 2) if 0 <= i < len(elements)
                                and elements[i]["kind"] == "table" and same_scope(elements[i], element)]
                if not other_tables:
                    caption, caption_id = nearby[0]["text"], nearby[0]["element_id"]
        empty_table = (not any(cell.search_text for cell in cells) if cells else not table.find(["td", "th"]))
        relations[element["element_id"]] = {"caption": caption, "caption_id": caption_id, "empty_table": empty_table}
        if cells:
            tables[index] = {"element": element, "cells": cells, "caption": caption, "caption_id": caption_id}
    indices = list(tables)
    for left, right in pairwise(indices):
        previous, current = tables[left], tables[right]
        p, c = previous["element"], current["element"]
        between = elements[left + 1:right]
        if (not isinstance(p["page"], int) or c["page"] != p["page"] + 1
            or p["heading_path"] != c["heading_path"]
            or not CONTINUED.search(current["caption"])
            or any(item["kind"] not in {"caption", "header", "footer", "page_header", "page_footer", "page_number"}
                   for item in between)):
            continue
        old_number = TABLE_NUMBER.match(previous["caption"].strip())
        new_number = TABLE_NUMBER.match(current["caption"].strip())
        if not old_number or not new_number or old_number.group().casefold().replace(" ", "") != new_number.group().casefold().replace(" ", ""):
            continue
        old_width = max(cell.column + cell.columns for cell in previous["cells"])
        new_width = max(cell.column + cell.columns for cell in current["cells"])
        old_headers, new_headers = column_headers(previous["cells"]), column_headers(current["cells"])
        if old_width != new_width or not old_headers or (new_headers and header_signature(new_headers) != header_signature(old_headers)):
            continue
        relations[c["element_id"]].update({"inherited_headers": old_headers,
            "previous_table_id": p["element_id"], "previous_caption": previous["caption"],
            "previous_caption_id": previous["caption_id"]})
    return relations


def figure_relationships(elements):
    result = {}
    marker = re.compile(r"^[<\[(]?\s*(?:그림|도표|차트|figure|chart)(?=\s|\d|[.:])", re.IGNORECASE)
    for index, element in enumerate(elements):
        if element["kind"] not in {"figure", "chart", "image"}:
            continue
        candidates = [(i, elements[i]) for i in (index - 1, index + 1)
                      if 0 <= i < len(elements) and elements[i]["kind"] == "caption"
                      and same_scope(elements[i], element) and marker.match(elements[i]["text"])]
        if len(candidates) != 1:
            continue
        caption_index, caption = candidates[0]
        other = 2 * caption_index - index
        if 0 <= other < len(elements) and same_scope(elements[other], element) and elements[other]["kind"] in {"figure", "chart", "image"}:
            continue
        result[element["element_id"]] = {"caption": caption["text"], "caption_id": caption["element_id"]}
    return result
