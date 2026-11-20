import json
from types import SimpleNamespace

from indexer.contracts import SearchRequest
from indexer.published_retrieval import PublishedRetrieval
from indexer.visual_relations import apply_visual_relations, preserve_visual_relations


def layout(x, y):
    return {'space': 'pptx_emu', 'left': x, 'top': y, 'width': 100, 'height': 100,
            'container_width': 1000, 'container_height': 1000}


def test_pptx_relation_is_inferred_only_with_unambiguous_nearby_image():
    location = {'kind': 'pptx_slide', 'slide': 1}
    text = {'element_id': 'e1', 'kind': 'paragraph', 'text': '설명', 'office_location': location, 'layout': layout(100, 100)}
    image = {'element_id': 'e2', 'source_origin': 'native_image', 'asset_id': 'x', 'visual_id': 'v1',
             'office_location': location, 'layout': layout(210, 100)}
    apply_visual_relations([text, image])
    assert text['related_images'][0]['type'] == 'nearby'
    assert text['related_images'][0]['origin'] == 'inferred'
    ambiguous = {**image, 'element_id': 'e3', 'visual_id': 'v2', 'layout': layout(0, 100)}
    text.pop('related_images')
    apply_visual_relations([text, image, ambiguous])
    assert 'related_images' not in text


def test_existing_relation_is_remapped_without_fabricating_excluded_links():
    old = [{'element_id': 'e1', 'related_images': [{'element_id': 'e2', 'type': 'nearby', 'origin': 'inferred'}]},
           {'element_id': 'e2', 'source_origin': 'native_image'}]
    new = [{'element_id': 'e10', 'previous_element_id': 'e1'},
           {'element_id': 'e11', 'previous_element_id': 'e2', 'source_origin': 'native_image'}]
    preserve_visual_relations(new, old)
    assert new[0]['related_images'][0]['element_id'] == 'e11'
    new[1]['image_policy'] = 'exclude'
    preserve_visual_relations(new, old)
    assert new[0]['related_images'][0]['element_id'] == 'e11'


def test_search_returns_related_image_only_for_nearby_excel_cells(tmp_path):
    version = tmp_path / 'v'
    version.mkdir()
    chunk = {'chunk_id': 'c', 'document_id': 'd', 'job_id': 'j', 'kind': 'table', 'element_ids': ['e1'],
             'page': None, 'text': '표', 'table_cells': [{'row': 5, 'column': 2}]}
    (version / 'chunks.json').write_text(json.dumps([chunk]))
    doc = tmp_path / 'd'
    doc.mkdir()
    (doc / 'document.json').write_text(json.dumps({'filename': 'test.xlsx', 'format': 'xlsx'}))
    elements = [{'element_id': 'e1', 'related_images': [{'element_id': 'e2', 'type': 'cell_anchor',
                 'origin': 'source', 'status': 'unverified'}]},
                {'element_id': 'e2', 'asset_id': 'a', 'layout': {'start_row': 5, 'start_column': 2}}]
    storage = SimpleNamespace(document_dir=lambda _: doc, load_job=lambda *_: elements)
    publisher = SimpleNamespace(root=tmp_path, search=lambda *_: {'index_version': 'v', 'results': [chunk]})
    retrieval = PublishedRetrieval(publisher, storage)
    hit = retrieval.search(SearchRequest(query='표', method='bm25'))['results'][0]
    assert hit['related_images'][0]['element_id'] == 'e2'
    elements[1]['image_policy'] = 'exclude'
    assert retrieval.search(SearchRequest(query='표', method='bm25'))['results'][0]['related_images'] == []
    elements[1].pop('image_policy')
    chunk['table_cells'] = [{'row': 50, 'column': 8}]
    (version / 'chunks.json').write_text(json.dumps([chunk]))
    hit = retrieval.search(SearchRequest(query='표', method='bm25'))['results'][0]
    assert hit['related_images'] == []


def test_related_image_ocr_is_added_as_separate_evidence_with_relation(tmp_path):
    version = tmp_path / 'v'
    version.mkdir()
    base = {'chunk_id': 'base', 'document_id': 'd', 'job_id': 'j', 'kind': 'text',
            'element_ids': ['e1'], 'page': None, 'text': '본문'}
    ocr = {**base, 'chunk_id': 'ocr', 'element_ids': ['e3'], 'text': '그림 글자'}
    (version / 'chunks.json').write_text(json.dumps([base, ocr]))
    doc = tmp_path / 'd'
    doc.mkdir()
    (doc / 'document.json').write_text(json.dumps({'filename': 'test.docx', 'format': 'docx'}))
    elements = [{'element_id': 'e1', 'related_images': [{'element_id': 'e2', 'type': 'same_container',
                 'origin': 'source', 'status': 'unverified'}]},
                {'element_id': 'e2', 'asset_id': 'a', 'visual_id': 'image-0'},
                {'element_id': 'e3', 'source_origin': 'visual_analysis', 'visual_id': 'image-0'}]
    storage = SimpleNamespace(document_dir=lambda _: doc, load_job=lambda *_: elements)
    publisher = SimpleNamespace(root=tmp_path, search=lambda *_: {'index_version': 'v', 'results': [base]})
    hits = PublishedRetrieval(publisher, storage).search(SearchRequest(query='본문', method='bm25'))['results']
    assert [hit['chunk_id'] for hit in hits] == ['base', 'ocr']
    assert hits[1]['visual_relation']['status'] == 'unverified'
    assert hits[1]['evidence_urls'][0].endswith('/e3')


def test_pageless_native_neighbors_expand_only_with_same_scope(tmp_path):
    version = tmp_path / 'v'
    version.mkdir()
    first = {'chunk_id': 'first', 'document_id': 'd', 'job_id': 'j', 'kind': 'text', 'element_ids': ['e1'],
             'page': None, 'source_scope': 'section:one', 'text': '첫 내용', 'body_text': '첫 내용',
             'heading_path': ['제목'], 'next_chunk_id': 'second'}
    second = {**first, 'chunk_id': 'second', 'element_ids': ['e2'], 'text': '다음 내용', 'body_text': '다음 내용',
              'previous_chunk_id': 'first'}
    (version / 'chunks.json').write_text(json.dumps([first, second]))
    doc = tmp_path / 'd'
    doc.mkdir()
    (doc / 'document.json').write_text(json.dumps({'filename': 'test.hwpx', 'format': 'hwpx'}))
    elements = [{'element_id': 'e1'}, {'element_id': 'e2'}]
    storage = SimpleNamespace(document_dir=lambda _: doc, load_job=lambda *_: elements)
    publisher = SimpleNamespace(root=tmp_path, search=lambda *_: {'index_version': 'v', 'results': [first]})
    retrieval = PublishedRetrieval(publisher, storage)
    assert retrieval.search(SearchRequest(query='첫', method='bm25'))['results'][0]['context_after'] == '다음 내용'
    second['source_scope'] = 'section:two'
    (version / 'chunks.json').write_text(json.dumps([first, second]))
    assert retrieval.search(SearchRequest(query='첫', method='bm25'))['results'][0]['context_after'] == ''
