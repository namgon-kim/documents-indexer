"""XLSX region separation, layered headers, display values, filters, joins and reprocessing."""

from datetime import datetime, timedelta
from zipfile import ZipFile

import pytest
from indexer.contracts import IndexerError
from indexer.ingestion_graph import make_graph
from indexer.ingestion_service import IngestionService
from indexer.office_inspection import SheetLimits, inspect_office
from indexer.parsing.native_documents import NativeExtractor, reset_xlsx_review
from indexer.parsing.xlsx_format import render
from indexer.xlsx_query import PublishedTableQuery, QueryPlan, execute, verified_tables
from indexer.xlsx_structure import SheetProposal, validate
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.table import Table

SETTINGS = {'extraction_mode': 'native_text', 'target_chars': 400, 'overlap_chars': 0}
DOCUMENT = 'bb58ab7a-806e-4130-818b-f78eda55d3ac'
VERSION = '7db28131-ad86-4c34-8a8c-6dcd2f16d23e'


def native(path, review=None):
    state = {'path': str(path), 'document_id': 'd', 'job_id': 'j', 'settings': SETTINGS}
    if review:
        state['xlsx_review'] = review
    return make_graph(None).invoke(state)


def region(start, end, kind='table', headers=(), **changes):
    return {'start': start, 'end': end, 'kind': kind, 'headers': list(headers),
            'title': None, 'uncertain': False, **changes}


def bold_row(sheet, row, values, column=1):
    for offset, value in enumerate(values):
        cell = sheet.cell(row, column + offset, value)
        cell.font = Font(bold=True)


def spans(raw):
    return [(item['start'], item['end'], item['kind']) for item in raw['xlsx_sheets'][0]['regions']]


def patch_sheet(path, target, change):
    with ZipFile(path) as source, ZipFile(target, 'w') as destination:
        for item in source.infolist():
            value = source.read(item)
            if item.filename == 'xl/worksheets/sheet1.xml':
                value = change(value)
            destination.writestr(item, value)


# X1 — independent tables


def test_tables_of_different_widths_and_positions_are_separate_regions(tmp_path):
    path = tmp_path / 'blocks.xlsx'
    book = Workbook()
    sheet = book.active
    bold_row(sheet, 1, ['이름', '부서', '점수'])
    for row, values in enumerate([('가', '개발', 10), ('나', '기획', 20), ('다', '운영', 30), ('라', '개발', 40)], 2):
        for col, value in enumerate(values, 1):
            sheet.cell(row, col, value)
    bold_row(sheet, 8, ['코드', '항목', '수량', '단가', '비고'])
    for row in range(9, 13):
        for col, value in enumerate([f'C{row}', '부품', row, row * 100, '정상'], 1):
            sheet.cell(row, col, value)
    bold_row(sheet, 1, ['월', '매출'], column=7)
    for row in range(2, 5):
        sheet.cell(row, 7, f'{row}월')
        sheet.cell(row, 8, row * 10)
    book.save(path)
    raw = native(path)['parser_result']
    assert spans(raw) == [('A1', 'C5', 'table'), ('G1', 'H4', 'table'), ('A8', 'E12', 'table')]
    regions = raw['xlsx_sheets'][0]['regions']
    assert all(item['status'] == 'unverified' for item in regions)
    assert regions[2]['headers'] == ['A8', 'B8', 'C8', 'D8', 'E8']


def test_blank_rows_inside_one_table_and_a_title_above_it_stay_together(tmp_path):
    path = tmp_path / 'one-table.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'] = '매출 현황'
    sheet['A1'].font = Font(bold=True)
    bold_row(sheet, 3, ['지점', '금액', '건수'])
    for row, values in {4: ('서울', 10, 1), 5: ('부산', 20, 2), 7: ('대구', 30, 3), 8: ('광주', 40, 4)}.items():
        for col, value in enumerate(values, 1):
            sheet.cell(row, col, value)
    book.save(path)
    raw = native(path)['parser_result']
    assert spans(raw) == [('A1', 'C8', 'table')]
    table = raw['xlsx_sheets'][0]['regions'][0]
    assert table['title'] == 'A1' and table['headers'] == ['A3', 'B3', 'C3']


