import hashlib
import io
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from indexer.chunking import make_chunks, normalize_elements
from indexer.contracts import IndexerError
from indexer.office_inspection import inspect_office
from indexer.office_locations import apply_source_locations
from indexer.parsing.contracts import ParseResult
from indexer.parsing.native_documents import NativeExtractor
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from PIL import Image


def png():
    data = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(data, format='PNG')
    return data.getvalue()


def workbook(path, image_cell='B5'):
    book = Workbook()
    sheet = book.active
    sheet.title = '예산'
    sheet.append(['항목', '금액'])
    sheet.append(['개발', 42])
    sheet.merge_cells('A3:B3')
    sheet['A3'] = '합계'
    sheet.add_image(XLImage(io.BytesIO(png())), image_cell)
    book.save(path)


class ImageParser:
    config = SimpleNamespace(provider='paddle_vl')

    def __init__(self):
        self.calls = 0

    def parse(self, request):
        self.calls += 1
        assert request.format == 'png' and request.path.read_bytes().startswith(b'\x89PNG')
        return ParseResult('paddle_vl', [{'category': 'paragraph', 'content': {'text': '이미지 글자'}}], {})


def test_excel_direct_and_image_results_keep_source_and_order(tmp_path):
    path = tmp_path / 'example.xlsx'
    workbook(path)
    parser = ImageParser()
    result = NativeExtractor(parser, 'native_visual').extract(path, 'xlsx')
    assert parser.calls == 1 and not result.incomplete
    assert [e['category'] for e in result.elements] == ['heading', 'table', 'image', 'paragraph']
    assert result.elements[2]['source_location']['cell'] == 'B5'
    elements = normalize_elements(result.elements)
    apply_source_locations(elements, result.elements, inspect_office(path))
    assert elements[1]['office_location']['kind'] == 'xlsx_range'
    assert elements[3]['source_origin'] == 'visual_analysis'
    chunks = make_chunks('doc', 'job', elements)
    assert any('개발' in c['text'] and '42' in c['text'] for c in chunks)
    assert any('이미지 글자' in c['text'] for c in chunks)



