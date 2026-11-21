import json
from dataclasses import replace
from datetime import datetime
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient
from indexer.artifact_store import ArtifactStore
from indexer.chat import RagChat
from indexer.config import Config
from indexer.contracts import ChatRequest, IndexerError, SearchRequest
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.embedding_adaptation import split_chunk
from indexer.index_publication import IndexPublisher
from indexer.ingestion_graph import make_graph
from indexer.ingestion_service import IngestionService
from indexer.parsing.contracts import ParseResult
from indexer.parsing.xlsx_analysis import StructureAnalyzer
from indexer.published_retrieval import PublishedRetrieval
from indexer.xlsx_query import QueryPlan, execute
from indexer.xlsx_structure import SheetProposal, validate
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table
from PIL import Image
from qdrant_client import QdrantClient

from apps.api.main import create_app

SETTINGS = {'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0}


def native(path, analyzer=None):
    return make_graph(None, xlsx_analyzer=analyzer).invoke(
        {'path': str(path), 'document_id': 'd', 'job_id': 'j', 'settings': SETTINGS})


def patch_sheet(path, target, change):
    with ZipFile(path) as source, ZipFile(target, 'w') as destination:
        for item in source.infolist():
            value = source.read(item)
            if item.filename == 'xl/worksheets/sheet1.xml':
                value = change(value)
            destination.writestr(item, value)


def test_original_values_and_style_model_keep_types_cache_and_line_breaks(tmp_path):
    path, cached_path = tmp_path / 'source.xlsx', tmp_path / 'cached.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['C3'] = datetime(2024, 11, 21)
    sheet['C3'].number_format = 'dd/mm/yyyy'
    sheet['D3'] = True
    sheet['E3'] = '=1+2'
    sheet['F3'] = '=2+2'
    sheet['G4'] = 'first\nsecond'
    sheet['G4'].font = Font(color='FF121314', bold=True)
    sheet['G4'].fill = PatternFill('solid', fgColor='FF445566')
    sheet['G4'].border = Border(left=Side(style='thin', color='FF778899'))
    sheet['G4'].hyperlink = 'https://example.test'
    sheet.row_dimensions[4].hidden = True
    sheet.column_dimensions.group('D', 'F', hidden=True)
    book.save(path)
    patch_sheet(path, cached_path, lambda data: data.replace(b'<f>1+2</f><v></v>', b'<f>1+2</f><v>3</v>'))
    raw = native(cached_path)['parser_result']
    sheet = raw['xlsx_sheets'][0]
    cells = {item['address']: item for item in sheet['cells']}
    assert cells['C3']['value'] == '2024-11-21T00:00:00' and cells['C3']['date_format'] == 'dd/mm/yyyy'
    assert cells['C3']['raw_value'].isdigit()
    assert cells['D3']['value'] is True
    assert cells['E3']['formula']['text'] == '=1+2' and cells['E3']['value'] is None
    assert cells['E3']['cached_value'] == 3 and cells['E3']['cached_value_type'] == 'integer'
    assert cells['F3']['cached_value'] is None and cells['F3']['display_text'] == '계산값 없음'
    assert cells['G4']['value'] == 'first\nsecond'
    assert cells['G4']['hyperlink']['target'] == 'https://example.test'
    style = raw['xlsx_style_model']['styles'][cells['G4']['style_id']]
    assert all(color in style[key] for color, key in [('FF121314', 'font'), ('FF445566', 'fill'), ('FF778899', 'border')])
    assert sheet['hidden_rows'] == [4] and sheet['hidden_column_ranges'] == [{'start': 4, 'end': 6}]


def test_adjacent_tables_use_registered_ranges_and_keep_absolute_fragments(tmp_path):
    path = tmp_path / 'adjacent.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['C3'], sheet['D3'], sheet['E3'], sheet['F3'] = 'ID', '이름', 'Code', '값'
    sheet['C4'], sheet['D4'], sheet['E4'], sheet['F4'] = 4, '고유 내용\n' * 500, 72, '두 번째'
    sheet.add_table(Table(displayName='First', ref='C3:D4'))
    sheet.add_table(Table(displayName='Second', ref='E3:F4'))
    book.save(path)
    state = native(path)
    regions = state['parser_result']['xlsx_sheets'][0]['regions']
    assert [(item['start'], item['end'], item['origin'], item['status']) for item in regions] == [
        ('C3', 'D4', 'explicit', 'verified'), ('E3', 'F4', 'explicit', 'verified')]
    chunks = [item for item in state['chunks'] if any(cell['address'] == 'D4' for cell in item['table_cells'])]
    original = next(item for item in chunks if '\n' in item['table_segments'][0]['value'])
    split = split_chunk(original, 100)
    assert len(split) > 1
    for item in split:
        refs = item['element_refs'][0]['office_location']['cells']
        assert refs == list(dict.fromkeys(seg['cell'] for seg in item['source_segments']))
        assert all(cell['row'] == 4 and cell['column'] in {3, 4} for cell in item['table_cells'])
        assert all(seg['text'] == part['value'] for seg, part in zip(item['source_segments'], item['table_segments'], strict=True))


def test_unregistered_adjacent_headers_produce_unverified_regions(tmp_path):
    path = tmp_path / 'adjacent.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['ID', '이름', 'Code', '값'])
    sheet.append([1, '가', 2, '나'])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FF111111' if cell.column <= 2 else 'FF333333')
    book.save(path)
    regions = native(path)['parser_result']['xlsx_sheets'][0]['regions']
    assert [(item['start'], item['end'], item['status']) for item in regions] == [
        ('A1', 'B2', 'unverified'), ('C1', 'D2', 'unverified')]


def region(start, end, kind='table', headers=(), **changes):
    return {'start': start, 'end': end, 'kind': kind, 'headers': list(headers),
            'title': None, 'uncertain': False, **changes}


@pytest.mark.parametrize('proposal', [
    {'regions': [region('A1', 'B2')], 'excluded': {}},
    {'regions': [region('A1', 'C1'), region('A2', 'A3')], 'excluded': {}},
    {'regions': [region('A1', 'C3', headers=('A1', 'C1'))], 'excluded': {}},
    {'regions': [region('A1', 'C3'), region('A3', 'C3')], 'excluded': {}},
    {'regions': [region('A1', 'D3')], 'excluded': {}},
])
def test_bad_coordinates_merge_coverage_and_headers_are_rejected(tmp_path, proposal):
    path = tmp_path / 'validate.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'], sheet['C1'], sheet['C3'] = '하나', '둘', '셋'
    sheet.merge_cells('A1:B1')
    book.save(path)
    data = native(path)['parser_result']['xlsx_sheets'][0]
    with pytest.raises(ValueError):
        validate(data, SheetProposal.model_validate(proposal), 'llm')