def test_a_new_header_after_a_blank_row_starts_a_new_table(tmp_path):
    path = tmp_path / 'stacked.xlsx'
    book = Workbook()
    sheet = book.active
    for start in (1, 5):
        bold_row(sheet, start, ['항목', '값'])
        sheet.cell(start + 1, 1, '가')
        sheet.cell(start + 1, 2, start)
        sheet.cell(start + 2, 1, '나')
        sheet.cell(start + 2, 2, start + 1)
    book.save(path)
    raw = native(path)['parser_result']
    assert spans(raw) == [('A1', 'B3', 'table'), ('A5', 'B7', 'table')]
    assert {'axis': 'row', 'after': 3, 'reason': 'separate_block'} in raw['xlsx_sheets'][0]['boundary_candidates']


# X2 — inferred context in chunks


def test_inferred_headers_are_search_context_without_becoming_confirmed(tmp_path):
    path = tmp_path / 'plain.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['사번', '연봉'])
    sheet.append(['E01', 4200])
    sheet.append(['E02', 5100])
    book.save(path)
    state = native(path)
    raw = state['parser_result']
    assert raw['xlsx_sheets'][0]['regions'][0]['headers'] == ['A1', 'B1']
    assert verified_tables(raw) == []
    with pytest.raises(ValueError):
        execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='count'))
    chunk = next(item for item in state['chunks'] if any(s['address'] == 'B3' for s in item.get('table_segments', [])))
    segment = next(s for s in chunk['table_segments'] if s['address'] == 'B3')
    assert '연봉 / 5100' in chunk['text'] and segment['label'] == '연봉'
    assert segment['label_relations'][0]['status'] == 'unverified'
    assert chunk['quality'] == 'unverified' and chunk['xlsx_context_status'] == 'unverified'
    assert 'B1' in chunk['xlsx_context_cells']


# X4 — layered and merged headers, merged group labels


def layered_workbook(path):
    book = Workbook()
    sheet = book.active
    sheet['A1'], sheet['B1'], sheet['B2'], sheet['C2'] = '구분', '2023', '상반기', '하반기'
    sheet.merge_cells('A1:A2')
    sheet.merge_cells('B1:C1')
    sheet['A3'], sheet['B3'], sheet['C3'], sheet['B4'], sheet['C4'] = '서울', 10, 20, 30, 40
    sheet.merge_cells('A3:A4')
    sheet['A5'], sheet['B5'], sheet['C5'] = '부산', 5, 6
    book.save(path)


def test_layered_headers_and_merged_group_labels_keep_column_meaning(tmp_path):
    path = tmp_path / 'layered.xlsx'
    layered_workbook(path)
    review = {'Sheet': {'regions': [region('A1', 'C5', headers=('A1', 'B1', 'B2', 'C2'))], 'excluded': {}}}
    state = native(path, review)
    raw = state['parser_result']
    assert verified_tables(raw)[0]['columns'] == {'A': '구분', 'B': '2023 > 상반기', 'C': '2023 > 하반기'}
    plan = QueryPlan(sheet='Sheet', start='A1', operation='sum', column='C',
                     filters=[{'column': 'A', 'operator': 'eq', 'value': '서울'}])
    result = execute(raw, plan)
    assert result['result']['value'] == 60 and result['complete']
    assert result['scope']['first_data_row'] == 3 and result['scope']['merged_group_cells'] == 1
    rows = execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='rows'))['result']['rows']
    assert rows[1]['cells']['A'] == {'value': '서울', 'type': 'string', 'source': 'A4', 'issue': None, 'merged_from': 'A3'}
    segment = next(s for c in state['chunks'] for s in c.get('table_segments', []) if s['address'] == 'B4')
    assert segment['label'] == '구분 서울 / 2023 > 상반기'
    assert [(rel['kind'], rel['address'], rel['status']) for rel in segment['label_relations']] == [
        ('group', 'A3', 'verified'), ('column', 'B1', 'verified'), ('column', 'B2', 'verified')]
    # In the same chunk the context text is written once, but every cell keeps the relation.
    other = next(s for c in state['chunks'] for s in c.get('table_segments', []) if s['address'] == 'C4')
    assert other['label'] == '2023 > 하반기'
    assert ('group', 'A3') in [(rel['kind'], rel['address']) for rel in other['label_relations']]


