"""Opt-in request-local diagnostic metadata; never record query or document text."""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

_current = ContextVar('rag_diagnostic_trace', default=None)
_private = ContextVar('rag_diagnostic_private', default=None)


@contextmanager
def capture_trace():
    trace = {'trace_id': str(uuid4()), 'stages': {}}
    token = _current.set(trace)
    private_token = _private.set({})
    try:
        yield trace
    finally:
        _private.reset(private_token)
        _current.reset(token)


def active():
    return _current.get() is not None


def record(stage, value):
    trace = _current.get()
    if trace is not None:
        trace['stages'][stage] = value


def set_model_input(context):
    private = _private.get()
    if private is not None:
        private['model_input'] = context


def set_answer(answer, sources):
    """Keep text request-local, while publishing only sentence IDs and citation IDs."""
    import re

    from indexer.citations import CITATION, parse_citation

    private = _private.get()
    if private is None:
        return
    sentences = [part.strip() for part in re.split(
        r'\n+|(?<=[.!?。])\s+(?!\[)|(?<=\])\s+(?=[가-힣A-Za-z])', answer) if part.strip()]
    private['answer_text'] = answer
    private['answer_sentences'] = sentences[:500]
    metadata = []
    for index, sentence in enumerate(sentences[:500], 1):
        parsed = [parse_citation(match.group(1)) for match in CITATION.finditer(sentence)]
        numbers = list(dict.fromkeys(number for item in parsed if item is not None for number in item))
        valid = [number for number in numbers if 1 <= number <= len(sources)]
        metadata.append({'sentence_id': index, 'citation_ids': valid,
                         'chunk_ids': [sources[number - 1]['chunk_id'] for number in valid
                                       if sources[number - 1].get('chunk_id')],
                         'invalid_citation_ids': [number for number in numbers if number not in valid],
                         **({'invalid_citation_format': True} if any(item is None for item in parsed) else {})})
    record('answer_sentences', {'items': metadata, 'truncated': len(sentences) > 500})


def ranking(stage, hits):
    if active():
        record(stage, [{'chunk_id': hit.chunk_id, 'rank': index, 'score': hit.score}
                       for index, hit in enumerate(hits, 1)])


def chunks(stage, items):
    if active():
        import re
        record(stage, [{'chunk_id': item['chunk_id'], 'document_id': item.get('document_id'),
                        'job_id': item.get('job_id'), 'representation_version': item.get('representation_version'),
                        'element_ids': [value for value in item.get('element_ids', [])
                                        if isinstance(value, str) and re.fullmatch(r'e[0-9]+', value)],
                        'part_number': item.get('part_number'), 'page': item.get('page'),
                        'expanded_from': item.get('expanded_from'), 'expansion_reason': item.get('expansion_reason'),
                        'table_expansion_complete': item.get('table_expansion_complete')}
                       for item in items])


def filter_rankings(allowed):
    trace = _current.get()
    if trace is None:
        return
    for stage, entries in trace['stages'].items():
        if isinstance(entries, list) and entries and all(isinstance(entry, dict) and 'chunk_id' in entry for entry in entries):
            trace['stages'][stage] = [entry for entry in entries if entry['chunk_id'] in allowed]