def test_structure_analysis_is_opt_in_bounded_validated_and_versioned(tmp_path):
    path = tmp_path / 'test.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['ID', '이름'])
    sheet.append([4, '가'])
    book.save(path)
    calls = []
    def model(request):
        payload = json.loads(request.content)
        data = json.loads(payload['messages'][-1]['content'])
        assert data['cells'][0]['address'] == 'A1' and 'merged_range' in data['cells'][0]
        calls.append(payload['model'])
        result = {'regions': [region('A1', 'B2', headers=('A1', 'B1'))], 'excluded': {}}
        if payload['model'] == 'invalid':
            result['regions'][0]['start'] = 'Z99'
        if payload['model'] == 'exclude':
            result['excluded']['B2'] = '외부 모델 제외 제안'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]})
    config = Config(tmp_path, llm_api_key='key', xlsx_structure_enabled=True, xlsx_structure_model='first')
    with httpx.Client(transport=httpx.MockTransport(model)) as http:
        def extract(**changes):
            state = native(path, StructureAnalyzer(replace(config, **changes), http))
            return state['parser_result']['xlsx_sheets'][0]
        assert extract()['analysis_status'] == 'disabled'
        assert extract(external_processing=True)['analysis_status'] == 'analyzed'
        assert extract(external_processing=True)['analysis_status'] == 'cached'
        assert extract(external_processing=True, xlsx_structure_max_input_bytes=20)['analysis_status'] == 'input_limit'
        failed = extract(external_processing=True, xlsx_structure_model='invalid')
        assert failed['analysis_status'] == 'invalid_response'
        assert failed['regions'][0]['status'] == 'unverified'
        pending = extract(external_processing=True, xlsx_structure_model='exclude')
        assert pending['analysis_status'] == 'review_required'
        assert not pending['excluded'] and pending['suggested_review']['excluded'] == {'B2': '외부 모델 제외 제안'}
    assert calls == ['first', 'invalid', 'exclude']