def test_group_context_follows_each_cell_into_split_chunks(tmp_path):
    from indexer.embedding_adaptation import split_chunk

    path = tmp_path / 'groups.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['지역', '상반기 판매 실적', '하반기 판매 실적', '연간 판매 실적', '분기 평균 실적', '전년 대비 증감'])
    sheet.append(['서울특별시', 10, 20, 30, 7, 8])
    sheet.append([None, 40, 50, 90, 22, 23])
    sheet.merge_cells('A2:A3')
    book.save(path)
    state = native(path)
    chunk = next(c for c in state['chunks'] if any(s['address'] == 'D3' for s in c.get('table_segments', [])))
    labels = {s['address']: s['label'] for s in chunk['table_segments']}
    assert labels == {'B3': '지역 서울특별시 / 상반기 판매 실적', 'C3': '하반기 판매 실적', 'D3': '연간 판매 실적',
                      'E3': '분기 평균 실적', 'F3': '전년 대비 증감'}
    # 144 bytes hold the context once with four cells; the fifth cell starts a new piece.
    pieces = split_chunk(chunk, 144)
    assert [[s['address'] for s in piece['table_segments']] for piece in pieces] == [['B3', 'C3', 'D3', 'E3'], ['F3']]
    for piece in pieces:
        first = piece['table_segments'][0]
        assert first['label'].startswith('지역 서울특별시 / ') and '지역 서울특별시' in piece['text']
        assert all(('group', 'A2') in [(rel['kind'], rel['address']) for rel in s['label_relations']]
                   for s in piece['table_segments'])


def test_layered_headers_are_inferred_from_merges_and_rejected_when_columns_share_a_leaf(tmp_path):
    path = tmp_path / 'layered.xlsx'
    layered_workbook(path)
    data = native(path)['parser_result']['xlsx_sheets'][0]
    inferred = data['regions'][0]
    assert inferred['headers'] == ['A1', 'B1', 'B2', 'C2'] and inferred['status'] == 'unverified'
    with pytest.raises(ValueError, match='각 열에 하나씩'):
        validate(data, SheetProposal.model_validate({'regions': [region('A1', 'C5', headers=('A1', 'B1'))],
                                                     'excluded': {}}), 'user')
    with pytest.raises(ValueError, match='헤더 행'):
        validate(data, SheetProposal.model_validate({'regions': [region('A1', 'C5', headers=('A1', 'B1', 'B2'))],
                                                     'excluded': {}}), 'user')


def test_a_long_group_context_is_counted_once_when_sizing_chunks(tmp_path):
    path = tmp_path / 'long-group.xlsx'
    group = '영업본부' * 20
    book = Workbook()
    sheet = book.active
    sheet.append(['지역', *(f'Q{index}' for index in range(1, 9))])
    sheet.append([group, *range(10, 18)])
    sheet.append([None, *range(20, 28)])
    sheet.merge_cells('A2:A3')
    book.save(path)
    chunks = [c for c in native(path)['chunks'] if any(s['row'] == 3 for s in c.get('table_segments', []))]
    # Eight copies of the context would exceed the 400-character target; one copy fits.
    assert len(chunks) == 1
    segments = chunks[0]['table_segments']
    assert [s['address'] for s in segments] == [f'{column}3' for column in 'BCDEFGHI']
    assert segments[0]['label'] == f'지역 {group} / Q1'
    assert [s['label'] for s in segments[1:]] == [f'Q{index}' for index in range(2, 9)]
    assert chunks[0]['text'].count(group) == 1


