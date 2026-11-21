"""원본 요소 참조를 보존하는 초기 청크 표현."""

import re
from collections import defaultdict
from html import unescape
from itertools import groupby, pairwise
from uuid import NAMESPACE_URL, uuid5

from bs4 import BeautifulSoup
from openpyxl.utils.cell import coordinate_to_tuple

from indexer.element_scope import same_scope, source_scope
from indexer.embedding_input import MAX_INPUT_BYTES, byte_size, context_prefix, embedding_text, split_text
from indexer.heading_hierarchy import apply_flat_hierarchy
from indexer.inspection import is_page_label
from indexer.office_locations import set_xlsx_chunk_evidence
from indexer.parsing.normalization import PAGE_FURNITURE, cells_from, list_items_from
from indexer.representation import table_groups, table_parts
from indexer.structure_relations import figure_relationships, table_relationships
from indexer.xlsx_structure import confirmed


def normalize_elements(raw_elements: list[dict]) -> list[dict]:
    elements = []
    headings: list[tuple[int, str, str]] = []
    for order, raw in enumerate(raw_elements):
        if not isinstance(raw, dict) or not isinstance(raw.get("content"), dict):
            raise TypeError("파싱 요소 형식이 잘못되었습니다")
        content = raw["content"]
        html = content.get("html") or ""
        soup = BeautifulSoup(html, "html.parser")
        for node in soup.find_all(["script", "style", "iframe", "object"]):
            node.decompose()
        text = " ".join(soup.stripped_strings) or unescape(content.get("text") or "").strip()
        items = list_items_from(soup)
        if items:
            outside = " ".join(str(word).strip() for word in soup.strings if not word.find_parent("li") and str(word).strip())
            text = (soup.get_text("\n", strip=True) if outside else
                    "\n".join(" / ".join([*item["ancestors"], item["text"]]) for item in items))
        kind = raw.get("category") if isinstance(raw.get("category"), str) else "other"
        if kind in {"figure", "chart", "image"}:
            alternatives = [str(image.get("alt", "")).strip() for image in soup.find_all("img")]
            provider_text = unescape(content.get("text") or "").strip()
            for extra in [provider_text, *alternatives]:
                if extra and extra not in text:
                    text = "\n".join(filter(None, (text, extra)))
        heading = soup.find(re.compile(r"^h[1-6]$"))
        if kind.startswith("heading") or heading:
            subtype = raw.get("sub_category")
            level = (int(subtype[1]) if isinstance(subtype, str) and re.fullmatch(r"h[1-6]", subtype)
                     else int(heading.name[1]) if heading
                     else int(kind[-1]) if re.fullmatch(r"heading[1-6]", kind) else 1)
            headings = [item for item in headings if item[0] < level]
            if text:
                headings.append((level, text, f"e{order + 1}"))
        page = raw.get("page")
        elements.append({"element_id": f"e{order + 1}", "provider_id": raw.get("id"),
                         "kind": "heading" if kind.startswith("heading") or heading else kind,
                         "text": text, "html": html, "heading_path": [item[1] for item in headings],
                         "heading_element_ids": [item[2] for item in headings],
                         "page": page if type(page) is int and page > 0 else None,
                         "coordinates": raw.get("coordinates"), "order": order,
                         **({'source_origin': raw['_origin']} if raw.get('_origin') else {}),
                         **({'parser_label': raw['_source_label']} if raw.get('_source_label') else {}),
                         **({'heading_level_explicit': True} if raw.get('_heading_explicit') else {}),
                         **({'hwp_style': raw['_hwp_style']} if raw.get('_hwp_style') else {}),
                         **({'asset_id': raw['_asset_id'], 'image_format': raw.get('_image_format')} if raw.get('_asset_id') else {}),
                         **({'image_is_preview': True} if raw.get('_image_is_preview') else {}),
                         **({'visual_id': raw['_visual_id']} if raw.get('_visual_id') else {}),
                         **({'image_policy': raw['_image_policy']} if raw.get('_image_policy') else {}),
                         **({'previous_element_id': raw['_merge_from']} if raw.get('_merge_from') else {}),
                         **({'search_exclusion': 'image_policy'} if raw.get('_image_suppressed') else {}),
                         **({'layout': raw['_layout']} if raw.get('_layout') else {}),
                         **({'xlsx_region': raw['_xlsx_region']} if '_xlsx_region' in raw else {}),
                         **({'container_id': raw['_container']} if raw.get('_container') else {}),
                         **({"coordinate_space": {"provider": raw["_provider"], "width": raw.get("_page_width"),
                                                   "height": raw.get("_page_height"), "unit": "pixels"}}
                            if raw.get("_provider") == "paddle_vl" else {}),
                         **({"heading_level": level, "heading_level_origin": raw.get('_heading_origin', 'parser')} if kind.startswith("heading") or heading else {})})
    # Preserve explicit multi-level parser output. Flat headings need conservative context.
    if not any(item.get("heading_level", 1) > 1 or item.get('heading_level_explicit') for item in elements):
        apply_flat_hierarchy(elements)
    inferred = {item["element_id"] for item in elements if item.get("heading_level_origin") == "inferred"}
    for item in elements:
        item["heading_path_origin"] = "inferred" if inferred.intersection(item.get("heading_element_ids", [])) else "parser"
    return elements


