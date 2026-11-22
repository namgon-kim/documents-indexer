import pytest
from indexer.published_retrieval import PublishedRetrieval


def table(chunk_id, element='e1', job='j1', part=0):
    return {'chunk_id': chunk_id, 'document_id': 'd1', 'job_id': job, 'kind': 'table',
            'element_ids': [element], 'part_number': part, 'text': '본문 ' + chunk_id}


def test_table_expansion_uses_only_exact_source_and_is_bounded():
    chunks = [table(str(i), part=i) for i in range(6)]
    chunks += [table('other', element='e2'), table('other-job', job='j2')]
    results = PublishedRetrieval._expand_table([chunks[2]], {c['chunk_id']: c for c in chunks})
    assert len(results) == 4
    assert all(c['element_ids'] == ['e1'] and c['job_id'] == 'j1' for c in results)
    assert results[0]['table_expansion_complete'] is False
    assert all(c['expansion_reason'] == 'same_source_table' for c in results[1:])


def test_table_expansion_reports_complete_and_does_not_duplicate_hits():
    first, second = table('one'), table('two', part=1)
    results = PublishedRetrieval._expand_table([first, second], {'one': first, 'two': second})
    assert len(results) == 2
    assert all(c['table_expansion_complete'] is True for c in results)


def test_heading_expands_only_if_its_section_has_one_table():
    hit = {'chunk_id': 'heading', 'document_id': 'd1', 'job_id': 'j1', 'kind': 'text',
           'page': 1, 'heading_path': ['목표'], 'heading_element_ids': ['h1']}
    first = {**table('one'), 'page': 1, 'heading_path': ['목표'], 'heading_element_ids': ['h1']}
    assert len(PublishedRetrieval._expand_table([hit], {'one': first})) == 2
    other = {**first, 'chunk_id': 'other', 'element_ids': ['e2']}
    assert len(PublishedRetrieval._expand_table([hit], {'one': first, 'other': other})) == 1


def test_expanded_table_fragments_are_independent_chat_evidence(tmp_path):
    import json
    from types import SimpleNamespace

    from indexer.chat import RagChat
    from indexer.contracts import ChatRequest, SearchRequest

    first, second = table('one'), table('two', part=1)
    for item in (first, second):
        item.update(page=1, heading_path=[], heading_element_ids=[], coordinates=None)
    version = tmp_path / 'v1'
    version.mkdir()
    (version / 'chunks.json').write_text(json.dumps([first, second]), encoding='utf-8')
    doc = tmp_path / 'd1'
    doc.mkdir()
    (doc / 'document.json').write_text(json.dumps({'filename': 'source.pdf', 'format': 'pdf'}))
    publisher = SimpleNamespace(root=tmp_path, search=lambda *args: {'index_version': 'v1', 'results': [first]})
    storage = SimpleNamespace(document_dir=lambda _: doc,
                              load_job=lambda *args: [{'element_id': 'e1', 'page': 1}])
    results = PublishedRetrieval(publisher, storage).search(SearchRequest(query='계획', method='bm25'))
    assert len(results['results']) == 2
    assert all(hit['source_refs'][0]['role'] == 'evidence' for hit in results['results'])
    class Client:
        def post(self, *args, **kwargs):
            payload = kwargs['json']
            sent = json.loads(payload['messages'][-1]['content'])['검색된 문서']
            assert [entry['text'] for entry in sent] == ['본문 one', '본문 two']
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {'choices': [
                {'message': {'role': 'assistant', 'content': '내용 [2]'}, 'finish_reason': 'stop'}]})
    chat = RagChat(SimpleNamespace(llm_model='m', llm_base_url='http://localhost', llm_api_key='x', remote_timeout=1, chat_evidence_bytes=32768),
                   None, Client())
    answer = chat.answer((ChatRequest(message='계획'), results))
    assert answer['answer'] == '내용 [2]' and len(answer['sources']) == 2


