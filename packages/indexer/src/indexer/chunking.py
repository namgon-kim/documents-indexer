"""원본 요소 참조를 보존하는 초기 청크 표현."""

import re
from collections import defaultdict
from html import unescape
from itertools import groupby, pairwise
from uuid import NAMESPACE_URL, uuid5

from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

from indexer.element_scope import same_scope, source_scope
from indexer.embedding_input import (
    MAX_INPUT_BYTES,
    byte_size,
    embedding_text,
    heading_prefix,
    split_text,
    split_text_ranges,
)
from indexer.figure_text import figure_search_text
from indexer.heading_hierarchy import (
    apply_flat_hierarchy,
    promote_chapter_headers,
)
from indexer.inspection import is_page_label
from indexer.office_locations import set_xlsx_chunk_evidence
from indexer.parsing.normalization import (
    PAGE_FURNITURE,
    cells_from,
    clean,
    list_items_from,
    parse_table_html,
    readable,
)
from indexer.representation import table_groups, table_parts
from indexer.structure_relations import figure_relationships, table_relationships
from indexer.table_evidence import set_html_table_evidence
from indexer.xlsx_structure import confirmed

TABLE_REPRESENTATION = "table-structure-v17"

def normalize_elements(raw_elements: list[dict], *, source_format: str | None = None) -> list[dict]:
    elements = []
    headings: list[tuple[int, str, str]] = []
    for order, raw in enumerate(raw_elements):
        if not isinstance(raw, dict) or not isinstance(raw.get("content"), dict):
            raise TypeError("파싱 요소 형식이 잘못되었습니다")
        content = raw["content"]
        html = content.get("html") or ""
        soup = parse_table_html(html)
        for node in soup.find_all(["script", "style", "iframe", "object"]):
            node.decompose()
        has_table = soup.find("table") is not None
        text = readable(soup) if has_table else " ".join(soup.stripped_strings)
        if not text.strip():
            text = unescape(content.get("text") or "").strip()
        items = [] if has_table else list_items_from(soup)
        if items:
            outside = " ".join(str(word).strip() for word in soup.strings if not word.find_parent("li") and str(word).strip())
            text = (soup.get_text("\n", strip=True) if outside else
                    "\n".join(" / ".join([*item["ancestors"], item["text"]]) for item in items))
        kind = raw.get("category") if isinstance(raw.get("category"), str) else "other"
        if kind in {"figure", "chart", "image"}:
            text = figure_search_text(soup, content.get("text"))
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
    promoted = promote_chapter_headers(elements) if source_format == 'pdf' else False
    if not any(item.get("heading_level", 1) > 1 or item.get('heading_level_explicit') for item in elements):
        apply_flat_hierarchy(elements)
    elif promoted:
        apply_flat_hierarchy(elements, preserve_explicit=True)
    inferred = {item["element_id"] for item in elements if item.get("heading_level_origin") == "inferred"}
    for item in elements:
        item["heading_path_origin"] = "inferred" if inferred.intersection(item.get("heading_element_ids", [])) else "parser"
    return elements


def _span(cell):
    if cell.get('merged_range'):
        return range_boundaries(cell['merged_range'])
    return cell['column'], cell['row'], cell['column'], cell['row']


def _header_paths(header_keys, cells):
    """Map each column to its header cells from top to bottom, using merged spans."""
    paths = defaultdict(list)
    bottom = 0
    for key in sorted(header_keys, key=lambda key: (_span(cells[key])[1], cells[key]['column'])):
        left, _, right, last = _span(cells[key])
        bottom = max(bottom, last)
        for col in range(left, right + 1):
            paths[col].append(cells[key])
    return paths, bottom


