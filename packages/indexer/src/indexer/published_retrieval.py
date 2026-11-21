"""게시된 청크를 검색과 대화가 공유하는 근거 계약으로 변환한다."""

import json
from contextlib import ExitStack, contextmanager, nullcontext

import httpx
from openpyxl.utils import get_column_letter
from qdrant_client.http.exceptions import ApiException

from indexer.contracts import IndexerError
from indexer.table_evidence import HTML_CELL_RANGE, html_table_evidence_urls
from indexer.visual_relations import xlsx_anchor_matches
from indexer.xlsx_access import CANDIDATE_LIMIT, open_workbook
from indexer.xlsx_query import execute, preview_mapping, unavailable
from indexer.xlsx_structure import confirmed


class PublishedRetrieval:
    def __init__(self, publisher, storage):
        self.publisher, self.storage = publisher, storage

    @contextmanager
    def _xlsx_snapshot(self, document_id, version):
        with ExitStack() as stack:
            try:
                manifest = self.publisher.get_version(version)
                if not manifest or document_id in self.publisher.deleted_documents():
                    raise unavailable()
                source = self.publisher.xlsx_source(document_id, manifest)
                if not source:
                    raise unavailable()
                job_id = source['job_id']
                workbook = stack.enter_context(open_workbook(self.storage, document_id, job_id))
            except (OSError, KeyError, ValueError) as error:
                raise unavailable() from error
            yield manifest['version'], job_id, workbook, source['regions']

    def xlsx_catalog(self, document_id, version=None):
        guard = getattr(self.publisher, 'search_lock', None)
        with guard if guard is not None else nullcontext(), self._xlsx_snapshot(document_id, version) as snapshot:
            pinned, job_id, workbook, _ = snapshot
            return {'index_version': pinned, 'job_id': job_id, 'tables': workbook.catalog()}

    def xlsx_join_candidates(self, document_id, version, sheet, start, right_sheet, right_start, offset):
        guard = getattr(self.publisher, 'search_lock', None)
        with guard if guard is not None else nullcontext(), self._xlsx_snapshot(document_id, version) as snapshot:
            pinned, job_id, workbook, _ = snapshot
            tables = {(r['sheet'], r['start']) for r in workbook.catalog()}
            if sheet == right_sheet or not {(sheet, start), (right_sheet, right_start)} <= tables:
                raise IndexerError('XLSX_QUERY_INVALID', '서로 다른 시트에서 확인된 표를 지정해 주세요.')
            candidates, total = workbook.candidate_page(sheet, start, right_sheet, right_start, offset, 100)
            return {'index_version': pinned, 'job_id': job_id, 'complete': total <= CANDIDATE_LIMIT,
                    'total': min(total, CANDIDATE_LIMIT), 'offset': offset, 'candidates': candidates}

    def xlsx_join_preview(self, document_id, version, plan):
        guard = getattr(self.publisher, 'search_lock', None)
        with guard if guard is not None else nullcontext(), self._xlsx_snapshot(document_id, version) as snapshot:
            pinned, job_id, workbook, _ = snapshot
            try:
                return {**preview_mapping(workbook, plan), 'index_version': pinned, 'job_id': job_id}
            except ValueError as error:
                raise IndexerError('XLSX_QUERY_INVALID', str(error)) from error

    def xlsx_query(self, document_id, version, plan):
        guard = getattr(self.publisher, 'search_lock', None)
        with guard if guard is not None else nullcontext(), self._xlsx_snapshot(document_id, version) as snapshot:
            pinned, job_id, workbook, regions = snapshot
            if not workbook.catalog():
                raise unavailable()
            try:
                result = execute(workbook, plan)
            except ValueError as error:
                raise IndexerError('XLSX_QUERY_INVALID', str(error)) from error
            sources = {(sheet, start): eid for sheet, start, eid in regions}
            scopes = [result['scope']]
            if result['join']:
                scopes.append(result['join']['right_scope'])
            refs = [{**scope, 'element_id': sources.get((scope['sheet'], scope['start']))} for scope in scopes]
            return {**result, 'document_id': document_id, 'job_id': job_id, 'index_version': pinned,
                    'element_id': refs[0]['element_id'], 'source_ranges': refs}

    @staticmethod
    def _expand_table(results, indexed):
        """Add bounded evidence from the same exact table, never an inferred continuation."""
        output = [dict(item) for item in results]
        seen = {item['chunk_id'] for item in output}
        anchor = next((item for item in output if item.get('kind') == 'table'
                       and len(item.get('element_ids', [])) == 1 and item.get('job_id')), None)
        origin_id = anchor['chunk_id'] if anchor else None
        if not anchor and output:
            hit = output[0]
            if hit.get('document_id') and hit.get('job_id') and hit.get('heading_element_ids') and hit.get('page') is not None:
                matches = [item for item in indexed.values() if item.get('kind') == 'table'
                           and item.get('document_id') == hit.get('document_id')
                           and item.get('job_id') == hit.get('job_id') and item.get('page') == hit.get('page')
                           and item.get('heading_path') == hit.get('heading_path')
                           and item.get('heading_element_ids') == hit.get('heading_element_ids')
                           and len(item.get('element_ids', [])) == 1]
                if len({tuple(item['element_ids']) for item in matches}) == 1:
                    matches.sort(key=lambda item: (item.get('part_number', 0), item['chunk_id']))
                    anchor = matches[0] if matches else None
                    origin_id = hit['chunk_id']
        if not anchor:
            return output
        candidates = [item for item in indexed.values()
                      if item.get('kind') == 'table' and item.get('document_id') == anchor.get('document_id')
                      and item.get('job_id') == anchor.get('job_id')
                      and item.get('element_ids') == anchor.get('element_ids')
                      and item.get('chunk_id') not in seen]
        candidates.sort(key=lambda item: (abs(item.get('part_number', 0) - anchor.get('part_number', 0)),
                                          item.get('part_number', 0), item['chunk_id']))
        budget = 9000
        added = 0
        for item in candidates:
            length = len(item.get('text', ''))
            if not length or length > budget:
                continue
            output.append({**item, 'expanded_from': origin_id, 'expansion_reason': 'same_source_table'})
            budget -= length
            added += 1
            if added >= 3:
                break
        complete = added == len(candidates)
        for item in output:
            if (item.get('kind') == 'table' and item.get('document_id') == anchor.get('document_id')
                and item.get('job_id') == anchor.get('job_id') and item.get('element_ids') == anchor.get('element_ids')):
                item['table_expansion_complete'] = complete
        return output

    def search(self, request):
        guard = getattr(self.publisher, "search_lock", None)
        with guard if guard is not None else nullcontext():
            return self._search(request)

    def _search(self, request):
        try:
            result = self.publisher.search(request.query, request.method, request.top_k,
                                           str(request.document_id) if request.document_id else None,
                                           request.rerank, str(request.index_version) if request.index_version else None)
            documents = {}
            elements_by_job = {}
            xlsx_relations_by_job = {}
            hits = []
            chunk_path = self.publisher.root / result["index_version"] / "chunks.json"
            indexed = {item["chunk_id"]: item for item in json.loads(chunk_path.read_text(encoding="utf-8"))}
            pending = self._expand_table(result["results"], indexed)
            seen_chunks = {item['chunk_id'] for item in pending}
            expanded_visual_count, expanded_visual_chars = 0, 0
            for chunk in pending:
                document_id = chunk["document_id"]
                if document_id not in documents:
                    path = self.storage.document_dir(document_id) / "document.json"
                    documents[document_id] = json.loads(path.read_text(encoding="utf-8"))
                job_id = chunk.get("job_id")
                page = chunk.get("page")
                before, after = None, None
                for key, direction in (("previous_chunk_id", "before"), ("next_chunk_id", "after")):
                    adjacent = indexed.get(chunk.get(key))
                    if (adjacent and adjacent.get("document_id") == document_id and adjacent.get("job_id") == job_id
                        and ((page is not None and adjacent.get("page") == page) or (page is None and chunk.get("source_scope") is not None and adjacent.get("source_scope") == chunk.get("source_scope")))
                        and adjacent.get("heading_path") == chunk.get("heading_path")
                        and adjacent.get("kind") == chunk.get("kind") == "text"):
                        if direction == "before":
                            before = adjacent
                        else:
                            after = adjacent
                evidence_ids = list(dict.fromkeys(chunk.get("element_ids", [])))
                related = [item for item in (before, chunk, after) if item]
                context_ids = list(dict.fromkeys(eid for item in related for eid in
                    [*item.get("heading_element_ids", []), *item.get("context_element_ids", []),
                     *(item.get("element_ids", []) if item is not chunk else [])] if eid not in evidence_ids))
                refs = []
                related_images = []
                if job_id and (evidence_ids or context_ids):
                    key = (document_id, job_id)
                    if key not in elements_by_job:
                        elements_by_job[key] = {item["element_id"]: item for item in
                            self.storage.load_job(document_id, job_id, "elements")}
                    source_elements = elements_by_job[key]
                    image_ids = set()
                    for eid in evidence_ids:
                        for link in source_elements.get(eid, {}).get('related_images', []):
                            image = source_elements.get(link.get('element_id'), {})
                            if (not image.get('asset_id') or image.get('element_id') in image_ids
                                or image.get('image_policy') == 'exclude'
                                or (image.get('image_policy') == 'analyze' and link.get('type') != 'image_analysis')):
                                continue
                            if link.get('type') == 'cell_anchor':
                                anchor = image.get('layout') or {}
                                cells = chunk.get('table_cells', [])
                                if not xlsx_anchor_matches(anchor, cells):
                                    continue
                            image_ids.add(image['element_id'])
                            related_images.append({**link, 'url': f'/v1/ingest/{document_id}/jobs/{job_id}/elements/{image["element_id"]}/image',
                                                   'is_preview': bool(image.get('image_is_preview')),
                                                   'location': image.get('office_location'), 'layout': image.get('layout')})
                            if link.get('type') != 'image_analysis' and expanded_visual_count < 2:
                                visual_ids = {item['element_id'] for item in source_elements.values()
                                              if item.get('source_origin') == 'visual_analysis'
                                              and item.get('visual_id') == image.get('visual_id') and item.get('visual_id')}
                                for candidate in indexed.values():
                                    if (candidate.get('document_id') != document_id or candidate.get('job_id') != job_id
                                        or candidate['chunk_id'] in seen_chunks or not visual_ids.intersection(candidate.get('element_ids', []))):
                                        continue
                                    length = len(candidate.get('text', ''))
                                    if not length or expanded_visual_chars + length > 4000:
                                        continue
                                    pending.append({**candidate, 'expanded_from': chunk['chunk_id'],
                                                    'expansion_reason': 'related_image_analysis',
                                                    'visual_relation': {key: link[key] for key in ('type', 'origin', 'status') if key in link}})
                                    seen_chunks.add(candidate['chunk_id'])
                                    expanded_visual_count += 1
                                    expanded_visual_chars += length
                                    if expanded_visual_count >= 2:
                                        break
                            if len(related_images) >= 4:
                                break
                        if len(related_images) >= 4:
                            break
                    for role, ids in (("evidence", evidence_ids), ("context", context_ids)):
                        for eid in ids:
                            source = elements_by_job[key].get(eid)
                            if source is None:
                                raise IndexerError("SEARCH_UNAVAILABLE", "검색 결과의 원문 근거를 확인할 수 없습니다.", 409)
                            xlsx_region = source.get('xlsx_region')
                            location = source.get("office_location")
                            if xlsx_region and location and not confirmed(xlsx_region):
                                location = {**location, 'status': 'unverified'}
                            if location:
                                ref = {**location, "role": role}
                                exact = next((item['office_location'] for item in chunk.get('element_refs', [])
                                              if item['element_id'] == eid and (item.get('office_location') or {}).get('cells')), None)
                                if role == 'evidence' and exact:
                                    ref = {**exact, 'status': location.get('status', 'unverified'), 'role': role}
                                    for address in chunk.get('xlsx_context_cells', []):
                                        if address not in exact['cells']:
                                            context_ref = {'kind': 'xlsx_cell', 'sheet': exact['sheet'], 'cell': address,
                                                           'role': 'context', 'origin': exact.get('origin'),
                                                           'status': exact.get('status', 'unverified')}
                                            if context_ref not in refs:
                                                refs.append(context_ref)
                                elif role == 'evidence' and location.get('kind') == 'xlsx_range' and chunk.get('table_cells'):
                                    cells = chunk['table_cells']
                                    ref['start'] = f"{get_column_letter(min(c['column'] for c in cells))}{min(c['row'] for c in cells)}"
                                    ref['end'] = f"{get_column_letter(max(c['column'] for c in cells))}{max(c['row'] for c in cells)}"
                                    ref['status'] = 'unverified'
                                if ref not in refs:
                                    refs.append(ref)
                            source_page = source.get("page")
                            if source_page:
                                ref = {"page": source_page, "role": role,
                                       "kind": "pdf_page" if documents[document_id]["format"] == "pdf" else "parser_page"}
                                if ref not in refs:
                                    refs.append(ref)
                if not refs and page:
                    refs.append({"page": page, "role": "evidence", "kind": "pdf_page" if documents[document_id]["format"] == "pdf" else "parser_page"})
                urls = [f"/v1/ingest/{document_id}/jobs/{job_id}/elements/{element_id}"
                        for element_id in evidence_ids] if job_id else []
                context_urls = [f"/v1/ingest/{document_id}/jobs/{job_id}/elements/{element_id}"
                                for element_id in context_ids] if job_id else []
                if chunk.get('xlsx_region_start') and urls:
                    cells = list(dict.fromkeys(cell['address'] for cell in chunk['table_cells']))
                    base_url = urls[0]
                    urls = [base_url + '?cells=' + ','.join(cells[start:start + 100])
                            for start in range(0, len(cells), 100)]
                    xlsx_context = [address for address in chunk.get('xlsx_context_cells', []) if address not in cells]
                    context_urls.extend(base_url + '?cells=' + ','.join(xlsx_context[start:start + 100])
                                        for start in range(0, len(xlsx_context), 100))
                if chunk.get("source_range_quality") == HTML_CELL_RANGE and len(urls) == 1:
                    urls = html_table_evidence_urls(urls[0], chunk["source_segments"])
                before_text = (before.get("body_text") or before["text"])[-600:] if before else ""
                after_text = (after.get("body_text") or after["text"])[:600] if after else ""
                context_text = "\n".join(filter(None, [chunk.get("heading_text"), before_text,
                    chunk.get("body_text") or chunk["text"], after_text])) if before or after else chunk["text"]
                row_relations = []
                if chunk.get('xlsx_region_start'):
                    if key not in xlsx_relations_by_job:
                        try:
                            raw = self.storage.load_job(document_id, job_id, 'parser_result')
                            xlsx_relations_by_job[key] = raw.get('xlsx_row_relations', [])
                        except FileNotFoundError:
                            xlsx_relations_by_job[key] = []
                    rows = {cell['row'] for cell in chunk['table_cells']}
                    row_relations = [link for link in xlsx_relations_by_job[key] if any(
                        point['sheet'] == chunk['xlsx_sheet'] and point['region_start'] == chunk['xlsx_region_start']
                        and point['row'] in rows for point in (link['left'], link['right']))]
                hits.append({**chunk, "unit_id": chunk["chunk_id"], "filename": documents[document_id]["filename"],
                             "search_text": chunk["text"], "context_text": context_text,
                             **({"quality": "unverified"} if chunk.get('xlsx_region_start') and any(
                                 ref.get('role') == 'evidence' and ref.get('status') == 'unverified' for ref in refs) else {}),
                             "context_before": before_text, "context_after": after_text,
                             "context_element_ids": context_ids,
                             "related_images": related_images,
                             **({"xlsx_row_relations": row_relations} if row_relations else {}),
                             "source_refs": refs, "evidence_urls": urls, "context_urls": context_urls})
            return {**result, "results": hits}
        except ApiException as error:
            raise IndexerError("VECTOR_DB_UNAVAILABLE", "벡터 데이터베이스 연결 또는 설정을 확인해 주세요.", 503) from error
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            message = ("검색 서비스 인증 정보를 확인해 주세요." if code in {401, 403} else
                       "검색 서비스 요청 한도를 확인한 뒤 다시 시도해 주세요." if code == 429 else
                       "검색 서비스가 요청을 처리하지 못했습니다. 연결과 모델 설정을 확인해 주세요.")
            raise IndexerError("SEARCH_PROVIDER_ERROR", message, 503) from error
        except httpx.RequestError as error:
            raise IndexerError("SEARCH_PROVIDER_UNAVAILABLE", "검색 서비스에 연결하지 못했습니다. 연결 설정을 확인해 주세요.", 503) from error
        except (TypeError, AttributeError) as error:
            raise IndexerError("SEARCH_PROVIDER_ERROR", "검색 서비스 응답을 확인할 수 없습니다. 연결과 모델 설정을 확인해 주세요.", 503) from error
        except ValueError as error:
            raise IndexerError("SEARCH_UNAVAILABLE", str(error), 409) from error
        except (FileNotFoundError, KeyError) as error:
            raise IndexerError("SEARCH_UNAVAILABLE", "검색 색인 또는 근거를 확인할 수 없습니다.", 409) from error