def artifact_summary(storage, publisher, document_id, job_id, version):
    """Compare available saved artifacts without returning their contents or content hashes."""
    import json

    if document_id in publisher.deleted_documents():
        return {'status': 'document_unavailable'}
    data, availability = {}, {}
    for name in ('parser_initial_elements', 'parser_elements', 'elements', 'chunks', 'processing'):
        try:
            data[name] = storage.load_job(document_id, job_id, name)
            availability[name] = 'available'
        except (OSError, ValueError):
            availability[name] = 'unavailable'
    summary = {'status': 'available' if any(value == 'available' for value in availability.values())
               else 'job_artifacts_unavailable', 'document_id': document_id, 'job_id': job_id,
               'index_version': version, 'artifacts': availability}
    for name in ('parser_initial_elements', 'parser_elements', 'elements', 'chunks'):
        value = data.get(name)
        if isinstance(value, list):
            summary[name] = {'count': len(value)}
        elif name in data:
            summary['artifacts'][name] = 'invalid'
    initial, final = data.get('parser_initial_elements'), data.get('parser_elements')
    if isinstance(initial, list) and isinstance(final, list):
        common = min(len(initial), len(final))
        summary['parser_comparison'] = {'changed_positions': sum(initial[i] != final[i] for i in range(common)),
                                        'initial_extra': len(initial) - common, 'final_extra': len(final) - common,
                                        'comparison': 'position_based'}
    normalized = data.get('elements')
    if isinstance(final, list) and isinstance(normalized, list):
        common = min(len(final), len(normalized))
        pairs = [(final[i], normalized[i]) for i in range(common)
                 if isinstance(final[i], dict) and isinstance(normalized[i], dict)]
        summary['normalization_comparison'] = {
            'comparison': 'position_based', 'compared': len(pairs),
            'parser_extra': len(final) - common, 'normalized_extra': len(normalized) - common,
            'text_changed_count': sum((raw.get('content') or {}).get('text', '') != item.get('text', '')
                                      for raw, item in pairs if isinstance(raw.get('content'), dict)),
            'html_changed_count': sum((raw.get('content') or {}).get('html', '') != item.get('html', '')
                                      for raw, item in pairs if isinstance(raw.get('content'), dict)),
        }
    processing = data.get('processing')
    if isinstance(processing, dict):
        # Only fixed fields whose values are generated system metadata; never parser config or secrets.
        known = {'mode': {'initial', 'reparse', 'rechunk', 'complete_images'},
                 'provider': {'upstage', 'paddle_vl', 'native_text', 'native_xlsx', 'native_pptx', 'native_hwp'},
                 'profile': {'general', 'report', 'manual', 'spreadsheet', 'slides', 'scanned'}}
        summary['processing'] = {key: value if value in known[key] else 'other'
                                 for key in known if isinstance((value := processing.get(key)), str)}
        settings = processing.get('settings')
        if isinstance(settings, dict):
            safe = {key: settings[key] for key in ('dpi', 'target_chars', 'overlap_chars')
                    if type(settings.get(key)) is int}
            for key, allowed in {'ocr_policy': {'automatic', 'force'},
                                 'extraction_mode': {'parser', 'native_text', 'native_visual'}}.items():
                if isinstance(settings.get(key), str) and settings[key] in allowed:
                    safe[key] = settings[key]
            summary['processing']['settings'] = safe
    try:
        manifest = publisher.get_version(version)
        if not manifest:
            raise FileNotFoundError
        if document_id not in manifest.get('document_ids', []):
            summary['published_status'] = 'document_not_in_snapshot'
            return summary
        path = publisher.root / manifest['version'] / 'chunks.json'
        published = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(published, list):
            raise TypeError
        document_chunks = [item for item in published if item.get('document_id') == document_id]
        selected = [item for item in document_chunks if item.get('job_id') == job_id]
        if not selected:
            if not document_chunks:
                summary['published_status'] = 'document_chunks_missing'
            elif any(not item.get('job_id') for item in document_chunks):
                summary['published_status'] = 'job_membership_unknown'
            else:
                summary['published_status'] = 'job_not_in_snapshot'
            return summary
        summary['published_status'] = 'available'
        summary['published_chunks'] = {'count': len(selected), 'representation_versions': sorted({
            item.get('representation_version', 'unknown') for item in selected})}
        job_chunks = data.get('chunks')
        if isinstance(job_chunks, list):
            job_ids = {item['chunk_id'] for item in job_chunks if 'chunk_id' in item}
            pub_ids = {item['chunk_id'] for item in selected if 'chunk_id' in item}
            job_map = {item['chunk_id']: item for item in job_chunks if 'chunk_id' in item}
            pub_map = {item['chunk_id']: item for item in selected if 'chunk_id' in item}
            changed = {key for key in job_ids & pub_ids if job_map[key] != pub_map[key]}
            summary['chunk_comparison'] = {'job_only_count': len(job_ids - pub_ids),
                                           'published_only_count': len(pub_ids - job_ids),
                                           'shared_count': len(job_ids & pub_ids), 'shared_changed_count': len(changed),
                                           'shared_changed_ids': sorted(changed)[:1000],
                                           'job_only_ids': sorted(job_ids - pub_ids)[:1000],
                                           'published_only_ids': sorted(pub_ids - job_ids)[:1000],
                                           'truncated': len(job_ids - pub_ids) > 1000 or len(pub_ids - job_ids) > 1000 or len(changed) > 1000}
    except (OSError, ValueError, KeyError, TypeError):
        summary['published_status'] = 'published_snapshot_unavailable'
    return summary


