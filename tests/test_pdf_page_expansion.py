import json
from types import SimpleNamespace

import pytest
from indexer.contracts import SearchRequest
from indexer.published_retrieval import PublishedRetrieval


@pytest.mark.parametrize('rerank', [False, True])
def test_page_expansion_respects_scope_and_reports_final_selection(tmp_path, rerank):
    root = tmp_path / 'index'
    (root / 'v').mkdir(parents=True)
    document = tmp_path / 'doc'
    document.mkdir()
    (document / 'document.json').write_text(json.dumps({'format': 'pdf', 'filename': 'a.pdf'}))
    common = {'document_id': 'doc', 'job_id': 'job', 'heading_path': []}
    chunks = [
        {**common, 'chunk_id': 'anchor', 'page': 1, 'kind': 'table', 'element_ids': ['table'],
         'text': 'needle', 'part_number': 0},
        {**common, 'chunk_id': 'same', 'page': 1, 'kind': 'table', 'element_ids': ['table'],
         'text': 'same page', 'part_number': 1},
        {**common, 'chunk_id': 'other', 'page': 2, 'kind': 'table', 'element_ids': ['table'],
         'text': 'other page', 'part_number': 2},
        {**common, 'chunk_id': 'visual1', 'page': 1, 'kind': 'text', 'element_ids': ['analysis1'],
         'text': 'same page analysis'},
        {**common, 'chunk_id': 'visual2', 'page': 2, 'kind': 'text', 'element_ids': ['analysis2'],
         'text': 'other page analysis'},
    ]
    (root / 'v' / 'chunks.json').write_text(json.dumps(chunks))
    elements = [
        {'element_id': 'table', 'page': 1, 'related_images': [
            {'element_id': 'image', 'type': 'nearby'}, {'element_id': 'other_image', 'type': 'nearby'}]},
        {'element_id': 'image', 'page': 1, 'asset_id': 'asset', 'visual_id': 'visual'},
        {'element_id': 'other_image', 'page': 2, 'asset_id': 'asset2', 'visual_id': 'visual'},
        {'element_id': 'analysis1', 'page': 1, 'source_origin': 'visual_analysis', 'visual_id': 'visual'},
        {'element_id': 'analysis2', 'page': 2, 'source_origin': 'visual_analysis', 'visual_id': 'visual'},
    ]
    publisher = SimpleNamespace(root=root, get_version=lambda _: {'version': 'v', 'document_ids': ['doc']},
                                deleted_documents=lambda: set())
    publisher.reranker_factory = lambda _: SimpleNamespace(rerank=lambda query, candidates, limit: candidates[:limit])
    storage = SimpleNamespace(document_dir=lambda _: document, load_job=lambda *_: elements)
    retrieval = PublishedRetrieval(publisher, storage)
    request = SearchRequest(query='needle', method='bm25', top_k=1, rerank=rerank)
    result = retrieval.search_pdf_pages(request, [1])
    assert {hit['chunk_id'] for hit in result['results']} == {'anchor', 'same', 'visual1'}
    assert result['page_scope']['selected_pages'] == [1]
    assert result['page_scope']['all_page_chunks_selected']
    assert not result['results'][0]['table_expansion_complete']
    assert len(result['results'][0]['related_images']) == 1
    assert all(ref['page'] == 1 for hit in result['results'] for ref in hit['source_refs']
               if ref['role'] == 'evidence')
    both = retrieval.search_pdf_pages(request, [1, 2])
    assert both['page_scope']['selected_pages'] == [1, 2]
    assert both['page_scope']['all_page_chunks_selected']
