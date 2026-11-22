"""Split oversized retrieval chunks while retaining their original evidence."""

from itertools import pairwise
from uuid import NAMESPACE_URL, uuid5

import httpx

from indexer.contracts import IndexerError
from indexer.embedding_input import byte_size, embedding_text, heading_prefix, split_text
from indexer.office_locations import set_xlsx_chunk_evidence
from indexer.representation import table_groups
from indexer.table_evidence import HTML_CELL_RANGE, set_html_table_evidence


def input_limit_error(error):
    if not isinstance(error, httpx.HTTPStatusError):
        return False
    response = error.response
    if response.status_code == 413:
        return True
    if response.status_code not in {400, 422}:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    info = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(info, dict):
        return False
    code = str(info.get("code") or info.get("type") or "").lower()
    message = str(info.get("message") or "").lower()
    return code in {"context_length_exceeded", "input_too_long", "max_tokens_exceeded"} or any(
        phrase in message for phrase in ("maximum context length", "maximum input length", "too many tokens", "input is too long"))


def split_chunk(chunk, max_bytes):
    text = chunk["text"]
    segments = chunk.get("table_segments") or []
    # Table rows carry their source cell locations in each fragment.
    if segments:
        body = "\n".join(segment['text'] for segment in segments)
        original_prefix = text.removesuffix(body).rstrip()
        prefix = heading_prefix(original_prefix.split(' > '), max_bytes // 3)
        source_parts = [{**segment, "row": segment["row"], "column": segment["column"],
                         "label": segment.get("label", ""), "text": segment.get("value", segment["text"]),
                         "label_origin": segment.get("label_origin", "parser")}
                        for segment in segments]
        if chunk.get("source_range_quality") == HTML_CELL_RANGE:
            for part, source in zip(source_parts, chunk["source_segments"], strict=True):
                part.update(source_text=source["text"], source_start=source["start"])
        parts = []
        for group in table_groups(prefix, source_parts, max_bytes, max_bytes):
            converted = [{**item, "row": item["row"], "column": item["column"], "label": item["label"],
                          "value": item["text"], "label_origin": item.get("label_origin", "parser"),
                          "text": " / ".join(filter(None, [item["label"], item["text"]]))}
                         for item in group]
            parts.append(("\n".join(filter(None, [prefix, *(item["text"] for item in converted)])), converted))
    else:
        body = embedding_text(chunk.get("body_text") or text)
        # Figure captions can be part of the actual prefix even when they are
        # absent from heading_text. Preserve them when embedding resplits it.
        prefix = (text.removesuffix(body).rstrip() if chunk.get('source_range_quality') == 'normalized_element_text'
                  and text.endswith(body) else chunk.get('heading_text') or '')
        heading = heading_prefix(prefix.split(' > '), max_bytes // 3)
        available = max_bytes - byte_size(heading) - (1 if heading else 0)
        parts = [("\n".join(filter(None, [heading, value])), None) for value in
                 split_text(body, chars=max(1, len(body)), max_bytes=available)]
    if len(parts) == 1 and parts[0][0] == text:
        raise ValueError("임베딩 입력을 더 작게 나눌 수 없습니다")
    result = []
    search_cursor = 0
    for index, (value, part_segments) in enumerate(parts):
        item = {**chunk, "chunk_id": str(uuid5(NAMESPACE_URL, f"{chunk['chunk_id']}:embedding-split:{max_bytes}:{index}")),
                "text": value, "body_text": value.removeprefix((chunk.get("heading_text") or "") + "\n"),
                "embedding_split_from": chunk["chunk_id"]}
        item.pop("previous_chunk_id", None)
        item.pop("next_chunk_id", None)
        if part_segments is not None:
            item["table_segments"] = [{key: val for key, val in part.items()
                                       if key not in {"source_text", "source_start", "source_end"}}
                                      for part in part_segments]
            item["table_cells"] = [{"row": part["row"], "column": part["column"]} for part in part_segments]
            item['body_text'] = "\n".join(part['text'] for part in part_segments)
            if chunk.get('xlsx_region_start'):
                set_xlsx_chunk_evidence(item, part_segments)
            elif chunk.get('source_range_quality') == HTML_CELL_RANGE:
                set_html_table_evidence(item, part_segments)
            elif chunk.get('source_range_quality') == 'unverified':
                item['source_segments'] = [{**chunk['source_segments'][0],
                                            'text': "\n".join(part['value'] for part in part_segments)}]
        else:
            item["heading_text"] = heading
            body_fragment = value.removeprefix(heading + "\n") if heading else value
            item["body_text"] = body_fragment
            sources = chunk.get("source_segments") or []
            original_body = embedding_text(chunk.get("body_text") or text)
            start = original_body.find(body_fragment, search_cursor)
            if start >= 0:
                search_cursor = start + len(body_fragment)
            if (start >= 0 and len(sources) == 1 and chunk.get('source_range_quality') == 'normalized_element_text'
                    and embedding_text(sources[0]['text']) == original_body):
                source_start = sources[0]['start'] + start
                item['source_segments'] = [{**sources[0], 'text': body_fragment, 'start': source_start,
                                            'end': source_start + len(body_fragment)}]
                item['source_range_quality'] = 'normalized_element_text'
            elif start >= 0 and sources:
                offsets, cursor = [], 0
                for source in sources:
                    source_text = embedding_text(source["text"])
                    pos = original_body.find(source_text, cursor)
                    if pos < 0:
                        offsets = []
                        break
                    offsets.append((pos, pos + len(source_text), source))
                    cursor = pos + len(source_text)
                matched = [{**source, "text": original_body[max(lo, start):min(hi, start + len(body_fragment))]}
                           for lo, hi, source in offsets if lo < start + len(body_fragment) and hi > start]
                if matched:
                    ids = {source["element_id"] for source in matched}
                    item["element_ids"] = [eid for eid in chunk["element_ids"] if eid in ids]
                    item["element_refs"] = [ref for ref in chunk.get("element_refs", []) if ref["element_id"] in ids]
                    item["source_segments"] = matched
                    orders = [source["order"] for source in matched if "order" in source]
                    if orders:
                        item["first_order"], item["last_order"] = min(orders), max(orders)
                    item["source_range_quality"] = "element"
                else:
                    item["source_range_quality"] = "unverified"
            else:
                item["source_range_quality"] = "unverified"
            if item["source_range_quality"] == "unverified":
                item["quality"] = "unverified"
        result.append(item)
    return result


def embed_chunks(cache, chunks, max_bytes, batch_size, progress=None, *, tracked_document=None, detail_progress=None):
    def embed_batch(batch):
        try:
            return batch, cache.embed([item["text"] for item in batch])
        except httpx.HTTPStatusError as error:
            if not input_limit_error(error):
                raise
            if len(batch) > 1:
                middle = len(batch) // 2
                left, lv = embed_batch(batch[:middle])
                right, rv = embed_batch(batch[middle:])
                return left + right, lv + rv
            if byte_size(batch[0]["text"]) <= 32:
                raise IndexerError("EMBED_INPUT_LIMIT", "임베딩 모델의 입력 길이 제한을 확인해 주세요.", 422) from error
            smaller = max(16, byte_size(batch[0]["text"]) // 2)
            try:
                replacement = split_chunk(batch[0], smaller)
            except ValueError as split_error:
                raise IndexerError("EMBED_INPUT_LIMIT", "표 또는 본문의 문맥을 보존하며 더 작게 나눌 수 없습니다.", 422) from split_error
            output, vectors = [], []
            for offset in range(0, len(replacement), batch_size):
                group, values = embed_batch(replacement[offset:offset + batch_size])
                output.extend(group)
                vectors.extend(values)
            return output, vectors

    pending = []
    for chunk in chunks:
        if byte_size(chunk["text"]) > max_bytes:
            pending.extend(split_chunk(chunk, max_bytes))
        else:
            pending.append(chunk)
    output, vectors = [], []
    total = len(pending)
    doc_total = sum(item.get("document_id") == tracked_document for item in pending)
    doc_done = 0
    if progress and not detail_progress:
        progress(0, total)
    if detail_progress:
        detail_progress(0, doc_total, 0, total)
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        group, values = embed_batch(batch)
        total += len(group) - len(batch)
        doc_total += sum(item.get("document_id") == tracked_document for item in group) - sum(item.get("document_id") == tracked_document for item in batch)
        doc_done += sum(item.get("document_id") == tracked_document for item in group)
        output.extend(group)
        vectors.extend(values)
        if progress and not detail_progress:
            progress(len(output), total)
        if detail_progress:
            detail_progress(doc_done, doc_total, len(output), total)
    for chunk in output:
        chunk.pop("previous_chunk_id", None)
        chunk.pop("next_chunk_id", None)
    for previous, current in pairwise(output):
        if (previous.get("kind") == current.get("kind") == "text"
            and previous.get("document_id") == current.get("document_id")
            and previous.get("job_id") == current.get("job_id")
            and (previous.get("source_scope") or (f"page:{previous['page']}" if previous.get("page") is not None else None)) is not None
            and (previous.get("source_scope") or f"page:{previous.get('page')}") == (current.get("source_scope") or f"page:{current.get('page')}")
            and previous.get('group_scope') == current.get('group_scope')
            and previous.get("heading_path") == current.get("heading_path")
            and 0 <= current.get("first_order", 0) - previous.get("last_order", 0) <= 1):
            previous["next_chunk_id"] = current["chunk_id"]
            current["previous_chunk_id"] = previous["chunk_id"]
    return output, vectors

