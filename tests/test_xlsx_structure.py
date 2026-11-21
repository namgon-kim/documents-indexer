import json
import os
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
from indexer.xlsx_query import PublishedTableQuery, QueryPlan, execute, verified_tables
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
        analyzed = extract(external_processing=True)
        assert analyzed['analysis_status'] == 'analyzed'
        proposal = analyzed['regions'][0]
        assert proposal['verification'] == 'llm_validated' and proposal['status'] == 'unverified'
        # Old published artifacts may contain the pre-fix status.
        proposal['status'] = 'verified'
        raw = {'xlsx_sheets': [analyzed], 'xlsx_structure_version': 2}
        assert verified_tables(raw) == []
        with pytest.raises(ValueError, match='사용자가 확인한 표'):
            execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='count'))
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
        endpoint = os.getenv('TEST_QDRANT_URL')
        qdrant = QdrantClient(url=endpoint) if endpoint else QdrantClient(':memory:')
        publisher = IndexPublisher(tmp_path, qdrant, EmbeddingClient(
            http, 'https://example.test/v1', 'fake', EmbeddingProfile('p', 'doc', 'query', 2)))
        storage = ArtifactStore(tmp_path)
        service = IngestionService(storage, object(), publisher)
        config = Config(tmp_path, access_key='key', external_processing=True, llm_api_key='fake')
        with TestClient(create_app(config, ingestion_service=service), base_url='https://testserver') as client:
            login = client.post('/v1/auth/login', json={'access_key': 'key'}, headers={'Origin': 'https://testserver'})
            client.headers.update({'Origin': 'https://testserver', 'X-CSRF-Token': login.json()['csrf_token']})
            upload = client.post('/v1/ingest', files={'file': (path.name, path.read_bytes())},
                                 data={'settings': json.dumps(SETTINGS)})
            assert upload.status_code == 200, upload.text
            first = upload.json()
            assert first['status'] == 'completed'
            doc, job, version = (first[key] for key in ('document_id', 'job_id', 'index_version'))
            prefix = f'/v1/ingest/{doc}/jobs/{job}'
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
            updated = client.get(f'/v1/ingest/{doc}/jobs/{new_job}/xlsx-structure', params={'sheet': 'Roster'}).json()
            assert updated['excluded'] == {'D17': '검색에서 제외하는 수식'}
            assert publisher.active()['version'] == version and status['can_accept_content_changes']
            matches = client.get(f'/v1/ingest/{doc}/xlsx-join-candidates', params={
                'index_version': version, 'sheet': 'Roster', 'start': 'A1', 'right_sheet': 'Linked', 'right_start': 'A1'})
            assert matches.status_code == 200
            pair = next(item for item in matches.json()['candidates'] if not item['ambiguous'])
            assert pair['left']['values'] == ['2', '이름2']
            joined = client.post('/v1/xlsx-query', json={'document_id': doc, 'index_version': version,
                'sheet': 'Roster', 'start': 'A1', 'operation': 'sum', 'column': 'A', 'column_side': 'right',
                'join': {'sheet': 'Linked', 'start': 'A1', 'approved_pairs': [{'left_row': 3, 'right_row': 2}]}})
            assert joined.status_code == 200
            assert joined.json()['result']['value'] == 2
            assert {ref['sheet'] for ref in joined.json()['source_ranges']} == {'Roster', 'Linked'}
            published = client.post(f'/v1/ingest/{doc}/jobs/{new_job}/accept-content-changes', json={})
            assert published.status_code == 200, published.text
            previous = client.get(hit['evidence_urls'][0]).json()['cells']
            assert previous == evidence and publisher.active()['version'] != version
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


def test_empty_merge_and_adjacent_registered_table_stay_readable(tmp_path):
    path = tmp_path / 'empty-merge.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.merge_cells('A1:B2')
    sheet['A3'], sheet['B3'] = 'ID', '이름'
    sheet['A3'].font = sheet['B3'].font = Font(bold=True)
    sheet['A4'], sheet['B4'] = 1, '존재하는 값'
    sheet['C1'], sheet['D1'], sheet['C2'], sheet['D2'] = 'Code', 'Name', 2, 'Registered'
    sheet.add_table(Table(displayName='Adjacent', ref='C1:D2'))
    book.save(path)
    state = native(path)
    data = state['parser_result']['xlsx_sheets'][0]
    assert data['merged_ranges'] == ['A1:B2']
    assert [item['start'] for item in data['regions'] if item['verification'] == 'explicit'] == ['C1']
    assert any('존재하는 값' in chunk['text'] for chunk in state['chunks'])