def test_reranked_search_expands_table_with_configured_ranking(tmp_path):
    import json
    from types import SimpleNamespace

    from indexer.contracts import SearchRequest

    chunks = [table(str(index), part=index) for index in range(3)]
    for chunk in chunks:
        chunk.update(page=1, heading_path=[], heading_element_ids=[], coordinates=None)
    folder = tmp_path / 'v1'
    folder.mkdir()
    (folder / 'chunks.json').write_text(json.dumps(chunks), encoding='utf-8')
    document = tmp_path / 'd1'
    document.mkdir()
    (document / 'document.json').write_text(json.dumps({'filename': 'source.pdf', 'format': 'pdf'}))
    def search(*args, **kwargs):
        assert kwargs == {'retrieval_top_n': 50, 'rerank_top_n': 1}
        return {'index_version': 'v1', 'results': [chunks[0]], 'search_counts': {'result_limit': 1}}
    publisher = SimpleNamespace(root=tmp_path, search=search)
    storage = SimpleNamespace(document_dir=lambda _: document, load_job=lambda *args: [{'element_id': 'e1', 'page': 1}])
    hits = PublishedRetrieval(publisher, storage, ranking={'retrieval_top_n': 50, 'rerank_top_n': 1}).search(
        SearchRequest(query='표', method='bm25', rerank=True))['results']
    assert [hit['chunk_id'] for hit in hits] == ['0', '1', '2']
    assert all(hit['table_expansion_complete'] for hit in hits)


def test_expansion_shares_budget_between_independent_ranked_tables():
    first = [table(f'a{i}', element='a', part=i) for i in range(4)]
    second = [table(f'b{i}', element='b', part=i) for i in range(4)]
    chunks = first + second
    result = PublishedRetrieval._expand_table([first[0], second[0]], {c['chunk_id']: c for c in chunks})
    assert [c['chunk_id'] for c in result] == ['a0', 'b0', 'a1', 'b1', 'a2']
    assert not result[0]['table_expansion_complete']
    assert not result[1]['table_expansion_complete']


@pytest.mark.parametrize(('topic', 'parts'), [('inventory', 2), ('일정', 3), ('測定', 5)])
def test_indexed_table_fragments_flow_to_model_with_bounded_scope(tmp_path, topic, parts):
    from indexer.bm25_index import BM25Index
    from indexer.chat import _chat_evidence

    chunks = [{**table(str(part), part=part), 'text': f'{topic} item-{part}', 'page': part + 1}
              for part in range(parts)]
    index = BM25Index(tmp_path / 'test.sqlite')
    index.replace_document('d1', chunks)
    [ranked] = index.search(topic, 1)
    lookup = {chunk['chunk_id']: chunk for chunk in chunks}
    results = PublishedRetrieval._expand_table([lookup[ranked.chunk_id]], lookup)
    hits = [{**chunk, 'filename': 'source.pdf', 'source_refs': [], 'search_text': chunk['text']}
            for chunk in results]
    sources, context, counts = _chat_evidence(hits, 25, 32768)
    expected_ids = {chunk['chunk_id'] for chunk in results}
    assert len(sources) == min(parts, 4) and counts['evidence_omitted_count'] == 0
    assert {source['chunk_id'] for source in sources} == expected_ids
    assert {item['text'] for item in context} == {lookup[chunk_id]['text'] for chunk_id in expected_ids}
    assert all(chunk['table_expansion_complete'] == (parts <= 4) for chunk in results)


def test_search_reports_list_fragment_coverage_without_removing_candidates(tmp_path):
    import json
    from types import SimpleNamespace

    from indexer.contracts import SearchRequest

    chunks = [{**table('a', element='list'), 'kind': 'text', 'page': 1},
              {**table('b', element='list', part=1), 'kind': 'text', 'page': 2},
              {**table('c', element='other'), 'kind': 'text', 'page': 2}]
    version = tmp_path / 'v'
    version.mkdir()
    (version / 'chunks.json').write_text(json.dumps(chunks))
    doc = tmp_path / 'd1'
    doc.mkdir()
    (doc / 'document.json').write_text(json.dumps({'filename': 'source.pdf', 'format': 'pdf'}))
    publisher = SimpleNamespace(root=tmp_path, search=lambda *args: {'index_version': 'v',
                                                                    'results': [chunks[0], chunks[2]]})
    storage = SimpleNamespace(document_dir=lambda _: doc, load_job=lambda *_: [
        {'element_id': 'list', 'page': 1}, {'element_id': 'other', 'page': 2}])
    result = PublishedRetrieval(publisher, storage).search(SearchRequest(query='목록', method='bm25'))
    assert [hit['chunk_id'] for hit in result['results']] == ['a', 'c', 'b']
    assert result['results'][2]['expansion_reason'] == 'same_source_element'
    assert result['results'][0]['element_scopes'] == [
        {'element_id': 'list', 'indexed_chunks': 2, 'returned_chunks': 2, 'all_indexed_chunks_returned': True}]
    assert result['results'][1]['element_scopes'][0]['all_indexed_chunks_returned']