# X5 — display values


def test_display_values_follow_common_excel_formats_without_changing_values(tmp_path):
    path = tmp_path / 'formats.xlsx'
    book = Workbook()
    sheet = book.active
    entries = [('A1', 0.253, '0.0%'), ('B1', 1234567.891, '#,##0.00'), ('C1', datetime(2024, 3, 1), 'mm-dd-yy'),
               ('D1', timedelta(hours=26, minutes=5), '[h]:mm'), ('E1', -1500, '#,##0;(#,##0)'),
               ('F1', 0.1 + 0.2, 'General'), ('G1', 45, '"₩"#,##0'),
               ('H1', datetime(2024, 3, 1, 14, 5), 'yyyy"년" m"월" d"일" h:mm AM/PM'), ('I1', 12345, '0.00E+00')]
    for key, value, mask in entries:
        sheet[key] = value
        sheet[key].number_format = mask
    book.save(path)
    cells = {cell['address']: cell for cell in native(path)['parser_result']['xlsx_sheets'][0]['cells']}
    assert {key: cells[key]['display_text'] for key in cells} == {
        'A1': '25.3%', 'B1': '1,234,567.89', 'C1': '2024-03-01', 'D1': '26:05', 'E1': '(1,500)', 'F1': '0.3',
        'G1': '₩45', 'H1': '2024년 3월 1일 2:05 PM', 'I1': '12345'}
    assert cells['A1']['value'] == 0.253 and cells['A1']['value_category'] == 'percent_fraction'
    assert cells['D1']['value_type'] == 'duration' and cells['D1']['value_category'] == 'duration_seconds'
    assert cells['I1']['display_kind'] == 'unsupported_format' and cells['I1']['value'] == 12345
    assert render(5, 'integer', '000-0000') == ('000-0005', 'formatted', '000-0000')
    assert render(0.5, 'number', '#.##')[0] == '.5'


def test_catalog_describes_stored_value_kinds_for_typed_filters(tmp_path):
    path = tmp_path / 'rates.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['지점', '달성률'])
    for name, rate in [('서울', 0.25), ('부산', 0.4)]:
        sheet.append([name, rate])
        sheet.cell(sheet.max_row, 2).number_format = '0%'
    sheet.add_table(Table(displayName='Rates', ref='A1:B3'))
    book.save(path)
    raw = native(path)['parser_result']
    assert verified_tables(raw)[0]['column_formats'] == {'A': 'string', 'B': 'percent_fraction'}
    count = execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='count',
                                   filters=[{'column': 'B', 'operator': 'gte', 'value': 0.25}]))
    assert count['result']['matched_rows'] == 2 and count['complete']


# X6 — filters


def status_table(path):
    book = Workbook()
    sheet = book.active
    sheet.append(['상태', '금액', '지역'])
    for row in [('완료', 0.1 + 0.2, '서울'), (None, 0.3, '서울 '), ('진행', 1, 'Seoul'), ('완료', 2, '부산')]:
        sheet.append(row)
    sheet.add_table(Table(displayName='Status', ref='A1:C5'))
    book.save(path)
    return native(path)['parser_result']