def make_xlsx_state(path):
    from indexer.ingestion_graph import make_graph

    return make_graph(ImageParser()).invoke({'path': str(path), 'document_id': 'd', 'job_id': 'j',
        'settings': {'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0}})


def test_xlsx_regions_keep_absolute_cells_and_merged_rows(tmp_path):
    from indexer.ingestion_graph import make_graph
    from indexer.representation_audit import audit_representation

    path = tmp_path / 'regions.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'], sheet['B1'] = '첫 표', '값'
    sheet['A2'], sheet['B2'] = '항목', '100'
    sheet['A5'], sheet['B5'] = '다음 표', '결과'
    sheet['A6'], sheet['B6'] = '항목', '200'
    sheet.merge_cells('A8:A10')
    sheet['A8'], sheet['B10'] = '병합', '끝'
    book.save(path)
    book.close()
    state = make_graph(ImageParser()).invoke({'path': str(path), 'document_id': 'd', 'job_id': 'j',
        'settings': {'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0}})
    tables = [item for item in state['normalized'] if item['kind'] == 'table']
    assert len(tables) == 1
    assert tables[0]['layout']['boundary_origin'] == 'inferred'
    assert tables[0]['office_location']['start'] == 'A1'
    assert tables[0]['office_location']['end'] == 'B10'
    sheet_result = state['parser_result']['xlsx_sheets'][0]
    assert {'axis': 'row', 'after': 2, 'reason': 'blank_row'} in sheet_result['boundary_candidates']
    assert all(hint['after'] not in {8, 9} for hint in sheet_result['boundary_candidates'])
    cells = {(cell['row'], cell['column']) for chunk in state['chunks'] for cell in chunk.get('table_cells', [])}
    assert {(1, 1), (5, 1), (6, 2), (8, 1), (10, 2)} <= cells
    assert not audit_representation(state['normalized'], state['chunks'])['missing_table_cells']


def test_xlsx_retains_value_hidden_inside_merge_in_source_xml(tmp_path):
    from zipfile import ZIP_DEFLATED

    source = tmp_path / 'original.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'] = '제목'
    sheet.merge_cells('A1:D1')
    book.save(source)
    book.close()
    modified = tmp_path / 'hidden.xlsx'
    with ZipFile(source) as src, ZipFile(modified, 'w', ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == 'xl/worksheets/sheet1.xml':
                data = data.replace(b'</row>', '<c r="D1" t="inlineStr"><is><t>숨은 값</t></is></c></row>'.encode(), 1)
            dst.writestr(info, data)
    result = NativeExtractor(None, 'native_text').extract(modified, 'xlsx')
    hidden = result.raw['xlsx_sheets'][0]['hidden_merged_cells']
    assert hidden[0]['cell'] == 'D1' and hidden[0]['value'] == '숨은 값'
    assert hidden[0]['merged_range'] == 'A1:D1'
    state = make_xlsx_state(modified)
    assert any(cell['address'] == 'D1' and cell['hidden_merged']
               for chunk in state['chunks'] for cell in chunk.get('table_cells', []))
    assert not result.incomplete
    assert any('병합 영역 안에 숨은 값' in warning for warning in result.raw['warnings'])


def test_excel_fast_mode_marks_skipped_images_as_partial(tmp_path):
    path = tmp_path / 'example.xlsx'
    workbook(path)
    result = NativeExtractor(None, 'native_text').extract(path, 'xlsx')
    assert result.incomplete and result.raw['visual_count'] == 1
    assert any('이미지' in item for item in result.raw['warnings'])


def test_bitmap_is_converted_for_analysis_without_changing_original(tmp_path):
    from indexer.parsing.native_documents import image_for_analysis

    data = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(data, format='BMP')
    original = data.getvalue()
    sent, suffix = image_for_analysis(original, 'bmp')
    assert suffix == 'png' and sent.startswith(b'\x89PNG')
    assert original.startswith(b'BM')
    frames = io.BytesIO()
    first = Image.new('RGB', (3, 3), 'white')
    first.save(frames, format='GIF', save_all=True, append_images=[Image.new('RGB', (3, 3), 'black')])
    assert image_for_analysis(frames.getvalue(), 'gif') is None


def test_image_checkpoint_is_reused_and_unknown_result_is_not_resent(tmp_path):
    from indexer.ingestion_service import resumable_provider_checkpoint

    path = tmp_path / 'example.xlsx'
    workbook(path)
    events = []
    parser = ImageParser()
    NativeExtractor(parser, 'native_visual', persist=lambda event, value: events.append((event, value))).extract(path, 'xlsx')
    completed = events[-1][1]
    assert [value['status'] for _, value in events] == ['started', 'completed']
    assert resumable_provider_checkpoint({'visuals': [completed]}, 'initial')
    assert not resumable_provider_checkpoint({'visuals': [events[0][1]]}, 'initial')
    NativeExtractor(parser, 'native_visual', checkpoint={'visuals': [completed]}).extract(path, 'xlsx')
    assert parser.calls == 1
    pending = NativeExtractor(parser, 'native_visual', checkpoint={'visuals': [events[0][1]]}).extract(path, 'xlsx')
    assert pending.incomplete and any('완료 여부' in warning for warning in pending.raw['warnings'])
    assert any('개발' in item['content'].get('html', '') for item in pending.elements)
    assert parser.calls == 1


def test_image_internal_title_does_not_replace_document_heading(tmp_path):
    path = tmp_path / 'example.xlsx'
    workbook(path)
    class Parser(ImageParser):
        def parse(self, request):
            return ParseResult('paddle_vl', [{'category': 'heading',
                'content': {'text': '그림 내부 제목', 'html': '<h1>그림 내부 제목</h1>'}}], {})
    result = NativeExtractor(Parser(), 'native_visual').extract(path, 'xlsx')
    elements = normalize_elements(result.elements)
    item = next(element for element in elements if element['text'] == '그림 내부 제목')
    assert item['kind'] == 'paragraph' and item['heading_path'] == ['예산']


def test_hwpx_text_table_and_embedded_image(tmp_path):
    path = tmp_path / 'example.hwpx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('Contents/content.hpf', '<package><item id="img1" href="BinData/image1.png"/></package>')
        archive.writestr('Contents/section0.xml', '''<section><p><run><t>제목</t></run></p>
          <p><run><tbl><tr><tc><cellSpan rowSpan="1" colSpan="1"/><subList><p><run><t>표 값</t></run></p></subList></tc></tr></tbl>
          <pic><img binaryItemIDRef="img1"/></pic></run></p></section>''')
        archive.writestr('BinData/image1.png', png())
    parser = ImageParser()
    result = NativeExtractor(parser, 'native_visual').extract(path, 'hwpx')
    assert parser.calls == 1
    assert [item['category'] for item in result.elements] == ['paragraph', 'table', 'image', 'paragraph']
    assert '표 값' in result.elements[1]['content']['html']
    assert result.elements[2]['source_location']['section'] == 'Contents/section0.xml'


def test_settings_are_json_serializable(tmp_path):
    path = tmp_path / 'example.xlsx'
    workbook(path)
    result = NativeExtractor(None, 'native_text').extract(path, 'xlsx')
    json.dumps(result.elements, ensure_ascii=False)


def test_pptx_text_table_and_image_are_kept_in_slide(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    path = tmp_path / 'example.pptx'
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text_frame.text = '슬라이드 내용'
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    table.cell(0, 0).text = '항목'
    table.cell(1, 0).text = '개발'
    table.cell(1, 1).text = '42'
    slide.shapes.add_picture(io.BytesIO(png()), Inches(1), Inches(3))
    deck.save(path)
    parser = ImageParser()
    result = NativeExtractor(parser, 'native_visual').extract(path, 'pptx')
    assert parser.calls == 1 and not result.incomplete and result.raw['limitations']
    assert any(item['content']['text'] == '슬라이드 내용' for item in result.elements)
    assert any('개발' in item['content']['html'] and '42' in item['content']['html'] for item in result.elements)
    assert result.elements[-1]['source_location'] == {'kind': 'pptx_slide', 'slide': 1}


def test_docx_embedded_image_and_same_paragraph_relationship(tmp_path):
    from docx import Document
    from docx.shared import Inches
    from indexer.visual_relations import apply_visual_relations

    path = tmp_path / 'example.docx'
    doc = Document()
    paragraph = doc.add_paragraph('설명 문장')
    paragraph.add_run().add_picture(io.BytesIO(png()), width=Inches(1))
    doc.save(path)
    result = NativeExtractor(None, 'native_text').extract(path, 'docx')
    elements = normalize_elements(result.elements)
    apply_source_locations(elements, result.elements, {'format': 'docx'})
    apply_visual_relations(elements)
    image = next(item for item in elements if item.get('source_origin') == 'native_image')
    text = next(item for item in elements if item.get('text') == '설명 문장')
    assert text['related_images'][0]['element_id'] == image['element_id']
    assert text['related_images'][0]['type'] == 'same_container'


def test_graph_routes_native_mode_to_chunks_and_preserves_warning(tmp_path):
    from indexer.ingestion_graph import make_graph

    path = tmp_path / 'example.xlsx'
    workbook(path)
    result = make_graph(ImageParser()).invoke({'path': str(path), 'document_id': 'd', 'job_id': 'j',
        'settings': {'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0}})
    assert result['parser_provider'] == 'native_xlsx'
    assert result['coverage']['provider_incomplete'] is True
    assert any('이미지' in warning for warning in result['coverage']['warnings'])
    assert any('개발' in chunk['text'] for chunk in result['chunks'])
    assert any(ref.get('office_location', {}).get('kind') == 'xlsx_range'
               for chunk in result['chunks'] for ref in chunk.get('element_refs', [])
               if ref.get('office_location'))


def test_native_image_job_saves_checkpoint_and_reuses_parsing_for_rechunk(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    source = tmp_path / 'example.xlsx'
    workbook(source)
    parser = ImageParser()
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, parser)
    settings = {'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0,
                'dpi': None, 'ocr_policy': 'automatic'}
    first = service.ingest(source, settings=settings)
    doc, job = first['document_id'], first['job_id']
    checkpoint = store.provider_checkpoint(doc, job, 'initial')
    assert parser.calls == 1 and checkpoint['visuals'][0]['status'] == 'completed'
    original = store.load_job(doc, job, 'elements')
    image = next(e for e in original if e.get('source_origin') == 'native_image')
    assert store.image_path(doc, job, image['asset_id']).read_bytes() == png()
    recognized = next(e for e in original if e.get('source_origin') == 'visual_analysis')
    assert recognized['related_images'][0]['element_id'] == image['element_id']
    again = service.enqueue_reprocess(doc, job, 'rechunk', settings)
    service.run_job(doc, again['job_id'])
    assert parser.calls == 1
    assert store.image_path(doc, again['job_id'], image['asset_id']).read_bytes() == png()
    assert any('이미지 글자' in chunk['text'] for chunk in store.load_job(doc, again['job_id'], 'chunks'))


def test_complete_images_reuses_previous_results_and_only_calls_missing(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    source = tmp_path / 'example.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'] = '내용'
    sheet.add_image(XLImage(io.BytesIO(png())), 'B2')
    sheet.add_image(XLImage(io.BytesIO(png())), 'B4')
    book.save(source)
    parser = ImageParser()
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, parser)
    settings = {'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0,
                'dpi': None, 'ocr_policy': 'automatic'}
    first = service.ingest(source, settings=settings)
    doc, job = first['document_id'], first['job_id']
    assert parser.calls == 2
    checkpoint_file = store.document_dir(doc) / 'jobs' / job / 'provider' / 'initial' / 'visual-0001.json'
    checkpoint_file.unlink()
    retry = service.enqueue_reprocess(doc, job, 'complete_images', settings)
    service.run_job(doc, retry['job_id'])
    assert parser.calls == 3
    assert len(store.provider_checkpoint(doc, retry['job_id'], 'initial')['visuals']) == 2
    assert len(store.provider_checkpoint(doc, job, 'initial')['visuals']) == 1


def test_complete_images_refuses_unknown_submissions_before_new_work(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    source = tmp_path / 'example.xlsx'
    workbook(source)
    store = ArtifactStore(tmp_path)
    parser = ImageParser()
    service = IngestionService(store, parser)
    settings = {'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0,
                'dpi': None, 'ocr_policy': 'automatic'}
    first = service.ingest(source, settings=settings)
    doc, job = first['document_id'], first['job_id']
    checkpoint = store.provider_checkpoint(doc, job, 'initial')['visuals'][0]
    store.save_provider_event(doc, job, 'initial', 'visual', {**checkpoint, 'status': 'started'})
    with pytest.raises(ValueError, match='완료 여부'):
        service.enqueue_reprocess(doc, job, 'complete_images', settings)
    assert parser.calls == 1


def test_image_actions_merge_selected_results_and_preserve_existing_job(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    source = tmp_path / 'example.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'] = '기존 본문'
    sheet.add_image(XLImage(io.BytesIO(png())), 'B2')
    sheet.add_image(XLImage(io.BytesIO(png())), 'B4')
    book.save(source)
    parser = ImageParser()
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, parser)
    settings = {'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0,
                'dpi': None, 'ocr_policy': 'automatic'}
    first = service.ingest(source, settings=settings)
    doc, job = first['document_id'], first['job_id']
    old_raw = store.load_job(doc, job, 'parser_elements')
    old_elements = store.load_job(doc, job, 'elements')
    anchors = [item for item in old_elements if item.get('source_origin') == 'native_image']
    assert len(anchors) == 2 and parser.calls == 2
    action = service.enqueue_image_actions(doc, job, {anchors[0]['element_id']: 'both', anchors[1]['element_id']: 'exclude'})
    service.run_job(doc, action['job_id'])
    assert parser.calls == 3
    assert store.load_job(doc, job, 'parser_elements') == old_raw
    new_raw = store.load_job(doc, action['job_id'], 'parser_elements')
    new_anchors = [item for item in new_raw if item.get('_origin') == 'native_image']
    assert [item['_image_policy'] for item in new_anchors] == ['both', 'exclude']
    analyses = [item for item in new_raw if item.get('_origin') == 'visual_analysis']
    assert [item['_visual_id'] for item in analyses] == ['image-0', 'image-1']
    assert analyses[1]['_image_suppressed'] is True
    excluded = [item for item in store.load_job(doc, action['job_id'], 'elements') if item.get('search_exclusion') == 'image_policy']
    assert len(excluded) == 1
    assert all(excluded[0]['element_id'] not in chunk['element_ids'] for chunk in store.load_job(doc, action['job_id'], 'chunks'))
    assert all(store.image_path(doc, action['job_id'], item['_asset_id']).exists() for item in new_anchors)
    assert any('기존 본문' in item['content'].get('html', '') for item in new_raw)
    again = service.enqueue_reprocess(doc, action['job_id'], 'rechunk', settings)
    service.run_job(doc, again['job_id'])
    assert store.load_job(doc, again['job_id'], 'processing')['image_policies_present'] is True
    assert store.load_job(doc, again['job_id'], 'parser_elements') == new_raw
    with pytest.raises(ValueError, match='이미지별 설정'):
        service.enqueue_reprocess(doc, again['job_id'], 'complete_images', settings)
    stored = store.load_job(doc, again['job_id'], 'elements')
    hidden = next(item for item in stored if item.get('source_origin') == 'native_image' and item.get('image_policy') == 'exclude')
    restored = service.enqueue_image_actions(doc, again['job_id'], {hidden['element_id']: 'reuse'})
    service.run_job(doc, restored['job_id'])
    assert parser.calls == 3
    restored_elements = store.load_job(doc, restored['job_id'], 'elements')
    assert not any(item.get('search_exclusion') == 'image_policy' for item in restored_elements)


def test_image_actions_original_and_exclude_need_no_ocr(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    source = tmp_path / 'example.xlsx'
    workbook(source, 'B3')
    parser = ImageParser()
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, parser)
    settings = {'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0,
                'dpi': None, 'ocr_policy': 'automatic'}
    first = service.ingest(source, settings=settings)
    doc, job = first['document_id'], first['job_id']
    anchor = next(item for item in store.load_job(doc, job, 'elements') if item.get('source_origin') == 'native_image')
    action = service.enqueue_image_actions(doc, job, {anchor['element_id']: 'original'})
    service.run_job(doc, action['job_id'])
    assert parser.calls == 0
    image = next(item for item in store.load_job(doc, action['job_id'], 'elements') if item.get('source_origin') == 'native_image')
    assert image['image_policy'] == 'original' and store.image_path(doc, action['job_id'], image['asset_id']).exists()


def test_image_actions_publish_new_version_and_keep_old_evidence(tmp_path):
    import httpx
    from indexer.artifact_store import ArtifactStore
    from indexer.dense_index import EmbeddingClient, EmbeddingProfile
    from indexer.index_publication import IndexPublisher
    from indexer.ingestion_service import IngestionService
    from qdrant_client import QdrantClient

    source = tmp_path / 'example.xlsx'
    workbook(source)
    parser = ImageParser()

    def embed(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={'model': payload['model'], 'data': [
            {'index': index, 'embedding': [1.0, 0.5]} for index in range(len(payload['input']))]})

    with httpx.Client(transport=httpx.MockTransport(embed)) as client:
        vector = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, vector, EmbeddingClient(client, 'https://embed.test/v1', 'key',
                                   EmbeddingProfile('p', 'doc', 'query', 2)))
        storage = ArtifactStore(tmp_path)
        service = IngestionService(storage, parser, publisher)
        settings = {'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0,
                    'dpi': None, 'ocr_policy': 'automatic'}
        first = service.ingest(source, settings=settings)
        doc, old_job = first['document_id'], first['job_id']
        old_version = first['index_version']
        old_elements = storage.load_job(doc, old_job, 'elements')
        anchor = next(item for item in old_elements if item.get('source_origin') == 'native_image')
        pending = service.enqueue_image_actions(doc, old_job, {anchor['element_id']: 'both'})
        result = service.run_job(doc, pending['job_id'])
        assert not result['pending_review'] and publisher.active()['version'] == result['index_version']
        assert result['status'] == 'completed' and result['index_version'] != old_version
        assert storage.load_job(doc, old_job, 'elements') == old_elements
        assert publisher.search('이미지', 'bm25', version=old_version)['results']
        assert publisher.search('이미지', 'bm25', version=result['index_version'])['results']
        assert storage.image_path(doc, old_job, anchor['asset_id']).read_bytes() == png()
        assert storage.image_path(doc, pending['job_id'], anchor['asset_id']).read_bytes() == png()
        updated = storage.load_job(doc, pending['job_id'], 'elements')
        new_anchor = next(item for item in updated if item.get('source_origin') == 'native_image')
        excluded = service.enqueue_image_actions(doc, pending['job_id'], {new_anchor['element_id']: 'exclude'})
        excluded_result = service.run_job(doc, excluded['job_id'])
        assert not excluded_result['pending_review'] and publisher.active()['version'] == excluded_result['index_version']
        assert excluded_result['status'] == 'completed' and excluded_result['index_version'] != result['index_version']
        assert publisher.search('이미지', 'bm25', version=excluded_result['index_version'])['results'] == []
        assert publisher.search('이미지', 'bm25', version=result['index_version'])['results']
        assert storage.image_path(doc, excluded['job_id'], anchor['asset_id']).read_bytes() == png()
        vector.close()


def test_image_actions_require_explicit_retry_choice_for_unresolved_remote_request(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    source = tmp_path / 'example.xlsx'
    workbook(source)
    parser = ImageParser()
    storage = ArtifactStore(tmp_path)
    service = IngestionService(storage, parser)
    settings = {'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0,
                'dpi': None, 'ocr_policy': 'automatic'}
    first = service.ingest(source, settings=settings)
    doc, job = first['document_id'], first['job_id']
    anchor = next(item for item in storage.load_job(doc, job, 'elements') if item.get('source_origin') == 'native_image')
    checkpoint = storage.provider_checkpoint(doc, job, 'initial')['visuals'][0]
    storage.save_provider_event(doc, job, 'initial', 'visual', {**checkpoint, 'status': 'started'})
    queued = service.enqueue_image_actions(doc, job, {anchor['element_id']: 'both'})
    assert queued['status'] == 'queued'
    # The ambiguous initial request remains recorded for audit; the approved
    # retry is an independent job with its own checkpoint namespace.
    assert storage.provider_checkpoint(doc, job, 'initial')['visuals'][0]['status'] == 'started'
    assert parser.calls == 1


def test_image_actions_preserve_existing_document_image_relationship(tmp_path):
    from docx import Document
    from docx.shared import Inches
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService

    source = tmp_path / 'example.docx'
    document = Document()
    paragraph = document.add_paragraph('관련 설명')
    paragraph.add_run().add_picture(io.BytesIO(png()), width=Inches(1))
    document.save(source)
    parser = ImageParser()
    storage = ArtifactStore(tmp_path)
    service = IngestionService(storage, parser)
    settings = {'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0,
                'dpi': None, 'ocr_policy': 'automatic'}
    first = service.ingest(source, settings=settings)
    doc, job = first['document_id'], first['job_id']
    old = storage.load_job(doc, job, 'elements')
    paragraph = next(item for item in old if item.get('text') == '관련 설명')
    anchor = next(item for item in old if item.get('source_origin') == 'native_image')
    assert paragraph['related_images'][0]['element_id'] == anchor['element_id']
    pending = service.enqueue_image_actions(doc, job, {anchor['element_id']: 'both'})
    service.run_job(doc, pending['job_id'])
    new = storage.load_job(doc, pending['job_id'], 'elements')
    new_paragraph = next(item for item in new if item.get('text') == '관련 설명')
    new_anchor = next(item for item in new if item.get('source_origin') == 'native_image')
    assert new_paragraph['related_images'][0]['element_id'] == new_anchor['element_id']
    assert new_paragraph['related_images'][0]['type'] == paragraph['related_images'][0]['type']
    assert new_anchor['office_location'] == anchor['office_location']
    rechunk = service.enqueue_reprocess(doc, pending['job_id'], 'rechunk', settings)
    service.run_job(doc, rechunk['job_id'])
    after = storage.load_job(doc, rechunk['job_id'], 'elements')
    after_text = next(item for item in after if item.get('text') == '관련 설명')
    after_image = next(item for item in after if item.get('source_origin') == 'native_image')
    assert after_text['related_images'][0]['element_id'] == after_image['element_id']


def test_image_endpoints_require_session_and_return_original_bytes(tmp_path):
    from fastapi.testclient import TestClient
    from indexer.artifact_store import ArtifactStore
    from indexer.config import Config
    from indexer.ingestion_service import IngestionService

    from apps.api.main import create_app

    source = tmp_path / 'example.xlsx'
    workbook(source, 'B3')
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, ImageParser())
    result = service.ingest(source, settings={'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0})
    prefix = f"/v1/ingest/{result['document_id']}/jobs/{result['job_id']}"
    bmp = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(bmp, format='BMP')
    bmp_data = bmp.getvalue()
    bmp_id = hashlib.sha256(bmp_data).hexdigest()
    store.save_image(result['document_id'], result['job_id'], bmp_id, bmp_data)
    elements = store.load_job(result['document_id'], result['job_id'], 'elements')
    elements.append({'element_id': 'e999', 'source_origin': 'native_image', 'asset_id': bmp_id})
    store.save_job(result['document_id'], result['job_id'], 'elements', elements)
    with TestClient(create_app(Config(tmp_path, access_key='key', external_processing=True), ingestion_service=service),
                    base_url='https://testserver') as client:
        assert client.get(prefix + '/images').status_code == 401
        login = client.post('/v1/auth/login', json={'access_key': 'key'}, headers={'Origin': 'https://testserver'})
        assert login.status_code == 200
        images = client.get(prefix + '/images').json()['images']
        assert len(images) == 2
        response = client.get(images[0]['url'])
        assert response.status_code == 200 and response.content == png()
        assert response.headers['content-type'] == 'image/png'
        bitmap = client.get(prefix + '/elements/e999/image')
        assert bitmap.status_code == 200 and bitmap.headers['content-type'] == 'image/bmp' and bitmap.content == bmp_data
        preview = client.get(prefix + '/elements/e999/image?preview=true')
        assert preview.status_code == 200 and preview.headers['content-type'] == 'image/png'
        assert preview.content.startswith(b'\x89PNG')
        assert client.get(prefix + '/elements/e998/image').status_code == 404
        response = client.post(prefix + '/images/actions', json={'actions': {images[0]['element_id']: 'original'}},
                               headers={'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
        assert response.status_code == 202 and response.json()['job_id'] == result['job_id']


def test_unpublished_standalone_image_description_is_searchable_after_review(tmp_path):
    import httpx
    from indexer.artifact_store import ArtifactStore
    from indexer.dense_index import EmbeddingClient, EmbeddingProfile
    from indexer.index_publication import IndexPublisher
    from indexer.ingestion_service import IngestionService
    from qdrant_client import QdrantClient

    source = tmp_path / 'standalone.xlsx'
    book = Workbook()
    book.active.add_image(XLImage(io.BytesIO(png())), 'B2')
    book.save(source)
    def embed(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={'model': payload['model'], 'data': [
            {'index': i, 'embedding': [1.0, 0.5]} for i in range(len(payload['input']))]})
    with httpx.Client(transport=httpx.MockTransport(embed)) as client:
        vector = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, vector, EmbeddingClient(client, 'https://embed.test/v1', 'key',
                                   EmbeddingProfile('p', 'doc', 'query', 2)))
        store = ArtifactStore(tmp_path)
        service = IngestionService(store, ImageParser(), publisher)
        first = service.ingest(source, settings={'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0})
        doc, job = first['document_id'], first['job_id']
        assert first['pending_review'] and publisher.active() is None
        anchor = next(e for e in store.load_job(doc, job, 'elements') if e.get('source_origin') == 'native_image')
        with pytest.raises(ValueError, match='본문이 없는'):
            service.enqueue_image_actions(doc, job, {anchor['element_id']: 'original'})
        queued = service.enqueue_image_actions(doc, job, {anchor['element_id']: 'describe'},
                                               descriptions={anchor['element_id']: '기숙사 확장 조감도'})
        assert queued['job_id'] == job
        draft = service.run_job(doc, job)
        assert not draft['pending_review'] and publisher.active()['version'] == draft['index_version']
        elements = store.load_job(doc, job, 'elements')
        description = next(e for e in elements if e.get('source_origin') == 'visual_description')
        image = next(e for e in elements if e.get('source_origin') == 'native_image')
        assert description['text'] == '기숙사 확장 조감도'
        assert description['related_images'][0]['element_id'] == image['element_id']
        assert description['related_images'][0]['type'] == 'image_description'
        assert publisher.active()['version'] == draft['index_version']
        from indexer.contracts import SearchRequest
        from indexer.published_retrieval import PublishedRetrieval
        hits = PublishedRetrieval(publisher, store).search(SearchRequest(query='기숙사', method='bm25'))['results']
        assert hits and hits[0]['related_images'][0]['type'] == 'image_description'
        assert hits[0]['related_images'][0]['origin'] == 'user'
        vector.close()


def test_failed_image_edit_restores_unpublished_draft(tmp_path):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionFailure, IngestionService

    source = tmp_path / 'example.xlsx'
    workbook(source)
    class FailingParser(ImageParser):
        def parse(self, request):
            raise RuntimeError('simulated image failure')
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, FailingParser())
    first = service.ingest(source, settings={'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0})
    doc, job = first['document_id'], first['job_id']
    before = {name: store.load_job(doc, job, name) for name in ('parser_elements', 'elements', 'chunks')}
    anchor = next(e for e in before['elements'] if e.get('source_origin') == 'native_image')
    queued = service.enqueue_image_actions(doc, job, {anchor['element_id']: 'analyze'})
    assert queued['job_id'] == job
    with pytest.raises(IngestionFailure):
        service.run_job(doc, job)
    assert all(store.load_job(doc, job, name) == value for name, value in before.items())
    status = store.load_job(doc, job, 'processing')
    assert status['pending_review'] and status['last_image_failure']
    retry = service.enqueue_image_actions(doc, job, {anchor['element_id']: 'analyze'})
    assert retry['job_id'] == job and retry['image_attempt'] != queued['image_attempt']


def test_docx_heading_styles_and_table_headers_are_retained(tmp_path):
    from docx import Document
    from indexer.ingestion_graph import make_graph

    path = tmp_path / 'report.docx'
    document = Document()
    document.add_heading('1. 사업 개요', level=1)
    document.add_paragraph('사업의 목적을 설명합니다.')
    document.add_paragraph('사업을 추진합니다.')
    document.add_heading('1.1 예산', level=2)
    document.add_paragraph('예산 내용을 설명합니다.')
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = '항목', '금액'
    table.cell(1, 0).text, table.cell(1, 1).text = '인건비', '100'
    document.save(path)
    state = make_graph(ImageParser()).invoke({'path': str(path), 'document_id': 'd', 'job_id': 'j',
        'settings': {'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0}})
    assert not state['coverage']['provider_incomplete'] and state['coverage']['limitations']
    paragraph = next(item for item in state['normalized'] if item['text'] == '예산 내용을 설명합니다.')
    assert paragraph['heading_path'] == ['1. 사업 개요', '1.1 예산']
    table_chunk = next(item for item in state['chunks'] if item['kind'] == 'table')
    segment = next(part for part in table_chunk['table_segments'] if part['value'] == '100')
    assert '금액' in segment['label'] and segment['label_origin'] == 'inferred'
    assert {'항목', '금액', '인건비', '100'} <= {part['value'] for part in table_chunk['table_segments']}


def test_hwpx_outline_is_taken_from_paragraph_properties(tmp_path):
    path = tmp_path / 'outline.hwpx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('Contents/content.hpf', '<package/>')
        archive.writestr('Contents/header.xml', '<head><paraPr id="9"><heading type="OUTLINE" level="1"/></paraPr></head>')
        archive.writestr('Contents/section0.xml', '<section><p paraPrIDRef="9"><run><t>사업 </t><t>계획</t></run></p><p><run><t>본문</t></run></p></section>')
    result = NativeExtractor(None, 'native_text').extract(path, 'hwpx')
    assert not result.incomplete and result.raw['limitations']
    assert result.elements[0]['content']['html'] == '<h2>사업 계획</h2>'
    elements = normalize_elements(result.elements)
    assert elements[1]['heading_path'] == ['사업 계획']


def test_image_warnings_clear_only_after_every_image_is_resolved(tmp_path):
    from indexer.parsing.image_actions import apply_image_actions

    asset = hashlib.sha256(png()).hexdigest()
    (tmp_path / asset).write_bytes(png())
    anchors = [{'_origin': 'native_image', '_visual_id': f'image-{i}', '_asset_id': asset,
                '_image_format': 'png', 'content': {'text': '', 'html': ''}} for i in range(2)]
    base = {'provider': 'native_xlsx', 'elements': anchors, 'raw': {'warnings': [
        '텍스트를 확인할 수 없는 이미지가 있습니다.', '결과를 확인할 수 없는 이미지가 있습니다.',
        '차트는 직접 추출하지 않았습니다.']}}
    first = apply_image_actions(base, {'image-0': 'exclude'}, None, lambda key: tmp_path / key, {}, None, None)
    assert first.incomplete and '텍스트를 확인할 수 없는 이미지가 있습니다.' in first.raw['warnings']
    second = apply_image_actions({'provider': first.provider, 'elements': first.elements, 'raw': first.raw},
                                 {'image-1': 'describe'}, None, lambda key: tmp_path / key, {}, None, None,
                                 {'image-1': '조감도'})
    assert second.incomplete and second.raw['warnings'] == ['차트는 직접 추출하지 않았습니다.']


def test_unstyled_numbered_docx_heading_is_marked_inferred(tmp_path):
    from docx import Document
    path = tmp_path / 'numbered.docx'
    doc = Document()
    doc.add_paragraph('1. 사업 개요')
    doc.add_paragraph('본문입니다.')
    doc.add_paragraph('2024 예산 현황')
    doc.save(path)
    parsed = NativeExtractor(None, 'native_text').extract(path, 'docx')
    elements = normalize_elements(parsed.elements)
    assert elements[0]['kind'] == 'heading' and elements[0]['heading_level_origin'] == 'inferred'
    assert elements[1]['heading_path'] == ['1. 사업 개요']
    assert elements[2]['kind'] == 'paragraph'


def test_image_warning_is_removed_when_last_image_resolved(tmp_path):
    from indexer.parsing.image_actions import apply_image_actions
    asset = hashlib.sha256(png()).hexdigest()
    (tmp_path / asset).write_bytes(png())
    anchor = {'_origin': 'native_image', '_visual_id': 'image-0', '_asset_id': asset,
              '_image_format': 'png', 'content': {'text': '', 'html': ''}}
    base = {'provider': 'native_xlsx', 'elements': [anchor], 'raw': {'warnings': [
        '텍스트를 확인할 수 없는 이미지가 있습니다.', '결과를 확인할 수 없는 이미지가 있습니다.']}}
    result = apply_image_actions(base, {'image-0': 'describe'}, None, lambda key: tmp_path / key,
                                 {}, None, None, {'image-0': '도식 설명'})
    assert not result.incomplete and result.raw['warnings'] == []


def test_adjacent_blocks_from_same_image_are_grouped():
    raw = [{'category': 'paragraph', 'page': None, 'content': {'text': text},
            '_origin': 'visual_analysis', '_visual_id': visual} for visual, text in [
                ('image-0', '첫째 이미지 문장'), ('image-0', '첫째 이미지의 다음 문장'), ('image-1', '다른 이미지 문장')]]
    chunks = make_chunks('doc', 'job', normalize_elements(raw), 400, 0)
    assert len(chunks) == 2 and chunks[0]['element_ids'] == ['e1', 'e2']
    assert chunks[1]['source_scope'] == 'visual:image-1'


def test_numbered_body_does_not_override_docx_styled_headings(tmp_path):
    from docx import Document
    path = tmp_path / 'report.docx'
    document = Document()
    document.add_heading('제2장 사업', level=1)
    document.add_heading('가. 지원 대상', level=2)
    document.add_paragraph('1. 관내 중소기업')
    document.add_paragraph('2. 창업 3년 이내 기업')
    document.add_heading('나. 지원 내용', level=2)
    document.save(path)
    elements = normalize_elements(NativeExtractor(None, 'native_text').extract(path, 'docx').elements)
    assert [element['kind'] for element in elements] == ['heading', 'heading', 'paragraph', 'paragraph', 'heading']
    assert elements[-1]['heading_path'] == ['제2장 사업', '나. 지원 내용']


@pytest.mark.parametrize('text', ['10 명', '3 억원', '12 월 31 일까지 제출'])
def test_native_heading_inference_rejects_numbers_without_decimal_marker(text):
    assert NativeExtractor._numbered_heading_level(text) is None


def test_pptx_title_and_paragraphs_are_grouped_per_shape(tmp_path):
    from indexer.ingestion_graph import make_graph
    from pptx import Presentation
    from pptx.util import Inches
    path = tmp_path / 'slides.pptx'
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = '파트너십 강화'
    frame = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(2)).text_frame
    frame.text = '첫 문장'
    frame.add_paragraph().text = '둘째 문장'
    deck.save(path)
    state = make_graph(ImageParser()).invoke({'path': str(path), 'document_id': 'd', 'job_id': 'j',
        'settings': {'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0}})
    title = next(item for item in state['normalized'] if item['text'] == '파트너십 강화')
    assert title['kind'] == 'heading'
    body = next(chunk for chunk in state['chunks'] if '첫 문장' in chunk['text'])
    assert '둘째 문장' in body['text'] and '파트너십 강화' in body['heading_path']


def test_paddle_visual_response_keeps_text_and_completed_checkpoint(tmp_path):
    import httpx
    from indexer.parsing.contracts import ParserConfig
    from indexer.parsing.image_actions import apply_image_actions
    from indexer.parsing.paddle_vl_adapter import PaddleVLParser
    path = tmp_path / 'report.xlsx'
    workbook(path)
    payload = {'errorCode': 0, 'result': {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
        {'block_label': 'image', 'block_content': ''},
        {'block_label': 'text', 'block_content': '2024 매출 120억'}]}}]}}
    events = []
    assets = {}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        parser = PaddleVLParser(ParserConfig('paddle_vl', 'http://example', ''), client)
        first = NativeExtractor(parser, 'native_visual', persist=lambda event, data: events.append(data),
                                save_image=lambda key, data: assets.setdefault(key, data)).extract(path, 'xlsx')
        assert any(item.get('content', {}).get('text') == '2024 매출 120억' for item in first.elements)
        assert events[-1]['status'] == 'completed' and not first.incomplete
        assert not any(item.get('status') == 'started' for item in events[-1:])
        anchor = next(item for item in first.elements if item.get('_origin') == 'native_image')
        image_path = tmp_path / anchor['_asset_id']
        image_path.write_bytes(assets[anchor['_asset_id']])
        action_events = []
        edited = apply_image_actions({'provider': first.provider, 'elements': first.elements, 'raw': first.raw},
            {anchor['_visual_id']: 'both'}, parser, lambda _: image_path, {},
            lambda event, data: action_events.append(data), None)
        assert any(item.get('content', {}).get('text') == '2024 매출 120억' for item in edited.elements)
        assert action_events[-1]['status'] == 'completed' and not edited.incomplete


@pytest.mark.parametrize('text', ['1.5 배 증가', '2.3 % 상승', '3.5억원', '2.3 million', '1.2 사업 계획'])
def test_ambiguous_decimal_lines_remain_body_without_explicit_heading_style(text):
    assert NativeExtractor._numbered_heading_level(text) is None


def test_explicit_period_numbered_headings_can_be_inferred():
    assert NativeExtractor._numbered_heading_level('1. 사업 개요') == 1
    assert NativeExtractor._numbered_heading_level('1.2. 세부 계획') == 2


def test_unstyled_docx_consecutive_numbered_items_do_not_become_headings(tmp_path):
    from docx import Document
    path = tmp_path / 'list.docx'
    document = Document()
    document.add_paragraph('1. 관내 중소기업')
    document.add_paragraph('2. 창업 3년 이내 기업')
    document.add_paragraph('지원 내용을 확인합니다.')
    document.save(path)
    elements = normalize_elements(NativeExtractor(None, 'native_text').extract(path, 'docx').elements)
    assert all(element['kind'] == 'paragraph' for element in elements)


def test_docx_list_number_style_does_not_become_heading(tmp_path):
    from docx import Document
    path = tmp_path / 'styled-list.docx'
    document = Document()
    document.add_paragraph('1. 지원 대상', style='List Number')
    document.add_paragraph('뒤따르는 본문')
    document.save(path)
    elements = normalize_elements(NativeExtractor(None, 'native_text').extract(path, 'docx').elements)
    assert all(element['kind'] == 'paragraph' for element in elements)


def test_hwpx_numbered_list_properties_do_not_become_headings(tmp_path):
    path = tmp_path / 'list.hwpx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('Contents/content.hpf', '<package/>')
        archive.writestr('Contents/header.xml', '<head><paraPr id="1"><heading type="NUMBER" level="0"/></paraPr></head>')
        archive.writestr('Contents/section0.xml', '<section><p paraPrIDRef="1"><run><t>1. 지원 대상</t></run></p>'
                         '<p><run><t>다음 본문</t></run></p></section>')
    result = NativeExtractor(None, 'native_text').extract(path, 'hwpx')
    assert all(element['kind'] == 'paragraph' for element in normalize_elements(result.elements))


def test_consecutive_numbered_text_lines_are_not_inferred_as_headings():
    assert NativeExtractor._consecutive_numbered_items(['1. 대상', '2. 내용', '일반 문장', '3. 절 제목', '절 내용']) == {0, 1}


def test_hwp_extraction_keeps_measurements_and_numbered_list_as_body(tmp_path, monkeypatch):
    import subprocess

    import olefile
    path = tmp_path / 'sample.hwp'
    path.write_bytes(b'sample')
    def fake_run(command, **kwargs):
        output = command[command.index('--output') + 1]
        from pathlib import Path
        headings = ['1. 사업 개요', '목적 설명', '1.5 배 증가', '2.3 % 상승', '1. 지원 대상', '2. 지원 내용']
        paragraphs = ''.join(f'<Paragraph parashape-id="{0 if i == 0 else 1}" style-id="0"><LineSeg><Text>{text}</Text></LineSeg></Paragraph>' for i, text in enumerate(headings))
        Path(output).write_text('<HwpDoc><DocInfo><IdMappings><ParaShape head-shape="outline" level="0"/>'
            '<ParaShape head-shape="none" level="0"/></IdMappings></DocInfo><BodyText><SectionDef section-id="0">'
            '<ColumnSet>' + paragraphs + '</ColumnSet></SectionDef></BodyText></HwpDoc>', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0)
    class FakeOle:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def listdir(self): return []
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(olefile, 'OleFileIO', lambda *_: FakeOle())
    result = NativeExtractor(None, 'native_text').extract(path, 'hwp')
    elements = normalize_elements(result.elements)
    assert elements[0]['kind'] == 'heading' and elements[0]['heading_level_origin'] == 'parser'
    assert all(element['kind'] == 'paragraph' for element in elements[1:])
    assert all(element['heading_path'] == ['1. 사업 개요'] for element in elements[1:])


def test_visual_timeout_preserves_text_and_stops_later_image_requests(tmp_path):
    path = tmp_path / 'two-images.xlsx'
    book = Workbook()
    book.active['A1'] = '보존할 본문'
    book.active.add_image(XLImage(io.BytesIO(png())), 'B2')
    book.active.add_image(XLImage(io.BytesIO(png())), 'C2')
    book.save(path)
    class TimeoutParser(ImageParser):
        def parse(self, request):
            self.calls += 1
            raise IndexerError('PARSER_SYNC_TIMEOUT', '시간 초과', 503)
    parser, events, progress = TimeoutParser(), [], []
    result = NativeExtractor(parser, 'native_visual', persist=lambda event, data: events.append(data),
                             progress=lambda completed, total: progress.append((completed, total))).extract(path, 'xlsx')
    assert parser.calls == 1 and events[0]['status'] == 'started'
    assert progress[0] == (0, 2)
    assert result.incomplete and any('응답을 확인하지 못해' in warning for warning in result.raw['warnings'])
    assert any('보존할 본문' in item['content'].get('html', '') for item in result.elements)
    assert len([item for item in result.elements if item.get('_origin') == 'native_image']) == 2
    from indexer.parsing.image_actions import apply_image_actions
    actions = {item['_visual_id']: 'exclude' for item in result.elements if item.get('_origin') == 'native_image'}
    reviewed = apply_image_actions({'provider': result.provider, 'elements': result.elements, 'raw': result.raw},
                                   actions, parser, lambda _: path, {}, None, None)
    assert not reviewed.incomplete and not reviewed.raw['warnings'] and parser.calls == 1


@pytest.mark.parametrize('old_status', ['processing', 'failed'])
def test_interrupted_native_visual_recovers_without_repeating_unknown_request(tmp_path, old_status):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService
    path = tmp_path / 'example.xlsx'
    workbook(path)
    parser = ImageParser()
    store = ArtifactStore(tmp_path)
    service = IngestionService(store, parser)
    queued = service.enqueue(path, settings={'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0})
    doc, job = queued['document_id'], queued['job_id']
    status = store.load_job(doc, job, 'processing')
    store.save_job(doc, job, 'processing', {**status, 'status': old_status, 'stage': 'parsing'})
    store.save_provider_event(doc, job, 'initial', 'visual', {'index': 0, 'sha256': hashlib.sha256(png()).hexdigest(), 'status': 'started'})
    service.recover()
    assert store.load_job(doc, job, 'processing')['status'] == 'queued'
    recovered = service.run_job(doc, job)
    assert recovered['pending_review'] and recovered['status'] == 'partial' and parser.calls == 0
    assert any('완료 여부' in message for message in recovered['coverage']['warnings'])
    assert any('개발' in chunk['text'] for chunk in store.load_job(doc, job, 'chunks'))


def test_unknown_checkpoint_prevents_all_new_visual_requests_even_with_gaps(tmp_path):
    path = tmp_path / 'two-images.xlsx'
    book = Workbook()
    book.active['A1'] = '본문'
    book.active.add_image(XLImage(io.BytesIO(png())), 'B2')
    book.active.add_image(XLImage(io.BytesIO(png())), 'C2')
    book.save(path)
    parser = ImageParser()
    checkpoint = {'visuals': [{'index': 1, 'sha256': hashlib.sha256(png()).hexdigest(), 'status': 'started'}]}
    result = NativeExtractor(parser, 'native_visual', checkpoint=checkpoint).extract(path, 'xlsx')
    assert parser.calls == 0 and result.incomplete
    assert len([item for item in result.elements if item.get('_origin') == 'native_image']) == 2
def test_xlsx_chart_preview_is_available_for_review_and_failed_render_is_reported(tmp_path, monkeypatch):
    import indexer.parsing.xlsx_chart_preview as previews
    from indexer.chunking import normalize_elements
    from indexer.parsing.native_documents import NativeExtractor
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    source = tmp_path / 'chart.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '요약'
    sheet.append(['금액'])
    sheet.append([42])
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=1, min_row=1, max_row=2), titles_from_data=True)
    sheet.add_chart(chart, 'D16')
    workbook.save(source)
    workbook.close()
    saved = {}
    monkeypatch.setattr(previews, 'chart_preview', lambda *args: png())
    result = NativeExtractor(ImageParser(), 'native_visual', save_image=lambda key, data: saved.update({key: data})).extract(source, 'xlsx')
    image = next(item for item in result.elements if item.get('_origin') == 'native_image')
    assert image['_image_is_preview'] and image['source_location']['cell'] == 'D16'
    chart_model = result.raw['xlsx_sheets'][0]['charts'][0]
    assert chart_model['kind'] == 'chart' and chart_model['start']['column'] == 4
    assert chart_model['source_path'].startswith('xl/charts/') and 'barChart' in chart_model['definition_xml']
    assert saved[image['_asset_id']] == png()
    assert next(item for item in normalize_elements(result.elements) if item.get('asset_id'))['image_is_preview']
    assert not result.incomplete
    assert not result.raw['warnings']
    assert any('대략적인 미리보기' in item for item in result.raw['limitations'])
    from indexer.ingestion_graph import make_graph
    from indexer.ingestion_service import unresolved_visuals
    state = make_graph(ImageParser()).invoke({'path': str(source), 'document_id': 'd', 'job_id': 'j',
        'settings': {'extraction_mode': 'native_visual', 'target_chars': 400, 'overlap_chars': 0}})
    assert not state['coverage']['provider_incomplete']
    assert not unresolved_visuals(state['normalized'])
    assert state['coverage']['unknown_scope'] and state['coverage']['limitations']
    monkeypatch.setattr(previews, 'chart_preview', lambda *args: None)
    failed = NativeExtractor(None, 'native_text').extract(source, 'xlsx')
    assert failed.incomplete and any('D16' in warning for warning in failed.raw['warnings'])

