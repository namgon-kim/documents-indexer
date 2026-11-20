from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from indexer.artifact_store import ArtifactStore
from indexer.chunking import make_chunks, normalize_elements
from indexer.contracts import IndexerError
from indexer.ingestion_service import IngestionService
from indexer.office_locations import apply_source_locations
from indexer.parsing.native_documents import NativeExtractor
from indexer.visual_relations import apply_visual_relations

FIXTURES = Path(__file__).parent / 'fixtures' / 'hwp'


def test_nested_table_notice_is_a_limitation_not_an_unresolved_image():
    from indexer.review_status import (
        HWP_NESTED_TABLE_LIMITATION,
        HWP_NESTED_TABLE_NOTICE,
        normalize_review_status,
    )

    original = {'provider': 'native_hwp', 'provider_unavailable': [], 'coverage': {
        'warnings': [HWP_NESTED_TABLE_NOTICE], 'limitations': [], 'provider_incomplete': True,
        'missing_pages': [], 'unavailable_pages': []}}
    updated = normalize_review_status(original)
    assert updated['coverage']['warnings'] == []
    assert updated['coverage']['limitations'] == [HWP_NESTED_TABLE_LIMITATION]
    assert not updated['coverage']['provider_incomplete']
    assert original['coverage']['provider_incomplete']
    with_other_loss = {**original, 'coverage': {**original['coverage'], 'warnings': [HWP_NESTED_TABLE_NOTICE, '이미지 미처리']}}
    assert normalize_review_status(with_other_loss)['coverage']['provider_incomplete']
    assert normalize_review_status({**original, 'provider_unavailable': [1]})['coverage']['provider_incomplete']


def test_real_hwp_outline_and_numbered_lists_are_distinguished():
    result = NativeExtractor(None, 'native_text').extract(FIXTURES / 'outline-lists.hwp', 'hwp')
    assert not result.incomplete
    headings = [item for item in result.elements if item['category'] == 'heading']
    lists = [item for item in result.elements if item['category'] == 'list_item']
    assert headings and lists
    assert all(item['_hwp_style']['head_shape'] == 'outline' and item['_heading_explicit'] for item in headings)
    assert all(item['_hwp_style']['head_shape'] in {'number', 'bullet'} for item in lists)
    normalized = normalize_elements(result.elements)
    assert all(item['heading_level_origin'] == 'parser' for item in normalized if item['kind'] == 'heading')
    assert all(item['page'] is None for item in normalized)


def test_real_hwp_table_spans_and_captions_are_preserved():
    parsed = NativeExtractor(None, 'native_text').extract(FIXTURES / 'merged-table.hwp', 'hwp')
    table = next(item for item in parsed.elements if item['category'] == 'table')
    soup = BeautifulSoup(table['content']['html'], 'html.parser')
    assert any(cell.get('rowspan') == '2' for cell in soup.find_all('td'))
    assert any(cell.get('colspan') == '2' for cell in soup.find_all('td'))
    captions = NativeExtractor(None, 'native_text').extract(FIXTURES / 'table-captions.hwp', 'hwp')
    assert sum('<caption>' in item['content']['html'] for item in captions.elements) == 8
    elements = normalize_elements(captions.elements)
    chunks = make_chunks('d', 'j', elements, 400, 0)
    assert any('표  위 캡션' in chunk['text'] for chunk in chunks)