def test_filters_separate_blank_unknown_and_precision_cases(tmp_path):
    raw = status_table(tmp_path / 'status.xlsx')

    def count(rule):
        return execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='count', filters=[rule]))

    different = count({'column': 'A', 'operator': 'ne', 'value': '완료'})
    assert different['result']['matched_rows'] == 1 and different['unknown_filter_rows'] == 1
    assert different['issues'] == {'blank_filter_value': 1} and not different['complete']
    included = count({'column': 'A', 'operator': 'ne', 'value': '완료', 'blanks': 'include'})
    assert included['result']['matched_rows'] == 2 and included['complete']
    excluded = count({'column': 'A', 'operator': 'ne', 'value': '완료', 'blanks': 'exclude'})
    assert excluded['result']['matched_rows'] == 1 and excluded['complete']
    equal = count({'column': 'A', 'operator': 'eq', 'value': '완료'})
    assert equal['result']['matched_rows'] == 2 and equal['complete']
    precise = count({'column': 'B', 'operator': 'eq', 'value': 0.3})
    assert precise['result']['matched_rows'] == 2 and precise['complete']
    variant = count({'column': 'C', 'operator': 'eq', 'value': '서울'})
    assert variant['result']['matched_rows'] == 1 and variant['issues'] == {'text_variant': 1}
    assert not variant['complete']
    with pytest.raises(ValueError):
        QueryPlan(sheet='Sheet', start='A1', operation='count',
                  filters=[{'column': 'A', 'operator': 'is_blank', 'blanks': 'include'}])


# X7 — join aggregates


def join_tables(path):
    book = Workbook()
    departments = book.active
    departments.title = 'Departments'
    departments.append(['부서', '예산'])
    departments.append(['D1', 100])
    departments.append(['D2', 50])
    departments.add_table(Table(displayName='Departments', ref='A1:B3'))
    employees = book.create_sheet('Employees')
    employees.append(['부서', '이름'])
    for row in [('D1', '가'), ('D1', '나'), ('D2', '다')]:
        employees.append(row)
    employees.add_table(Table(displayName='Employees', ref='A1:B4'))
    book.save(path)
    return native(path)['parser_result']


def test_join_aggregates_do_not_repeat_source_rows_silently(tmp_path):
    raw = join_tables(tmp_path / 'join.xlsx')
    base = {'document_id': DOCUMENT, 'index_version': VERSION, 'sheet': 'Departments', 'start': 'A1',
            'operation': 'sum', 'column': 'B',
            'join': {'sheet': 'Employees', 'start': 'A1', 'cardinality': 'one_to_many',
                     'keys': [{'left': 'A', 'right': 'A', 'type': 'text'}]}}

    def run(**changes):
        return execute(raw, PublishedTableQuery.model_validate({**base, **changes}))

    automatic = run()
    assert automatic['result']['value'] is None and automatic['result']['aggregate_unit_required']
    assert automatic['result']['pair_based_value'] == 250 and automatic['result']['aggregate_unit'] == 'pairs'
    assert automatic['issues'] == {'duplicated_join_values': 1} and not automatic['complete']
    per_department = run(aggregate_unit='left_rows')
    assert per_department['result']['value'] == 150 and per_department['complete']
    per_pair = run(aggregate_unit='pairs')
    assert per_pair['result']['value'] == 250 and per_pair['complete']
    assert 'aggregate_unit_required' not in per_pair['result']
    employees = run(operation='count', column=None, aggregate_unit='right_rows')
    assert employees['result']['matched_rows'] == 3
    ambiguous = run(operation='count', column=None, aggregate_unit='left_rows', group_by='B', group_side='right')
    assert ambiguous['issues'] == {'multiple_join_values': 1}
    with pytest.raises(ValueError):
        execute(raw, QueryPlan(sheet='Departments', start='A1', operation='count', aggregate_unit='pairs'))