def fixture_tables(path):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Roster'
    sheet.append(['ID', '이름', '날짜', '그룹'])
    for index in range(1, 17):
        sheet.append([index, f'이름{index}', datetime(2024, 1, index), 'A' if index < 10 else 'B'])
    sheet['D17'] = '=D16'
    sheet.row_dimensions[9].hidden = True
    sheet.add_table(Table(displayName='RosterTable', ref='A1:D17'))
    other = book.create_sheet('Linked')
    other.append(['번호', '성명'])
    other.append([2, '이름2'])
    other.append([3, '다른 이름'])
    other.add_table(Table(displayName='LinkedTable', ref='A1:B3'))
    book.save(path)


def test_full_table_aggregates_hidden_rows_missing_caches_and_typed_filters(tmp_path):
    path = tmp_path / 'tables.xlsx'
    fixture_tables(path)
    raw = native(path)['parser_result']
    count = execute(raw, QueryPlan(sheet='Roster', start='A1', operation='count', include_hidden_rows=False))
    assert count['complete'] and count['result']['matched_rows'] == 15
    assert count['scope']['scanned_rows'] == 15 and count['scope']['hidden_rows_excluded'] == 1
    distribution = execute(raw, QueryPlan(sheet='Roster', start='A1', operation='distribution', column='D'))
    assert not distribution['complete'] and distribution['issues'] == {'missing_cache': 1}
    assert distribution['result']['distribution'] == [
        {'type': 'string', 'value': 'A', 'count': 9}, {'type': 'string', 'value': 'B', 'count': 6}]
    dates = execute(raw, QueryPlan(sheet='Roster', start='A1', operation='date_range', column='C',
                                  filters=[{'column': 'A', 'operator': 'gt', 'value': 3}]))
    assert dates['result']['min'] == '2024-01-04T00:00:00'
    assert dates['result']['max'] == '2024-01-16T00:00:00'
    for column, value in [('Z', 3), ('A', '3')]:
        with pytest.raises(ValueError):
            execute(raw, QueryPlan(sheet='Roster', start='A1', operation='count',
                                  filters=[{'column': column, 'operator': 'eq', 'value': value}]))
    linked, conflict = raw['xlsx_row_relations']
    assert linked['left']['cells'] == ['A3', 'B3'] and linked['right']['cells'] == ['A2', 'B2']
    assert linked['status'] == 'unverified' and conflict['conflict']