def _row_groups(cells, header_row):
    """Rows covered by a single-column merge in the data area, mapped to its anchor."""
    groups = defaultdict(list)
    for cell in cells.values():
        if cell['hidden_merged'] or not cell.get('merged_range') or cell['row'] <= header_row:
            continue
        left, top, right, bottom = range_boundaries(cell['merged_range'])
        if left == right and bottom > top and cell['text'].strip():
            for row in range(top + 1, bottom + 1):
                groups[row].append(cell)
    return groups


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
        if not title or title['kind'] != 'title':
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
        # An inferred title is search context for the body but keeps its own chunk.
        if len(matches) == 1 and matches[0][1] and confirmed(title):
            title_context.add(current['element_id'])
    relationships = table_relationships(elements)
    caption_tables = defaultdict(list)
    for element in elements:
        relation = relationships.get(element["element_id"], {})
        if relation.get("empty_table") and relation.get("caption_id") and not element.get("xlsx_region"):
            caption_tables[relation["caption_id"]].append(element["element_id"])
    figures = figure_relationships(elements)
    furniture_pages = defaultdict(set)
    for element in elements:
        if element["kind"] in PAGE_FURNITURE and element.get("page") is not None:
            furniture_pages[(element["kind"], embedding_text(element["text"]))].add(element["page"])

    def furniture_reason(element):
        if element['kind'] not in PAGE_FURNITURE:
            return None
        text = embedding_text(element['text'])
        if (is_page_label(text) and (element['kind'] == 'page_number'
                or (element['kind'] in {'footer', 'page_footer'} and element.get('page') is not None))):
            return 'page_label'
        if len(furniture_pages[(element['kind'], text)]) > 1:
            return 'repeated_page_furniture'
        return None

    def append_chunk(text, refs, part_number, kind, heading_path, table_extra=None, body_prefix=None):
        text = embedding_text(text)
        first = refs[0]
        heading_text = heading_prefix(heading_path, min(600, target_chars // 2, max_bytes // 3))
        actual_prefix = heading_text if body_prefix is None else body_prefix
        body_text = text.removeprefix(actual_prefix + "\n") if actual_prefix else text
        chunk_id = str(uuid5(NAMESPACE_URL, f"ingestion:{job_id}:{first['element_id']}:{part_number}"))
        chunk = {"chunk_id": chunk_id, "document_id": document_id, "job_id": job_id,
                 "representation_version": "structure-v13", "text": text,
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
                 "source_segments": [] if table_extra and table_extra.get("table_segments") else [
                     {"element_id": item["element_id"],
                      "text": (item["text"] if kind == "table" else
                               body_text if len(refs) == 1 and kind == "text" else embedding_text(item["text"])),
                      "order": item["order"]} for item in refs]}
        if table_extra:
            chunk.update(table_extra)
        chunks.append(chunk)

    def flush():
        if not pending:
            return
        first = pending[0]
        heading = heading_prefix(first["heading_path"], min(600, target_chars // 2, max_bytes // 3))
        text = embedding_text("\n".join(filter(None, [heading, *(item["text"] for item in pending)])))
        append_chunk(text, list(pending), 0, "text", first["heading_path"])
        pending.clear()

    for index, element in enumerate(elements):
        text = embedding_text(element["text"])
        if element.get('search_exclusion') == 'image_policy':
            flush()
            continue
        if reason := furniture_reason(element):
            element["search_exclusion"] = reason
            flush()
            continue
        if not text and not element.get("xlsx_region"):
            flush()
            continue
        heading = heading_prefix(element["heading_path"], min(600, target_chars // 2, max_bytes // 3))
        budget = max(1, target_chars - len(heading) - 1)
        table = parse_table_html(element["html"]).find("table")
        kind = element["kind"]
        if kind == "heading":
            flush()
            following = elements[index + 1] if index + 1 < len(elements) else None
            if following and following['kind'] == 'heading' and element['element_id'] in following.get('heading_element_ids', []):
                continue
            if (following and following["kind"] != "heading" and following["heading_path"] == element["heading_path"]
                and (following["text"] or any(cell['text'].strip() for cell in
                                             (following.get("xlsx_region") or {}).get('cells', [])))
                and not furniture_reason(following)):
                continue
            append_chunk(heading or text, [element], 0, "heading", element["heading_path"])
            continue
        if region := element.get('xlsx_region'):
            flush()
            if element['element_id'] in title_context:
                continue
            linked_title, direct_title = linked_titles.get(element['element_id'], (None, False))
            cells = {item['address']: item for item in region['cells']}
            # Inferred headers and titles come from source cells, so they serve as
            # search context. Their relations stay unverified until the structure is
            # confirmed, and only confirmed tables are offered to typed queries.
            context_status = 'verified' if confirmed(region) else 'unverified'
            header_keys = [key for key in region['headers'] if key in cells]
            paths, header_row = _header_paths(header_keys, cells)
            row_groups = _row_groups(cells, header_row)
            title = region['title_text']
            if linked_title:
                title = linked_title['xlsx_region']['title_text']
            prefix = heading_prefix(list(dict.fromkeys([*element['heading_path'], title])),
                                    min(800, target_chars // 2, max_bytes // 3))
            parts, row_labels, ambiguous_pairs = [], {}, False
            if region['kind'] == 'key_value' and confirmed(region):
                ordered = sorted((cell for cell in cells.values() if cell['address'] != region['title']),
                                 key=lambda cell: (cell['row'], cell['column']))
                for _, row in groupby(ordered, key=lambda cell: cell['row']):
                    members = list(row)
                    # Gaps and merges make positional pairing ambiguous. Never
                    # shift the remaining values across a missing cell.
                    for _, run in groupby(enumerate(members), key=lambda pair: pair[1]['column'] - pair[0]):
                        contiguous = [cell for _, cell in run]
                        if len(contiguous) % 2:
                            ambiguous_pairs = True
                            continue
                        for index in range(0, len(contiguous), 2):
                            label, value = contiguous[index:index + 2]
                            if (not label['hidden_merged'] and not value['hidden_merged']
                                    and not label.get('merged_range') and not value.get('merged_range')
                                    and label['text'].strip()):
                                row_labels[value['address']] = label
                            else:
                                ambiguous_pairs = True
            grouped_rows = set()
            for item in sorted(cells.values(), key=lambda cell: (cell['row'], cell['column'])):
                if not item['text'].strip():
                    continue
                sources, kind = [], 'column'
                if item['row'] > header_row and not item['hidden_merged'] and item['address'] != region['title']:
                    sources = paths.get(item['column'], [])
                    if region['kind'] == 'key_value' and confirmed(region):
                        sources, kind = ([row_labels[item['address']]] if item['address'] in row_labels else []), 'row'
                relations = [{'kind': kind, 'text': source['text'], 'element_id': element['element_id'],
                              'sheet': region['sheet'], 'address': source['address'], 'origin': region['origin'],
                              'status': 'unverified' if kind == 'row' else context_status} for source in sources]
                label = ' > '.join(source['text'] for source in sources)
                groups = [] if item['row'] in grouped_rows else [
                    group for group in row_groups.get(item['row'], []) if group['column'] != item['column']]
                if groups:
                    # A merged group label belongs to the whole row; add it once.
                    grouped_rows.add(item['row'])
                    context = [' '.join(filter(None, [' > '.join(s['text'] for s in paths.get(group['column'], [])),
                                                      group['text']])) for group in groups]
                    label = ' / '.join(filter(None, [*context, label]))
                    relations = [{'kind': 'group', 'text': group['text'], 'element_id': element['element_id'],
                                  'sheet': region['sheet'], 'address': group['address'], 'origin': region['origin'],
                                  'status': context_status} for group in groups] + relations
                parts.append({**item, 'label': label, 'label_origin': region['origin'] if label else 'source',
                              'label_relations': relations})
            rows = (list(row) for _, row in groupby(parts, key=lambda item: item['row'])) if profile == 'spreadsheet' else [parts]
            groups = (group for row in rows for group in table_groups(prefix, row, target_chars, max_bytes))
            for number, group in enumerate(groups):
                segments = [{**part, 'value': part['text'],
                             'text': ' / '.join(filter(None, [part['label'], part['text']]))} for part in group]
                kind = region['kind'] if region['kind'] in {'title', 'code', 'key_value'} else 'table'
                append_chunk('\n'.join(filter(None, [prefix, *(item['text'] for item in segments)])),
                             [element], number, kind, element['heading_path'], {
                                 'representation_version': 'xlsx-structure-v7', 'xlsx_region_start': region['start'],
                                 'xlsx_context_status': context_status,
                                 'xlsx_region_kind': region['kind'], 'xlsx_sheet': region['sheet'],
                                 'xlsx_context_cells': list(dict.fromkeys(key for key in [region['title'] if title else None, *header_keys,
                                     *(rel['address'] for part in group for rel in part['label_relations'])] if key)),
                                 'context_element_ids': [linked_title['element_id']] if linked_title else [],
                                 'relations': [{'type': 'xlsx_title', 'origin': 'inferred',
                                                'status': 'unverified', 'element_id': linked_title['element_id'],
                                                'adjacent': direct_title}] if linked_title else [],
                                 'quality': ('unverified' if not confirmed(region) or ambiguous_pairs or any(
                                     rel['status'] == 'unverified' for part in group for rel in part['label_relations'])
                                             else 'extracted'),
                             }, body_prefix=prefix)
                set_xlsx_chunk_evidence(chunks[-1], segments)
            continue
        if table:
            flush()
            relation = relationships.get(element["element_id"], {})
            external_caption_only = relation.get("empty_table") and relation.get("caption_id")
            if external_caption_only:
                # Some parsers also put the adjacent caption in the empty table's text fallback.
                if clean(text) == clean(embedding_text(relation["caption"])):
                    continue
                cells = []
            else:
                cells = cells_from(table) if not table.find("table") else []
            if cells:
                parts, uncertain = table_parts(cells, relation.get("inherited_headers", ()),
                                               infer_first_row=(element.get('coordinate_space') or {}).get('provider') == 'paddle_vl'
                                               or element.get('source_origin') == 'native_extraction')
                caption = relation.get("caption", "")
                inherited_caption = relation.get("previous_caption", "")
                if not any(cell.search_text for cell in cells) and caption:
                    for part_number, part in enumerate(split_text(caption, chars=budget,
                            max_bytes=max_bytes - byte_size(heading) - 1, overlap=overlap_chars)):
                        append_chunk("\n".join(filter(None, (heading, part))), [element], part_number,
                                     "table", element["heading_path"], {"table_cells": [], "table_segments": [], "quality": "unverified",
                                                                       "representation_version": TABLE_REPRESENTATION})
                        chunks[-1]["source_segments"][0]["text"] = part
                    continue
                table_heading = heading_prefix([*element["heading_path"], inherited_caption, caption], min(800, target_chars // 2, max_bytes // 3))
                context_ids = list(dict.fromkeys(value for value in (relation.get("caption_id"), relation.get("previous_table_id"), relation.get("previous_caption_id")) if value))
                represented = {(part["row"], part["column"]) for part in parts}
                for cell in cells:
                    if cell.search_text and (cell.row + 1, cell.column + 1) not in represented:
                        parts.append({"text": cell.search_text, "label": "", "row": cell.row + 1, "column": cell.column + 1})
                        if not cell.header:
                            uncertain = True
                layout = element.get('layout') or {}
                legacy_xlsx = layout.get('space') == 'xlsx_region'
                if not legacy_xlsx:
                    source_cells = {(cell.row + 1, cell.column + 1): cell.text for cell in cells}
                    for part in parts:
                        part["source_text"] = source_cells[part["row"], part["column"]]
                if legacy_xlsx:
                    for part in parts:
                        part['row'] += layout.get('row_offset', 0)
                        part['column'] += layout.get('column_offset', 0)
                        for rel in part.get('label_relations', []):
                            if not rel.get('inherited'):
                                rel['row'] += layout.get('row_offset', 0)
                                rel['column'] += layout.get('column_offset', 0)
                    uncertain = True
                for part in parts:
                    for rel in part.get('label_relations', []):
                        inherited = rel.pop('inherited', False)
                        rel['element_id'] = relation.get('previous_table_id') if inherited else element['element_id']
                        if legacy_xlsx and inherited:
                            source = next((item for item in elements if item['element_id'] == rel['element_id']), {})
                            source_layout = source.get('layout') or {}
                            if source_layout.get('space') == 'xlsx_region':
                                rel['row'] += source_layout.get('row_offset', 0)
                                rel['column'] += source_layout.get('column_offset', 0)
                parts.sort(key=lambda item: (item["row"], item["column"]))
                table_parts_by_policy = (list(row) for _, row in groupby(parts, key=lambda part: part["row"])) if profile == "spreadsheet" else [parts]
                groups = (group for row in table_parts_by_policy for group in table_groups(table_heading, row, target_chars, max_bytes))
                for part_number, group in enumerate(groups):
                    lines = [" / ".join(filter(None, (part.get("label", ""), part["text"]))) for part in group]
                    append_chunk("\n".join(filter(None, [table_heading, *lines])), [element], part_number,
                                 "table", element["heading_path"],
                                 {"representation_version": TABLE_REPRESENTATION, "context_element_ids": context_ids,
                                  "relations": ([{"type": "table_continuation", "origin": "inferred", "status": "unverified", "element_id": relation["previous_table_id"]}] if relation.get("previous_table_id") else []) + ([{"type": "caption", "origin": "inferred", "status": "unverified", "element_id": relation["caption_id"]}] if relation.get("caption_id") else []),
                                  "table_cells": [{"row": part["row"], "column": part["column"]} for part in group],
                                  "table_segments": [{"row": part["row"], "column": part["column"], "text": line,
                                                      "label": part.get("label", ""), "value": part["text"],
                                                      "label_origin": part.get("label_origin", "parser"),
                                                      "label_relations": part.get("label_relations", [])}
                                                     for part, line in zip(group, lines, strict=True)],
                                  "quality": "unverified" if uncertain or context_ids else "extracted"}, body_prefix=table_heading)
                    if legacy_xlsx:
                        # Retain only this chunk's cells instead of copying the
                        # entire region into every source segment.
                        chunks[-1]['source_segments'] = [{"element_id": element["element_id"], "order": element["order"],
                                                          "text": "\n".join(part["text"] for part in group)}]
                        chunks[-1]['source_range_quality'] = 'unverified'
                    else:
                        set_html_table_evidence(chunks[-1], group)
                continue
            text = embedding_text(clean(text))
        if kind in {"figure", "chart", "image"} and element["element_id"] in figures:
            flush()
            relation = figures[element["element_id"]]
            prefix = heading_prefix([*element["heading_path"], relation["caption"]], min(800, target_chars // 2, max_bytes // 3))
            for part_number, (part, start, end) in enumerate(split_text_ranges(
                    text, chars=max(1, target_chars - len(prefix) - 1),
                    max_bytes=max_bytes - byte_size(prefix) - 1, overlap=overlap_chars)):
                append_chunk("\n".join(filter(None, [prefix, part])), [element], part_number, "figure", element["heading_path"],
                             {"context_element_ids": [relation["caption_id"]], "relations": [{"type": "caption",
                              "origin": "inferred", "status": "unverified", "element_id": relation["caption_id"]}],
                              "quality": "unverified", "representation_version": "figure-structure-v4"}, body_prefix=prefix)
                chunks[-1]['source_segments'] = [{'element_id': element['element_id'], 'order': element['order'],
                                                  'text': part, 'start': start, 'end': end}]
                chunks[-1]['source_range_quality'] = 'normalized_element_text'
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
        groups = split_text_ranges(text, chars=budget, max_bytes=max_bytes - byte_size(heading) - 1,
                                   overlap=overlap_chars)
        extra = {"representation_version": TABLE_REPRESENTATION} if table else None
        if tables := caption_tables.get(element["element_id"]):
            extra = {"context_element_ids": tables,
                     "relations": [{"type": "caption_of", "origin": "inferred", "status": "unverified",
                                    "element_id": table_id} for table_id in tables]}
        for part_number, (part, start, end) in enumerate(groups):
            append_chunk("\n".join(filter(None, (heading, part))), [element], part_number,
                         kind if kind in {"figure", "chart", "table"} else "text", element["heading_path"], extra)
            if kind in {'figure', 'chart', 'image'}:
                chunks[-1]['representation_version'] = 'figure-structure-v4'
                chunks[-1]['source_segments'] = [{'element_id': element['element_id'], 'order': element['order'],
                                                  'text': part, 'start': start, 'end': end}]
                chunks[-1]['source_range_quality'] = 'normalized_element_text'
    flush()
    captions = {element["element_id"]: embedding_text(element["text"]) for element in elements
                if element["kind"] == "caption" and element["text"]}
    included_captions = {relation["element_id"] for chunk in chunks for relation in chunk.get("relations", [])
                         if relation.get("type") == "caption" and captions.get(relation["element_id"])
                         and captions[relation["element_id"]] in chunk["text"]}
    included_captions.difference_update(caption_tables)
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