def test_numeric_aggregates_after_manual_review_and_missing_data(tmp_path):
    path = tmp_path / 'ordinary.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['그룹', '금액'])
    sheet.append(['A', 1.25])
    sheet.append([])
    sheet.append(['A', 2.75])
    sheet.append(['B', 9])
    sheet.append(['B', '=10+5'])
    sheet.append(['B', 20])
    sheet.row_dimensions[5].hidden = True
    book.save(path)
    raw = native(path)['parser_result']
    assert verified_tables(raw) == []
    data = raw['xlsx_sheets'][0]
    data['regions'] = validate(data, SheetProposal.model_validate({
        'regions': [region('A1', 'B7', headers=('A1', 'B1'))], 'excluded': {'B7': '검토에서 제외'}}), 'user')
    data['excluded'] = {'B7': '검토에서 제외'}
    common = {'sheet': 'Sheet', 'start': 'A1', 'include_hidden_rows': False}
    count = execute(raw, QueryPlan(**common, operation='count'))
    assert count['result']['matched_rows'] == 4 and count['scope']['scanned_rows'] == 5
    for op, expected in [('sum', 4), ('average', 2), ('min', 1.25), ('max', 2.75)]:
        summary = execute(raw, QueryPlan(**common, operation=op, column='B', group_by='A'))
        assert summary['result']['value'] == expected and summary['result']['numeric_values'] == 2
        assert summary['result']['groups'][0]['value'] == expected
        assert summary['scope']['verification'] == 'user_verified'
        assert summary['issues'] == {'missing_cache': 1, 'excluded_cell': 1}
        assert not summary['complete']


def test_duplicates_and_name_conflicts_cannot_be_joined(tmp_path):
    path = tmp_path / 'join.xlsx'
    book = Workbook()
    for i, names in enumerate([('First', [(1, '가'), (2, '나'), (2, '나'), (3, '다')]),
                               ('Second', [(1, '가'), (2, '나'), (3, '다른 이름')])]):
        sheet = book.active if not i else book.create_sheet()
        sheet.title = names[0]
        sheet.append(['ID', 'Name'])
        for row in names[1]:
            sheet.append(row)
        sheet.add_table(Table(displayName=f'Table{i}', ref=f'A1:B{sheet.max_row}'))
    book.save(path)
    raw = native(path)['parser_result']
    def plan(row):
        return PublishedTableQuery.model_validate({
            'document_id': 'bb58ab7a-806e-4130-818b-f78eda55d3ac',
            'index_version': '7db28131-ad86-4c34-8a8c-6dcd2f16d23e',
            'sheet': 'First', 'start': 'A1', 'operation': 'rows',
            'filters': [{'column': 'B', 'side': 'right', 'operator': 'eq', 'value': '가'}],
            'join': {'sheet': 'Second', 'start': 'A1', 'approved_pairs': [row]}})
    for row in [{'left_row': 3, 'right_row': 3}, {'left_row': 5, 'right_row': 4}]:
        with pytest.raises(ValueError, match='충돌'):
            execute(raw, plan(row))
    output = execute(raw, plan({'left_row': 2, 'right_row': 2}))
    assert output['result']['rows'][0]['right_cells']['A']['source'] == 'A2'
    assert output['join']['ambiguous_pairs'] == 3 and not output['complete']


def test_bounded_grid_and_cross_sheet_formula_references(tmp_path):
    from indexer.parsing.native_documents import NativeExtractor
    from indexer.xlsx_review import cell_window, sheet_detail

    path = tmp_path / 'grid.xlsx'
    book = Workbook()
    sheet = book.active
    for _ in range(110):
        sheet.append(['long ' * 100] * 50)
    named = book.create_sheet("Other's sheet")
    named['B2'] = 7
    sheet['B2'] = "=SUM('Other''s sheet'!$B$2:$B$3)"
    book.save(path)
    raw = NativeExtractor(None, 'native_text').extract(path, 'xlsx').raw
    data = raw['xlsx_sheets'][0]
    assert not {'cells', 'hidden_merged_cells'}.intersection(sheet_detail(data))
    preview = cell_window(raw, data, 1, 1, 10, 8)['cells']
    assert len(preview) == 80 and max(len(item['display_text']) for item in preview) == 200
    assert cell_window(raw, data, 1, 1, 10, 8, exact='B2')['cells'][0]['formula_references'] == [
        {'sheet': "Other's sheet", 'start': 'B2', 'end': 'B3'}]


def test_xlsx_title_with_note_is_context_with_unverified_relationship(tmp_path):
    path = tmp_path / 'report.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'], sheet['A2'] = '매출', '매출은 세전 기준'
    sheet['A4'], sheet['B4'], sheet['A5'], sheet['B5'] = 'ID', '금액', 1, 20
    book.save(path)
    review = {'Sheet': {'regions': [
        region('A1', 'A1', 'title', title='A1'), region('A2', 'A2', 'text'),
        region('A4', 'B5', headers=('A4', 'B4')),
    ], 'excluded': {}}}
    state = make_graph(None).invoke({'path': str(path), 'document_id': 'd', 'job_id': 'j',
                                    'settings': SETTINGS, 'xlsx_review': review})
    title = next(chunk for chunk in state['chunks'] if chunk['kind'] == 'title')
    data = next(chunk for chunk in state['chunks'] if any(item['address'] == 'B5' for item in chunk['table_cells']))
    assert title['element_ids'][0] in data['context_element_ids']
    assert data['relations'][0]['status'] == 'unverified' and not data['relations'][0]['adjacent']