def test_text_expansion_is_bounded_scoped_and_preserves_ranked_candidates():
    chunks = [{**table(str(i), element='list', part=i), 'kind': 'text', 'page': 1 if i < 4 else 2}
              for i in range(6)]
    other = {**table('different', element='other'), 'kind': 'text', 'page': 1}
    other_job = {**table('other-job', element='list', job='j2'), 'kind': 'text', 'page': 1}
    indexed = {item['chunk_id']: item for item in [*chunks, other, other_job]}
    selected = [chunks[0], other]
    expanded = PublishedRetrieval._expand_text_elements(selected, indexed, allowed_pages={1})
    assert [item['chunk_id'] for item in expanded] == ['0', 'different', '1', '2', '3']
    assert all(item['page'] == 1 for item in expanded)
    assert all(item.get('expansion_reason') == 'same_source_element' for item in expanded[2:])


def test_text_expansion_does_not_merge_multiple_source_elements():
    anchor = {**table('a'), 'kind': 'text', 'element_ids': ['one', 'two']}
    candidate = {**table('b', element='one'), 'kind': 'text'}
    assert PublishedRetrieval._expand_text_elements([anchor], {'a': anchor, 'b': candidate}) == [anchor]


def test_external_q13_evidence_reaches_model_without_copying_source_into_repository(tmp_path):
    """Optional integration check against the independently supplied evidence archive."""
    import json
    import os
    import re
    from pathlib import Path
    from zipfile import ZipFile

    from indexer.bm25_index import BM25Index
    from indexer.chat import _chat_evidence

    archive = os.environ.get('POC_Q13_EVIDENCE_ZIP')
    if not archive:
        if os.environ.get('REQUIRE_POC_Q13_EVIDENCE') == '1':
            pytest.fail('POC_Q13_EVIDENCE_ZIP is required for this evidence validation run')
        pytest.skip('set POC_Q13_EVIDENCE_ZIP to run against the original evidence archive')
    with ZipFile(Path(archive)) as data:
        chunks = [chunk for chunk in json.loads(data.read('03_job_chunks_selected_pages.json'))
                  if chunk.get('element_ids') == ['e118']]
    assert len(chunks) == 3
    index = BM25Index(tmp_path / 'evidence.sqlite')
    index.replace_document(chunks[0]['document_id'], chunks)
    [ranked] = index.search('1-1-1 1차년도 추진 일정', 1)
    lookup = {chunk['chunk_id']: chunk for chunk in chunks}
    expanded = PublishedRetrieval._expand_table([lookup[ranked.chunk_id]], lookup)
    hits = [{**chunk, 'filename': 'source.pdf', 'source_refs': [], 'search_text': chunk['text']}
            for chunk in expanded]
    _, context, _ = _chat_evidence(hits, 25, 32768)
    assert {item['text'] for item in context} == {chunk['text'] for chunk in chunks}
    dates = re.findall(r"'\d{2}\.\d{2}-'\d{2}\.\d{2}", '\n'.join(item['text'] for item in context))
    assert len(dates) == 6


def test_six_synthetic_items_reach_model_across_three_table_fragments():
    from indexer.chat import _chat_evidence

    items = [f'entry-{index}' for index in range(6)]
    chunks = [{**table(str(index), part=index), 'text': ' '.join(items[index * 2:index * 2 + 2])}
              for index in range(3)]
    expanded = PublishedRetrieval._expand_table([chunks[1]], {c['chunk_id']: c for c in chunks})
    hits = [{**chunk, 'filename': 'synthetic', 'source_refs': [], 'search_text': chunk['text']}
            for chunk in expanded]
    sources, context, counts = _chat_evidence(hits, 25, 32768)
    assert len(sources) == 3 and counts['evidence_omitted_count'] == 0
    assert set(' '.join(item['text'] for item in context).split()) == set(items)