def test_a_possible_totals_row_does_not_break_join_uniqueness_silently(tmp_path):
    path = tmp_path / 'legacy-join.xlsx'
    book = Workbook()
    departments = book.active
    departments.title = 'Departments'
    departments.append(['부서', '예산'])
    for row in [('D1', 100), ('D2', 200), ('D1', 300)]:
        departments.append(row)
    departments.add_table(Table(displayName='Departments', ref='A1:B4'))
    employees = book.create_sheet('Employees')
    employees.append(['부서', '이름'])
    for row in [('D1', '가'), ('D1', '나'), ('D2', '다')]:
        employees.append(row)
    employees.add_table(Table(displayName='Employees', ref='A1:B4'))
    book.save(path)
    raw = native(path)['parser_result']
    plan = PublishedTableQuery.model_validate({
        'document_id': DOCUMENT, 'index_version': VERSION, 'sheet': 'Departments', 'start': 'A1', 'operation': 'count',
        'join': {'sheet': 'Employees', 'start': 'A1', 'cardinality': 'one_to_many',
                 'keys': [{'left': 'A', 'right': 'A', 'type': 'text'}]}})
    # A duplicate key among certain rows is still rejected.
    with pytest.raises(ValueError, match='고유 키가 중복'):
        execute(raw, plan)
    region_data = raw['xlsx_sheets'][0]['regions'][0]
    region_data['totals_rows'] = 1
    region_data.pop('totals_rows_basis')
    result = execute(raw, plan)
    assert result['join']['uncertain_totals_rows'] == {'left': [4], 'right': []}
    assert result['join']['uncertain_key_conflicts'] == {'left': 1, 'right': 0}
    assert result['join']['unmatched_left_rows'] == 0
    assert result['result']['matched_rows'] == 3
    assert result['issues'] == {'totals_row_uncertain': 1} and not result['complete']


# X9-3 — totals rows


@pytest.mark.parametrize(('count', 'expected', 'basis'), [(None, 3, 'shown_without_count'), (1, 2, 'declared')])
def test_totals_rows_follow_the_declared_count(tmp_path, count, expected, basis):
    path = tmp_path / 'totals.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['항목', '금액'])
    for row in [('가', 1), ('나', 2), ('다', 3)]:
        sheet.append(row)
    sheet.add_table(Table(displayName='Totals', ref='A1:B4', totalsRowShown=True, totalsRowCount=count))
    book.save(path)
    raw = native(path)['parser_result']
    assert raw['xlsx_sheets'][0]['tables'][0]['totals_rows_basis'] == basis
    result = execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='count'))
    assert result['result']['matched_rows'] == expected and result['complete']
    assert result['scope']['totals_rows_excluded'] == 3 - expected


def test_legacy_totals_rows_stay_in_range_and_are_undecided_only_when_used(tmp_path):
    raw = status_table(tmp_path / 'legacy.xlsx')
    table = raw['xlsx_sheets'][0]['regions'][0]
    table['totals_rows'] = 1
    table.pop('totals_rows_basis')
    result = execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='count'))
    assert result['scope']['last_data_row'] == 5 and result['scope']['totals_rows_excluded'] == 0
    assert result['scope']['uncertain_totals_rows'] == [5]
    assert result['result']['matched_rows'] == 3 and result['unknown_filter_rows'] == 1
    assert result['issues'] == {'totals_row_uncertain': 1} and not result['complete']
    total = execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='sum', column='B'))
    assert total['result']['value'] == 1.6 and not total['complete']
    # A filter that already excludes the possible totals row leaves a complete answer.
    progress = execute(raw, QueryPlan(sheet='Sheet', start='A1', operation='count',
                                      filters=[{'column': 'A', 'operator': 'eq', 'value': '진행'}]))
    assert progress['result']['matched_rows'] == 1 and progress['complete']


# X3 and X9-2 — size limits and formatting-only cells


def test_oversized_sheets_are_rejected_before_loading_the_workbook(tmp_path, monkeypatch):
    from indexer.parsing import native_documents

    path = tmp_path / 'large.xlsx'
    book = Workbook()
    sheet = book.active
    for row in range(1, 5002):
        sheet.cell(row, 1, row)
    book.save(path)

    def unexpected(*_args, **_kwargs):
        raise AssertionError('the workbook must not be loaded')

    monkeypatch.setattr(native_documents, 'load_workbook', unexpected)
    with pytest.raises(IndexerError) as error:
        NativeExtractor(None, 'native_text').extract(path, 'xlsx')
    assert error.value.code == 'NATIVE_TABLE_LIMIT' and error.value.status == 413