def test_xlsx_review_search_evidence_and_aggregate_use_published_job(tmp_path):
    path = tmp_path / 'tables.xlsx'
    fixture_tables(path)
    calls = []
    def remote(request):
        payload = json.loads(request.content)
        if request.url.path.endswith('/chat/completions'):
            calls.append(payload)
            plan = {'sheet': 'Roster', 'start': 'A1', 'operation': 'count'}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'plan': plan})}}]})
        return httpx.Response(200, json={'model': payload['model'], 'data': [
            {'index': index, 'embedding': [1.0, 0.5]} for index in range(len(payload['input']))]})
    with httpx.Client(transport=httpx.MockTransport(remote)) as http:
        qdrant = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(
            http, 'https://example.test/v1', 'fake', EmbeddingProfile('p', 'doc', 'query', 2)))
        storage = ArtifactStore(tmp_path)
        service = IngestionService(storage, object(), publisher)
        first = service.ingest(path, settings=SETTINGS)
        assert first['status'] == 'completed'
        doc, job, version = (first[key] for key in ('document_id', 'job_id', 'index_version'))
        prefix = f'/v1/ingest/{doc}/jobs/{job}'
        config = Config(tmp_path, access_key='key', external_processing=True, llm_api_key='fake')
        with TestClient(create_app(config, ingestion_service=service), base_url='https://testserver') as client:
            login = client.post('/v1/auth/login', json={'access_key': 'key'}, headers={'Origin': 'https://testserver'})
            client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
            hit = client.post('/v1/retrieve', json={'query': '이름16', 'method': 'bm25'}).json()['results'][0]
            assert hit['source_refs'][-1]['start'] == 'A17'
            evidence = client.get(hit['evidence_urls'][0]).json()['cells']
            assert {cell['address'] for cell in evidence} == {'A17', 'B17', 'C17', 'D17'}
            aggregate = client.post('/v1/xlsx-query', json={'document_id': doc, 'index_version': version,
                                    'sheet': 'Roster', 'start': 'A1', 'operation': 'count'})
            assert aggregate.json()['result']['matched_rows'] == 16
            assert client.get(prefix + '/xlsx-structure').json()['xlsx_structure_version'] == 2
            reviewed = client.post(prefix + '/xlsx-structure', json={'Roster': {
                'regions': [region('A1', 'D17', headers=('A1', 'B1', 'C1', 'D1'))],
                'excluded': {'D17': '검색에서 제외하는 수식'}}})
            assert reviewed.status_code == 202, reviewed.text
            new_job = reviewed.json()['job_id']
            assert new_job != job
            # The worker owns the queued job in the app. Re-open its persisted result after completion.
            from time import monotonic, sleep
            deadline = monotonic() + 5
            while monotonic() < deadline:
                status = storage.load_job(doc, new_job, 'processing')
                if status['status'] not in {'queued', 'processing'}:
                    break
                sleep(0.02)
            updated = client.get(f'/v1/ingest/{doc}/jobs/{new_job}/xlsx-structure').json()
            assert updated['xlsx_sheets'][0]['excluded'] == {'D17': '검색에서 제외하는 수식'}
            assert publisher.active()['version'] == version and status['can_accept_content_changes']
        retrieval = PublishedRetrieval(publisher, storage)
        chat = RagChat(config, retrieval, http).invoke(ChatRequest(message='전체 개수는?', document_id=doc,
                                                                    index_version=version, xlsx_full_table=True))
        assert chat['xlsx_aggregate']['result']['matched_rows'] == 16
        assert '이름16' not in calls[0]['messages'][1]['content']
        assert chat['sources'][0]['source_refs'][0]['end'] == 'D17'
        qdrant.close()