def probe_results(probes, result, trace, publisher, *, chat):
    """Report literal matches, never returning expected terms or evidence text."""
    import json
    import re

    from indexer.embedding_input import embedding_text

    try:
        path = publisher.root / result['index_version'] / 'chunks.json'
        deleted = publisher.deleted_documents()
        indexed = {item['chunk_id']: item for item in json.loads(path.read_text(encoding='utf-8'))
                   if item.get('document_id') not in deleted}
        snapshot_status = 'available'
    except (OSError, KeyError, TypeError, ValueError):
        indexed = {}
        snapshot_status = 'published_snapshot_unavailable'
    hits = result.get('sources' if chat else 'results', [])
    private = _private.get() or {}
    model_input = private.get('model_input') if chat else None
    sentence_texts = private.get('answer_sentences', []) if chat else []
    answer = embedding_text(private.get('answer_text', result.get('answer', ''))) if chat else None

    def match_terms(terms, line):
        line = embedding_text(line)
        return all((bool(re.search(r'(?<![\d.,+\-])' + re.escape(term) + r'(?![\d.,])', line))
                    if re.fullmatch(r'[+-]?\d+(?:[.,]\d+)*', term) else term in line) for term in terms)

    def match(probe, hit, *, returned):
        if str(probe.document_id) != hit.get('document_id'):
            return False
        if returned:
            if not any(ref.get('page') == probe.page and ref.get('role', 'evidence') == 'evidence'
                       for ref in hit.get('source_refs', [])):
                return False
        elif hit.get('page') != probe.page:
            return False
        terms = [embedding_text(term) for term in probe.contains_line]
        if probe.row is None and probe.column is None:
            return not terms or any(match_terms(terms, line) for line in hit.get('search_text', hit.get('text', '')).splitlines())
        return any((probe.row is None or segment.get('row') == probe.row)
                   and (probe.column is None or segment.get('column') == probe.column)
                   and (not returned or not hit.get('evidence_truncated'))
                   and match_terms(terms, segment.get('text', '')) for segment in hit.get('table_segments', []))

    def leaves(value):
        if isinstance(value, dict):
            for member in value.values():
                yield from leaves(member)
        elif isinstance(value, list):
            for member in value:
                yield from leaves(member)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            yield str(value)

    def cell_text_status(probe, source, item):
        """Confirm a cell only when its unique rendered segment is present in model text."""
        if str(probe.document_id) != source.get('document_id') or probe.page not in item.get('evidence_pages', []):
            return 'not_applicable'
        segments = source.get('table_segments', [])
        terms = [embedding_text(term) for term in probe.contains_line]
        candidates = [segment for segment in segments
                      if (probe.row is None or segment.get('row') == probe.row)
                      and (probe.column is None or segment.get('column') == probe.column)
                      and match_terms(terms, segment.get('text', ''))]
        if not candidates:
            return 'not_applicable'
        segment_texts = [embedding_text(segment.get('text', '')) for segment in segments]
        expected_body = embedding_text('\n'.join(segment.get('text', '') for segment in segments))
        stored_body = embedding_text(source.get('body_text') or '')
        actual_text = embedding_text(str(item.get('text', '')))
        # Require a consistent full table body, so a heading or duplicate value cannot
        # masquerade as the requested cell. Older or partially sent chunks stay unknown.
        if (not expected_body or stored_body != expected_body
                or not (actual_text == expected_body or actual_text.endswith('\n' + expected_body))):
            return 'unknown'
        model_lines = [embedding_text(line) for line in actual_text.splitlines()]
        for candidate in candidates:
            rendered = embedding_text(candidate.get('text', ''))
            if rendered and segment_texts.count(rendered) == 1 and model_lines.count(rendered) == 1:
                return 'confirmed'
        return 'unknown'

    def model_matches(probe, terms):
        if model_input is None:
            return []
        matches = []
        for source, item in zip(hits, model_input, strict=True):
            if str(probe.document_id) != source.get('document_id') or not source.get('chunk_id'):
                continue
            fields = []
            if probe.row is not None or probe.column is not None:
                if cell_text_status(probe, source, item) == 'confirmed':
                    fields.append('text')
            else:
                evidence_pages = item.get('evidence_pages', [])
                all_pages = {*evidence_pages, *item.get('context_pages', [])}
                model_text = str(item.get('text', ''))
                if probe.page in evidence_pages and model_text.strip() and (not terms or any(
                        match_terms(terms, line) for line in model_text.splitlines())):
                    fields.append('text')
                if terms and probe.page in all_pages and any(
                        match_terms(terms, line) for line in str(item.get('context', '')).splitlines()):
                    fields.append('context')
                metadata = {key: value for key, value in item.items() if key not in {'text', 'context'}}
                if terms and probe.page in all_pages and any(match_terms(terms, value) for value in leaves(metadata)):
                    fields.append('metadata')
            if fields:
                matches.append({'citation_id': item['id'], 'chunk_id': source['chunk_id'], 'fields': fields})
        return matches

    def expansion_reasons(matched_ids):
        expansion = trace['stages'].get('table_expansion_decisions')
        if not expansion:
            return []
        reasons = {item['chunk_id']: item['reason'] for item in expansion.get('candidates', [])}
        anchors = [indexed[key] for key in expansion.get('anchor_chunk_ids', []) if key in indexed]
        output = []
        for chunk_id in matched_ids[:1000]:
            chunk = indexed[chunk_id]
            if chunk_id in reasons:
                reason = reasons[chunk_id]
            elif expansion.get('truncated'):
                reason = 'candidate_not_enumerated'
            elif not anchors:
                reason = ('anchor_not_in_snapshot' if expansion.get('anchor_chunk_ids')
                          else expansion.get('anchor_status', 'no_table_anchor'))
            elif chunk.get('kind') != 'table':
                reason = 'not_table'
            elif any(anchor.get('document_id') == chunk.get('document_id')
                     and anchor.get('job_id') == chunk.get('job_id')
                     and anchor.get('element_ids') != chunk.get('element_ids') for anchor in anchors):
                reason = 'different_source_element'
            else:
                reason = 'not_same_table_or_context'
            output.append({'chunk_id': chunk_id, 'reason': reason})
        return output

    output = []
    for number, probe in enumerate(probes, 1):
        terms = [embedding_text(term) for term in probe.contains_line]
        matching = [hit['chunk_id'] for hit in hits if hit.get('chunk_id') and match(probe, hit, returned=True)]
        model_fields = model_matches(probe, terms) if chat else []
        cell_unknown = []
        if chat and model_input is not None and (probe.row is not None or probe.column is not None):
            for source, item in zip(hits, model_input, strict=True):
                if not source.get('chunk_id') or str(probe.document_id) != source.get('document_id'):
                    continue
                if probe.page not in {*item.get('evidence_pages', []), *item.get('context_pages', [])}:
                    continue
                status = cell_text_status(probe, source, item)
                metadata = {key: value for key, value in item.items() if key not in {'text', 'context'}}
                possible_values = [*str(item.get('text', '')).splitlines(),
                                   *str(item.get('context', '')).splitlines(), *leaves(metadata)]
                unlinked = status == 'not_applicable' and (
                    not terms or any(match_terms(terms, value) for value in possible_values))
                if status == 'unknown' or unlinked:
                    cell_unknown.append(source['chunk_id'])
        if chat:
            # For chat, report only actual captured model text; do not infer it from source metadata.
            matching = [entry['chunk_id'] for entry in model_fields if 'text' in entry['fields']]
        uncertain = [hit['chunk_id'] for hit in hits if hit.get('chunk_id') in indexed
                     and hit.get('evidence_truncated') and (probe.row is not None or probe.column is not None)
                     and match(probe, indexed[hit['chunk_id']], returned=False)]
        stages = {}
        for stage, entries in trace['stages'].items():
            if not isinstance(entries, list):
                continue
            ids = [entry.get('chunk_id') for entry in entries if isinstance(entry, dict) and entry.get('chunk_id')
                   and (stage != 'model_evidence' or entry.get('citation_id') is not None)]
            if ids:
                if stage == 'model_evidence':
                    if model_input is not None:
                        stages[stage] = [entry['chunk_id'] for entry in model_fields]
                else:
                    stages[stage] = [chunk_id for chunk_id in ids if chunk_id in indexed and match(probe, indexed[chunk_id], returned=False)]
        published_matches = [key for key, item in indexed.items() if match(probe, item, returned=False)]
        output.append({'probe': number, 'returned_chunk_ids': matching,
                       'truncated_cell_match_unverified_chunk_ids': uncertain,
                       'cell_model_input_unverified_chunk_ids': cell_unknown, 'stage_chunk_ids': stages,
                       'published_matching_chunk_ids': published_matches[:1000],
                       'published_matches_truncated': len(published_matches) > 1000,
                       'table_expansion_reasons': expansion_reasons(published_matches),
                       'model_input_status': ('captured' if model_input is not None else 'not_captured') if chat else 'not_requested',
                       'model_input_matches': model_fields, 'model_input_scope': 'source_evidence_fields_only',
                       'metadata_match_status': ('checked' if model_input is not None and terms
                                                  and probe.row is None and probe.column is None else 'not_checked'),
                       'answer_sentence_ids': [index for index, sentence in enumerate(sentence_texts, 1)
                                               if terms and match_terms(terms, sentence)],
                       'answer_literal_match': match_terms(terms, answer) if answer is not None and terms else None,
                       'candidate_match_scope': 'published_chunk_metadata', 'answer_match_scope': 'whole_answer_literal_terms',
                       'answer_match_origin': ('model_response' if 'answer_text' in private else 'returned_response') if chat else None,
                       'snapshot_status': snapshot_status})
    return output
