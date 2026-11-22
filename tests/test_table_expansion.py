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