def test_legacy_xlsx_search_rechunk_and_reparse_preserve_historical_version(tmp_path):
    path = tmp_path / 'old.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['이름', '점수'])
    sheet.append(['하늘', 20])
    sheet.add_table(Table(displayName='Legacy', ref='A1:B2'))
    book.save(path)

    class LegacyParser:
        def parse(self, request):
            table = {'id': 1, 'page': None, 'category': 'table',
                     'content': {'html': '<table><tr><td>이름</td><td>점수</td></tr><tr><td>하늘</td><td>20</td></tr></table>'},
                     '_origin': 'native_extraction', '_layout': {'space': 'xlsx_region', 'row_offset': 0, 'column_offset': 0},
                     'source_location': {'kind': 'xlsx_range', 'sheet': 'Sheet', 'start': 'A1', 'end': 'B2'}}
            return ParseResult('native_xlsx', [table], {})

    def remote(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={'model': payload['model'], 'data': [
            {'index': index, 'embedding': [1.0, 0.5]} for index in range(len(payload['input']))]})

    with httpx.Client(transport=httpx.MockTransport(remote)) as http:
        qdrant = QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(
            http, 'https://example.test/v1', 'key', EmbeddingProfile('p', 'doc', 'query', 2)))
        storage = ArtifactStore(tmp_path)
        service = IngestionService(storage, LegacyParser(), publisher)
        old_settings = {**SETTINGS, 'extraction_mode': 'parser'}
        first = service.ingest(path, settings=old_settings)
        doc, job, old_version = (first[key] for key in ('document_id', 'job_id', 'index_version'))
        manifest_path = publisher.root / old_version / 'chunks.json'
        old_index = manifest_path.read_bytes()
        retrieval = PublishedRetrieval(publisher, storage)
        request = SearchRequest(query='하늘', method='bm25', index_version=old_version)
        assert retrieval.search(request)['results'][0]['job_id'] == job
        with pytest.raises(IndexerError, match='게시된 XLSX 구조'):
            retrieval.xlsx_catalog(doc, old_version)
        queued = service.enqueue_reprocess(doc, job, 'rechunk', old_settings)
        reused = service.run_job(doc, queued['job_id'])
        assert reused['status'] == 'completed'
        assert not storage.load_job(doc, reused['job_id'], 'parser_result')
        queued = service.enqueue_reprocess(doc, reused['job_id'], 'reparse', SETTINGS)
        upgraded = service.run_job(doc, queued['job_id'])
        assert upgraded['status'] == 'completed'
        assert retrieval.xlsx_catalog(doc)['tables'][0]['start'] == 'A1'
        assert retrieval.search(request)['results'][0]['job_id'] == job
        assert manifest_path.read_bytes() == old_index
        qdrant.close()


def test_embedded_image_matches_absolute_cell_after_region_split_and_embedding(tmp_path):
    path, image = tmp_path / 'offsets.xlsx', tmp_path / 'image.png'
    Image.new('RGB', (8, 8), 'white').save(image)
    book = Workbook()
    sheet = book.active
    sheet['E5'], sheet['F5'] = '분류', '설명'
    sheet['E6'], sheet['F6'] = '새로운 항목', '검색할 값 ' * 100
    sheet.add_table(Table(displayName='Offset', ref='E5:F6'))
    sheet.add_image(ExcelImage(image), 'F6')
    book.save(path)
    state = native(path)
    found = next(item for item in state['normalized'] if item.get('xlsx_region'))
    anchor = next(item for item in state['normalized'] if item['kind'] == 'image')
    assert found['related_images'][0]['element_id'] == anchor['element_id']
    original = next(item for item in state['chunks'] if any(cell['address'] == 'F6' for cell in item['table_cells']))
    fragments = split_chunk(original, 110)
    from indexer.visual_relations import xlsx_anchor_matches

    assert all(xlsx_anchor_matches(anchor['layout'], item['table_cells']) for item in fragments)
    assert not xlsx_anchor_matches(anchor['layout'], [{'row': 2, 'column': 2}])


def test_reviewed_title_uses_body_context_and_keeps_address(tmp_path):
    path = tmp_path / 'title.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['D1'], sheet['D2'], sheet['E2'] = '매출 현황', 'ID', '결과'
    sheet['D3'], sheet['E3'] = 1, '정상'
    book.save(path)
    review = {'Sheet': {'regions': [
        region('D1', 'D1', 'title', title='D1'),
        region('D2', 'E3', headers=('D2', 'E2')),
    ], 'excluded': {}}}
    state = make_graph(None).invoke({'path': str(path), 'document_id': 'd', 'job_id': 'j',
                                    'settings': SETTINGS, 'xlsx_review': review})
    title = next(item for item in state['normalized'] if item['kind'] == 'title')
    assert not any(title['element_id'] in chunk['element_ids'] for chunk in state['chunks'])
    body = next(chunk for chunk in state['chunks'] if '정상' in chunk['text'])
    assert '매출 현황' in body['text'] and title['element_id'] in body['context_element_ids']