def make_chunks(document_id: str, job_id: str, elements: list[dict], target_chars: int = 1200,
                overlap_chars: int = 120, profile: str = "general", max_bytes: int = MAX_INPUT_BYTES) -> list[dict]:
    if not 400 <= target_chars <= 4000 or not 0 <= overlap_chars <= min(400, target_chars // 4):
        raise ValueError("청크 크기 또는 겹침을 확인해 주세요")
    if max_bytes < 256:
        raise ValueError("임베딩 입력 바이트 제한을 확인해 주세요")
    chunks = []
    pending = []
    linked_titles = {}
    title_context = set()
    for index, current in enumerate(elements):
        title = current.get('xlsx_region')
        if not title or title['kind'] != 'title' or not confirmed(title):
            continue
        _, left = coordinate_to_tuple(title['start'])
        bottom, right = coordinate_to_tuple(title['end'])
        matches = []
        for distance, following in enumerate(elements[index + 1:index + 5], 1):
            body = following.get('xlsx_region')
            if not body or body['sheet'] != title['sheet'] or body['kind'] == 'title':
                break
            row, start_col = coordinate_to_tuple(body['start'])
            _, end_col = coordinate_to_tuple(body['end'])
            if row > bottom + 5:
                break
            if not (bottom < row and max(left, start_col) <= min(right, end_col)):
                continue
            if (body['kind'] == 'text' and len(body['cells']) == 1
                    and len(body['cells'][0]['text']) <= 240 and distance <= 2):
                continue
            if not body['title_text'] and any(cell['text'].strip() for cell in body['cells']):
                matches.append((following['element_id'], distance == 1 and row <= bottom + 2))
        for element_id, direct in matches:
            linked_titles[element_id] = (current, direct and len(matches) == 1)
        if len(matches) == 1 and matches[0][1]:
            title_context.add(current['element_id'])
    relationships = table_relationships(elements)
    figures = figure_relationships(elements)
    furniture_pages = defaultdict(set)
    for element in elements:
        if element["kind"] in PAGE_FURNITURE and element.get("page") is not None:
            furniture_pages[(element["kind"], embedding_text(element["text"]))].add(element["page"])

    def append_chunk(text, refs, part_number, kind, heading_path, table_extra=None, body_prefix=None):
        text = embedding_text(text)
        first = refs[0]
        heading_text = context_prefix(" > ".join(heading_path), min(600, target_chars // 2, max_bytes // 3))
        actual_prefix = heading_text if body_prefix is None else body_prefix
        body_text = text.removeprefix(actual_prefix + "\n") if actual_prefix else text
        chunk_id = str(uuid5(NAMESPACE_URL, f"ingestion:{job_id}:{first['element_id']}:{part_number}"))
        chunk = {"chunk_id": chunk_id, "document_id": document_id, "job_id": job_id,
                 "representation_version": "structure-v11", "text": text,
                 "element_ids": [item["element_id"] for item in refs], "page": first["page"], "source_scope": source_scope(first),
                 "group_scope": first.get('container_id') if profile == 'slides' else None,
                 "heading_element_ids": first.get("heading_element_ids", []),
                 "heading_text": heading_text, "body_text": body_text,
                 "coordinates": first["coordinates"], "part_number": part_number,
                 "first_order": first["order"], "last_order": refs[-1]["order"],
                 "element_refs": [{"element_id": item["element_id"], "page": item["page"],
                                   "coordinates": item["coordinates"], "coordinate_space": item.get("coordinate_space"),
                                   "source_origin": item.get('source_origin'),
                                   "office_location": item.get("office_location")} for item in refs],
                 "kind": kind, "heading_path": heading_path, "heading_path_origin": first.get("heading_path_origin", "parser"),
                 "quality": "unverified" if kind in {"table", "figure", "chart"} else "extracted",
                 "source_segments": [{"element_id": item["element_id"],
                                      "text": body_text if len(refs) == 1 and kind == "text" else embedding_text(item["text"]),
                                      "order": item["order"]} for item in refs]}
        if table_extra:
            chunk.update(table_extra)
        chunks.append(chunk)

    def flush():
        if not pending:
            return
        first = pending[0]
        heading = context_prefix(" > ".join(first["heading_path"]), min(600, target_chars // 2, max_bytes // 3))
        text = embedding_text("\n".join(filter(None, [heading, *(item["text"] for item in pending)])))
        append_chunk(text, list(pending), 0, "text", first["heading_path"])
        pending.clear()

    for index, element in enumerate(elements):
        text = embedding_text(element["text"])
        if element.get('search_exclusion') == 'image_policy':
            flush()
            continue
        if (element["kind"] in PAGE_FURNITURE
            and (len(furniture_pages[(element["kind"], text)]) > 1
                 or (element["kind"] == "page_number" and is_page_label(text)))):
            element["search_exclusion"] = "repeated_page_furniture"
            flush()
            continue
        if not text and not element.get("xlsx_region"):
            flush()
            continue
        heading = context_prefix(" > ".join(element["heading_path"]), min(600, target_chars // 2, max_bytes // 3))
        budget = max(1, target_chars - len(heading) - 1)
        table = BeautifulSoup(element["html"], "html.parser").find("table")
        kind = element["kind"]
        if kind == "heading":
            flush()
            following = elements[index + 1] if index + 1 < len(elements) else None
            if following and following['kind'] == 'heading' and element['element_id'] in following.get('heading_element_ids', []):
                continue
            if (following and following["kind"] != "heading" and following["heading_path"] == element["heading_path"]
                and (following["text"] or any(cell['text'].strip() for cell in
                                             (following.get("xlsx_region") or {}).get('cells', []))) and not (
                    following["kind"] in PAGE_FURNITURE and (
                        len(furniture_pages[(following["kind"], embedding_text(following["text"]))]) > 1
                        or (following["kind"] == "page_number" and is_page_label(following["text"]))))):
                continue
            append_chunk(heading or text, [element], 0, "heading", element["heading_path"])
            continue
        if region := element.get('xlsx_region'):
            flush()
            if element['element_id'] in title_context:
                continue
            linked_title, direct_title = linked_titles.get(element['element_id'], (None, False))
            cells = {item['address']: item for item in region['cells']}
            header_keys = region['headers'] if confirmed(region) else []
            labels = {cells[key]['column']: cells[key]['text'] for key in header_keys}
            header_row = max((cells[key]['row'] for key in header_keys), default=0)
            title = region['title_text'] if confirmed(region) else None
            if linked_title:
                title = linked_title['xlsx_region']['title_text']
            prefix = context_prefix(' > '.join(dict.fromkeys(filter(None, [heading, title]))),
                                    min(800, target_chars // 2, max_bytes // 3))
            parts, row_labels = [], {}
            for item in sorted(cells.values(), key=lambda cell: (cell['row'], cell['column'])):
                if not item['text'].strip():
                    continue
                label = ''
                if item['row'] > header_row and not item['hidden_merged']:
                    label = labels.get(item['column'], '')
                    if (region['kind'] == 'key_value' and confirmed(region)
                            and item['address'] != region['title']):
                        label = row_labels.get(item['row'], '')
                        row_labels.setdefault(item['row'], item['text'])
                parts.append({**item, 'label': label, 'label_origin': region['origin'] if label else 'source'})
            rows = (list(row) for _, row in groupby(parts, key=lambda item: item['row'])) if profile == 'spreadsheet' else [parts]
            groups = (group for row in rows for group in table_groups(prefix, row, target_chars, max_bytes))
            for number, group in enumerate(groups):
                segments = [{**part, 'value': part['text'],
                             'text': ' / '.join(filter(None, [part['label'], part['text']]))} for part in group]
                kind = region['kind'] if region['kind'] in {'title', 'code', 'key_value'} else 'table'
                append_chunk('\n'.join(filter(None, [prefix, *(item['text'] for item in segments)])),
                             [element], number, kind, element['heading_path'], {
                                 'representation_version': 'xlsx-structure-v2', 'xlsx_region_start': region['start'],
                                 'xlsx_region_kind': region['kind'], 'xlsx_sheet': region['sheet'],
                                 'xlsx_context_cells': [key for key in [region['title'] if title else None, *header_keys] if key],
                                 'context_element_ids': [linked_title['element_id']] if linked_title else [],
                                 'relations': [{'type': 'xlsx_title', 'origin': 'inferred',
                                                'status': 'unverified', 'element_id': linked_title['element_id'],
                                                'adjacent': direct_title}] if linked_title else [],
                                 'quality': 'extracted' if confirmed(region) else 'unverified',
                             }, body_prefix=prefix)
                set_xlsx_chunk_evidence(chunks[-1], segments)
            continue
        if table:
            flush()
            cells = cells_from(table) if not table.find("table") else []
            if cells:
                relation = relationships.get(element["element_id"], {})
                parts, uncertain = table_parts(cells, relation.get("inherited_headers", ()),
                                               infer_first_row=(element.get('coordinate_space') or {}).get('provider') == 'paddle_vl'
                                               or element.get('source_origin') == 'native_extraction')
                caption = relation.get("caption", "")
                inherited_caption = relation.get("previous_caption", "")
                if not parts and caption:
                    for part_number, part in enumerate(split_text(caption, chars=budget,
                            max_bytes=max_bytes - byte_size(heading) - 1, overlap=overlap_chars)):
                        append_chunk("\n".join(filter(None, (heading, part))), [element], part_number,
                                     "table", element["heading_path"], {"table_cells": [], "table_segments": [], "quality": "unverified"})
                    continue
                table_heading = context_prefix(" > ".join(filter(None, [heading, inherited_caption, caption])), min(800, target_chars // 2, max_bytes // 3))
                context_ids = list(dict.fromkeys(value for value in (relation.get("caption_id"), relation.get("previous_table_id"), relation.get("previous_caption_id")) if value))
                represented = {(part["row"], part["column"]) for part in parts}
                for cell in cells:
                    if cell.text and (cell.row + 1, cell.column + 1) not in represented:
                        parts.append({"text": cell.text, "label": "", "row": cell.row + 1, "column": cell.column + 1})
                        uncertain = True
                layout = element.get('layout') or {}
                if layout.get('space') == 'xlsx_region':
                    for part in parts:
                        part['row'] += layout.get('row_offset', 0)
                        part['column'] += layout.get('column_offset', 0)
                    uncertain = True
                parts.sort(key=lambda item: (item["row"], item["column"]))
                table_parts_by_policy = (list(row) for _, row in groupby(parts, key=lambda part: part["row"])) if profile == "spreadsheet" else [parts]
                groups = (group for row in table_parts_by_policy for group in table_groups(table_heading, row, target_chars, max_bytes))
                for part_number, group in enumerate(groups):
                    lines = [" / ".join(filter(None, (part.get("label", ""), part["text"]))) for part in group]
                    append_chunk("\n".join(filter(None, [table_heading, *lines])), [element], part_number,
                                 "table", element["heading_path"],
                                 {"context_element_ids": context_ids,
                                  "relations": ([{"type": "table_continuation", "origin": "inferred", "status": "unverified", "element_id": relation["previous_table_id"]}] if relation.get("previous_table_id") else []) + ([{"type": "caption", "origin": "inferred", "status": "unverified", "element_id": relation["caption_id"]}] if relation.get("caption_id") else []),
                                  "table_cells": [{"row": part["row"], "column": part["column"]} for part in group],
                                  "table_segments": [{"row": part["row"], "column": part["column"], "text": line,
                                                      "label": part.get("label", ""), "value": part["text"],
                                                      "label_origin": part.get("label_origin", "parser")}
                                                     for part, line in zip(group, lines, strict=True)],
                                  "quality": "unverified" if uncertain or context_ids else "extracted"}, body_prefix=table_heading)
                    if layout.get('space') == 'xlsx_region':
                        # Retain only this chunk's cells instead of copying the
                        # entire region into every source segment.
                        chunks[-1]['source_segments'][0]['text'] = '\n'.join(part['text'] for part in group)
                        chunks[-1]['source_range_quality'] = 'unverified'
                continue
        if kind in {"figure", "chart", "image"} and element["element_id"] in figures:
            flush()
            relation = figures[element["element_id"]]
            prefix = context_prefix(" > ".join(filter(None, [heading, relation["caption"]])), min(800, target_chars // 2, max_bytes // 3))
            for part_number, part in enumerate(split_text(text, chars=max(1, target_chars - len(prefix) - 1),
                                                           max_bytes=max_bytes - byte_size(prefix) - 1,
                                                           overlap=overlap_chars)):
                append_chunk("\n".join(filter(None, [prefix, part])), [element], part_number, "figure", element["heading_path"],
                             {"context_element_ids": [relation["caption_id"]], "relations": [{"type": "caption",
                              "origin": "inferred", "status": "unverified", "element_id": relation["caption_id"]}],
                              "quality": "unverified"}, body_prefix=prefix)
            continue
        can_group = (kind in {"paragraph", "text", "list", "list_item", "other"} and source_scope(element) is not None
                     and profile != "spreadsheet" and (profile != 'slides' or bool(element.get('container_id')))
                     and not (profile == "manual" and kind in {"list", "list_item"}))
        candidate = "\n".join(filter(None, [heading, *(item["text"] for item in pending), text]))
        compatible = not pending or (same_scope(pending[0], element) and pending[0]["heading_path"] == element["heading_path"]
                                     and (profile != 'slides' or pending[0].get('container_id') == element.get('container_id')))
        if can_group and compatible and len(candidate) <= target_chars and byte_size(candidate) <= max_bytes:
            pending.append(element)
            continue
        flush()
        if can_group and len(text) <= budget and byte_size("\n".join(filter(None, [heading, text]))) <= max_bytes:
            pending.append(element)
            continue
        groups = split_text(text, chars=budget, max_bytes=max_bytes - byte_size(heading) - 1,
                            overlap=overlap_chars)
        for part_number, part in enumerate(groups):
            append_chunk("\n".join(filter(None, (heading, part))), [element], part_number,
                         kind if kind in {"figure", "chart", "table"} else "text", element["heading_path"])
    flush()
    captions = {element["element_id"]: embedding_text(element["text"]) for element in elements
                if element["kind"] == "caption" and element["text"]}
    included_captions = {relation["element_id"] for chunk in chunks for relation in chunk.get("relations", [])
                         if relation.get("type") == "caption" and captions.get(relation["element_id"])
                         and captions[relation["element_id"]] in chunk["text"]}
    chunks = [chunk for chunk in chunks if not (chunk["kind"] == "text" and len(chunk["element_ids"]) == 1
              and chunk["element_ids"][0] in included_captions)]
    for element in elements:
        if element["element_id"] in included_captions:
            element["search_exclusion"] = "included_as_caption_context"
    for previous, current in pairwise(chunks):
        if (previous["kind"] == current["kind"] == "text" and previous.get("source_scope") is not None
            and previous.get("source_scope") == current.get("source_scope") and previous["heading_path"] == current["heading_path"]
            and previous.get('group_scope') == current.get('group_scope')
            and 0 <= current["first_order"] - previous["last_order"] <= 1):
            previous["next_chunk_id"] = current["chunk_id"]
            current["previous_chunk_id"] = previous["chunk_id"]
    return chunks