def test_formatting_only_cells_are_compressed_and_bounded(tmp_path):
    path = tmp_path / 'styled.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'] = '값'
    fill = PatternFill('solid', fgColor='FFEEEEEE')
    for row in range(2, 22):
        for col in range(2, 12):
            sheet.cell(row, col).fill = fill
    book.save(path)
    data = native(path)['parser_result']['xlsx_sheets'][0]
    assert [cell['address'] for cell in data['cells']] == ['A1'] and data['bounds'] == [1, 1, 1, 1]
    assert data['style_only_cells'] == 200
    assert [(item['start'], item['end']) for item in data['style_ranges']] == [('B2', 'K21')]
    with pytest.raises(IndexerError):
        inspect_office(path, native_cells=True, limits=SheetLimits(cell_elements=50))


# X9-1 — optional cell references


def test_cells_without_references_follow_their_row_position(tmp_path):
    path, patched = tmp_path / 'source.xlsx', tmp_path / 'patched.xlsx'
    book = Workbook()
    sheet = book.active
    sheet['A1'], sheet['C1'], sheet['D1'] = '첫째', '셋째', '넷째'
    sheet['A2'] = 7
    book.save(path)
    patch_sheet(path, patched, lambda data: data.replace(b'<c r="A1"', b'<c').replace(b'<c r="D1"', b'<c')
                .replace(b'<c r="A2"', b'<c'))
    cells = {cell['address']: cell['display_text'] for cell in native(patched)['parser_result']['xlsx_sheets'][0]['cells']}
    assert cells == {'A1': '첫째', 'C1': '셋째', 'D1': '넷째', 'A2': '7'}
    contradiction = tmp_path / 'contradiction.xlsx'
    patch_sheet(path, contradiction, lambda data: data.replace(b'<c r="C1"', b'<c r="C2"'))
    with pytest.raises(IndexerError, match='문서 내부 구조'):
        inspect_office(contradiction)


# X8 — reviews across reprocessing


def test_stale_reviews_do_not_fail_reparsing_and_can_be_reset(tmp_path):
    path = tmp_path / 'review.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['ID', '값'])
    sheet.append([1, 2])
    book.save(path)
    stale = {'Sheet': {'regions': [region('A1', 'A2')], 'excluded': {}},
             'Gone': {'regions': [region('A1', 'A1', 'text')], 'excluded': {}}}
    state = native(path, stale)
    raw = state['parser_result']
    data = raw['xlsx_sheets'][0]
    assert raw['xlsx_review_state']['Sheet']['status'] == 'stale' and raw['xlsx_review_state']['Gone']['status'] == 'stale'
    assert data['stale_review']['regions'][0]['end'] == 'A2' and data.get('analysis_status') != 'reviewed'
    assert any('이전 구조 검토' in warning for warning in raw['warnings'])
    assert state['chunks']

    status = {'xlsx_review': stale, 'xlsx_review_basis': {'Sheet': 'old'}}
    updates = IngestionService._xlsx_review_status(status, raw)
    assert updates['xlsx_review'] == {} and set(updates['xlsx_review_stale']) == {'Sheet', 'Gone'}
    assert updates['xlsx_review_basis'] == {}

    valid = {'Sheet': {'regions': [region('A1', 'B2', headers=('A1', 'B1'))], 'excluded': {}}}
    reviewed = native(path, valid)
    raw, elements = reviewed['parser_result'], reviewed['elements']
    assert raw['xlsx_review_state']['Sheet']['status'] == 'applied'
    assert IngestionService._xlsx_review_status({'xlsx_review': valid}, raw)['xlsx_review_basis'] == {
        'Sheet': raw['xlsx_review_state']['Sheet']['basis']}
    reset_raw, reset_elements = reset_xlsx_review(raw, elements, ['Sheet'])
    sheet_data = reset_raw['xlsx_sheets'][0]
    assert sheet_data['analysis_status'] == 'review_reset' and 'Sheet' not in reset_raw['xlsx_review_state']
    assert all(item['verification'] != 'user_verified' for item in sheet_data['regions'])
    assert any(item.get('_xlsx_region') for item in reset_elements)