def test_real_hwp_source_order_images_and_searchable_table(tmp_path):
    path = FIXTURES / 'document-with-tables-images.hwp'
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, parser=None)
    result = service.ingest(path, settings={'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0})
    assert result['status'] == 'partial' and result['pending_review']
    doc, job = result['document_id'], result['job_id']
    raw = store.load_job(doc, job, 'parser_elements')
    elements = store.load_job(doc, job, 'elements')
    chunks = store.load_job(doc, job, 'chunks')
    assert any(item['kind'] == 'table' and 'A0' in item['text'] for item in elements)
    assert any('A0' in item['body_text'] and item['kind'] == 'table' for item in chunks)
    assert any('표  2x2짜리표가나다' in item['text'] for item in chunks)
    assert any(item.get('related_images') and '다음 페이지' in item['text'] for item in elements)
    images = [item for item in elements if item.get('source_origin') == 'native_image']
    assert any((item.get('office_location') or {}).get('kind') == 'hwp_paragraph' for item in images)
    assert any(item.get('office_location') is None for item in images)
    assert all(store.image_path(doc, job, image['asset_id']).is_file() for image in images)
    assert [item['_origin'] for item in raw].count('native_image') == len(images)


def test_hwp_explicit_heading_does_not_use_flat_heuristic():
    parsed = NativeExtractor(None, 'native_text').extract(FIXTURES / 'outline-lists.hwp', 'hwp')
    elements = normalize_elements(parsed.elements)
    shape_by_id = {item['element_id']: item['hwp_style'] for item in elements if item.get('hwp_style')}
    assert all(shape_by_id[item['element_id']]['head_shape'] == 'outline'
               for item in elements if item['kind'] == 'heading')
    apply_source_locations(elements, parsed.elements, {'format': 'hwp'})
    apply_visual_relations(elements)
    assert all(item.get('office_location', {}).get('kind') == 'hwp_paragraph' for item in elements)


def test_invalid_hwp_xml_is_not_silently_replaced_with_plain_text(tmp_path, monkeypatch):
    import subprocess

    import olefile
    class FakeOle:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def listdir(self): return []
    def run(command, **kwargs):
        Path(command[command.index('--output') + 1]).write_text('<HwpDoc>', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(olefile, 'OleFileIO', lambda *_: FakeOle())
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(IndexerError) as error:
        NativeExtractor(None, 'native_text').extract(tmp_path / 'bad.hwp', 'hwp')
    assert error.value.code == 'NATIVE_EXTRACTION_FAILED'


def test_real_hwp_image_review_keeps_job_and_publishes_after_all_choices(tmp_path):
    import json

    import httpx
    from indexer.dense_index import EmbeddingClient, EmbeddingProfile
    from indexer.index_publication import IndexPublisher
    from qdrant_client import QdrantClient

    def embed(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={'model': payload['model'], 'data': [
            {'index': index, 'embedding': [1.0, 0.5]} for index in range(len(payload['input']))]})
    with httpx.Client(transport=httpx.MockTransport(embed)) as client:
        vector = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, vector, EmbeddingClient(client, 'https://embed.test/v1', 'key',
                                   EmbeddingProfile('p', 'doc', 'query', 2)))
        store = ArtifactStore(tmp_path)
        service = IngestionService(store, None, publisher)
        first = service.ingest(FIXTURES / 'document-with-tables-images.hwp',
            settings={'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0})
        doc, job = first['document_id'], first['job_id']
        assert first['pending_review'] and publisher.active() is None
        elements = store.load_job(doc, job, 'elements')
        linked = next(item['related_images'][0]['element_id'] for item in elements if item.get('related_images'))
        images = [item for item in elements if item.get('source_origin') == 'native_image']
        actions = {item['element_id']: 'original' if item['element_id'] == linked else 'exclude' for item in images}
        queued = service.enqueue_image_actions(doc, job, actions)
        assert queued['job_id'] == job
        draft = service.run_job(doc, job)
        assert not draft['pending_review'] and draft['status'] == 'completed'
        assert '본문과의 위치를 확인하지 못한 HWP 이미지가 있습니다.' in draft['coverage']['limitations']
        assert publisher.active()['version'] == draft['index_version']
        from indexer.contracts import SearchRequest
        from indexer.published_retrieval import PublishedRetrieval
        result = PublishedRetrieval(publisher, store).search(SearchRequest(query='다음 페이지', method='bm25'))
        assert any(hit.get('related_images') for hit in result['results'])
        vector.close()


def test_legacy_resolved_image_draft_is_queued_and_published_without_reparsing(tmp_path):
    import json
    import shutil

    import httpx
    from indexer.dense_index import EmbeddingClient, EmbeddingProfile
    from indexer.index_publication import IndexPublisher
    from indexer.review_status import HWP_NESTED_TABLE_NOTICE
    from qdrant_client import QdrantClient

    def embed(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={'model': body['model'], 'data': [
            {'index': i, 'embedding': [1.0, 0.5]} for i in range(len(body['input']))]})
    with httpx.Client(transport=httpx.MockTransport(embed)) as client:
        vector = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, vector, EmbeddingClient(client, 'https://embed.test/v1', 'key',
                                   EmbeddingProfile('p', 'doc', 'query', 2)))
        store = ArtifactStore(tmp_path)
        service = IngestionService(store, None, publisher)
        first = service.ingest(FIXTURES / 'document-with-tables-images.hwp', settings={'extraction_mode': 'native_text'})
        doc, job = first['document_id'], first['job_id']
        elements = store.load_job(doc, job, 'elements')
        for element in elements:
            if element.get('source_origin') == 'native_image':
                element['image_policy'] = 'exclude'
        store.save_job(doc, job, 'elements', elements)
        edit_id = 'a' * 32
        folder = store.document_dir(doc) / 'jobs' / job
        snapshot = folder / 'draft-edits' / edit_id
        snapshot.mkdir(parents=True)
        for name in ('processing', 'parser_elements', 'parser_result', 'elements'):
            shutil.copy2(folder / f'{name}.json', snapshot / f'{name}.json')
        status = store.load_job(doc, job, 'processing')
        status.update({'mode': 'draft_image_actions', 'image_attempt': 'image-' + edit_id,
                       'image_snapshot': edit_id, 'image_actions': {}, 'force_review': True,
                       'provider': 'native_hwp', 'comparison_index_version': None,
                       'coverage': {**status['coverage'], 'provider_incomplete': True,
                                    'warnings': [HWP_NESTED_TABLE_NOTICE]}})
        store.save_job(doc, job, 'processing', status)
        service.recover()
        assert store.load_job(doc, job, 'processing')['status'] == 'queued'
        assert service.process_next()
        result = store.load_job(doc, job, 'processing')
        assert result['status'] == 'completed' and not result['pending_review']
        assert publisher.active()['version'] == result['index_version']
        vector.close()


def test_real_hwp_structure_keeps_plain_text_extraction(tmp_path):
    import subprocess
    import sys
    from unicodedata import normalize

    source = FIXTURES / 'document-with-tables-images.hwp'
    output = tmp_path / 'plain.txt'
    command = [str(Path(sys.executable).parent / 'hwp5txt'), '--output', str(output), str(source)]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    result = NativeExtractor(None, 'native_text').extract(source, 'hwp')
    structured = ' '.join(item['content']['text'] + ' ' + BeautifulSoup(item['content']['html'], 'html.parser').get_text(' ', strip=True)
                          for item in result.elements)
    searchable = ''.join(normalize('NFC', structured).split())
    for line in output.read_text(encoding='utf-8').splitlines():
        if line.strip() in {'<표>', '<그림>'}:
            continue
        assert ''.join(normalize('NFC', line).split()) in searchable
