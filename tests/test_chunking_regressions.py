
import httpx
import pytest
from indexer.chunking import make_chunks, normalize_elements
from indexer.contracts import IndexerError
from indexer.embedding_adaptation import split_chunk
from indexer.embedding_input import embedding_text
from indexer.parsing.contracts import ParserConfig, ParseRequest
from indexer.parsing.normalization import cells_from, clean, parse_table_html
from indexer.parsing.paddle_vl_adapter import PaddleVLParser
from indexer.parsing.upstage_adapter import UpstageParser
from indexer.structure_relations import figure_relationships
from indexer.table_evidence import split_cell_source


def row(kind, text, page=None, **extra):
    return {'category': kind, 'content': {'text': text}, 'page': page, **extra}


def test_page_less_native_paragraphs_group_and_link_without_crossing_scope():
    elements = normalize_elements([row('paragraph', f'문단 {i} ' + '내용 ' * 35, _origin='native_extraction') for i in range(6)])
    chunks = make_chunks('d', 'j', elements, 400, 0)
    assert 1 < len(chunks) < 6
    assert chunks[0]['next_chunk_id'] == chunks[1]['chunk_id']
    assert all(c['page'] is None and c['source_scope'] == 'native:document' for c in chunks)
    unknown = normalize_elements([row('paragraph', '하나'), row('paragraph', '둘')])
    assert len(make_chunks('d', 'j', unknown, 400, 0)) == 2
    slide_rows = normalize_elements([row('paragraph', '첫 슬라이드', _origin='native_extraction'), row('paragraph', '둘째 슬라이드', _origin='native_extraction')])
    for index, item in enumerate(slide_rows, 1):
        item['office_location'] = {'kind': 'pptx_slide', 'slide': index}
    assert len(make_chunks('d', 'j', slide_rows, 400, 0)) == 2


def test_page_less_figure_caption_uses_native_scope():
    rows = normalize_elements([row('figure', '그래프 내용', _origin='native_extraction'),
                               row('caption', '그림 1 학생 현황', _origin='native_extraction')])
    relation = figure_relationships(rows)
    assert relation[rows[0]['element_id']]['caption_id'] == rows[1]['element_id']
    for index, item in enumerate(rows, 1):
        item['office_location'] = {'kind': 'pptx_slide', 'slide': index}
    assert not figure_relationships(rows)


def test_flat_heading_labels_and_korean_outline_do_not_erase_anchored_parent():
    labels = ['I 대학', '2. 현황', '2.1 세부', '구분', '2.2 내부여건', '1) 세부계획', '2) 다음 계획']
    elements = normalize_elements([row('heading1', label, 1) for label in labels])
    assert elements[4]['heading_path'] == ['I 대학', '2. 현황', '2.2 내부여건']
    assert elements[5]['heading_path'] == ['I 대학', '2. 현황', '2.2 내부여건', '1) 세부계획']
    assert elements[6]['heading_path'] == ['I 대학', '2. 현황', '2.2 내부여건', '2) 다음 계획']
    assert elements[3]['heading_level_origin'] == 'inferred'


def test_long_heading_keeps_usable_body_budget_and_caption_body():
    elements = normalize_elements([row('heading1', 'A' * 500, 1), row('paragraph', 'b' * 800, 1)])
    chunks = make_chunks('d', 'j', elements, 400, 0)
    assert len(chunks) <= 5 and all(len(c['text']) <= 400 for c in chunks)
    rows = [row('heading1', '전략', 1), row('caption', '표 1 현황', 1),
            {'category': 'table', 'page': 1, 'content': {'html': '<table><tr><td>항목</td><td>값</td></tr></table>'}}]
    table = next(c for c in make_chunks('d', 'j', normalize_elements(rows), 400, 0) if c['kind'] == 'table')
    assert '표 1 현황' not in table['body_text'] and '항목' in table['body_text']


def test_paddle_table_escaped_newlines_and_inferred_headers(tmp_path):
    path = tmp_path / 'a.pdf'
    path.write_bytes(b'%PDF')
    table = '<table><tr><td>구분</td><td>목표</td></tr><tr><td>교육</td><td>• 가\\n• 나</td></tr></table>'
    response = {'errorCode': 0, 'result': {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
        {'block_label': 'table', 'block_content': table}]}}]}}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
        result = PaddleVLParser(ParserConfig('paddle_vl', 'http://example', ''), client).parse(ParseRequest(path, 'pdf', page_count=1))
    elements = normalize_elements(result.elements)
    assert '• 가\n• 나' in elements[0]['text']
    chunks = make_chunks('d', 'j', elements, 400, 0)
    assert all('\\n' not in c['text'] for c in chunks)
    segment = next(s for c in chunks for s in c['table_segments'] if '• 가' in s['value'])
    assert '목표' in segment['label'] and segment['label_origin'] == 'inferred'
    assert result.raw == response



def test_common_html_keeps_line_breaks_and_indentation_in_evidence():
    html = """<table>
    <tr><th scope="col">Unit<br> price</th><th scope="col">Notes</th></tr>
    <tr><td>1.<span>25</span></td>
    <td><div>first<br>  second<br><br>last</div>
    <pre>    indented
        child</pre>
    <code><span>  call()</span>
      arg</code><script>discard</script></td></tr>
    </table>"""
    soup = parse_table_html(html)
    original = str(soup)
    cells = cells_from(soup.table)
    assert str(soup) == original
    assert cells[2].text == '1.25'
    assert cells[3].text == 'first\n  second\n\nlast\n    indented\n        child\n  call()\n      arg'
    assert cells[3].search_text == 'first second last indented child call() arg'
    assert cells[0].search_text == 'Unit price'

    elements = normalize_elements([{'category': 'table', 'page': 1, 'content': {'html': html}}])
    assert cells[3].text in elements[0]['text']
    chunk = make_chunks('d', 'j', elements, 400, 0)[0]
    assert 'Unit price / 1.25' in chunk['text']
    assert cells[3].search_text in chunk['text'] and cells[3].text not in chunk['text']
    assert any(item['text'] == cells[3].text for item in chunk['source_segments'])
    assert 'discard' not in chunk['text']



def test_table_whitespace_style_applies_across_inline_elements():
    html = '<table style="white-space: pre-wrap"><tr><td><span>start</span>\n    <span>end</span></td></tr></table>'
    source = 'start\n    end'
    assert cells_from(parse_table_html(html).table)[0].text == source
    elements = normalize_elements([{'category': 'table', 'page': 1, 'content': {'html': html}}])
    assert elements[0]['text'] == source
    assert make_chunks('d', 'j', elements, 400, 0)[0]['table_segments'][0]['value'] == 'start end'



def test_rowspan_infers_search_header_without_source_indentation():
    html = ('<table><tr><td rowspan="2">Group</td><td>First<br>  Line</td><td>Second</td></tr>'
            '<tr><td>12</td><td>15</td></tr></table>')
    elements = normalize_elements([{'category': 'table', 'page': 1, 'content': {'html': html}}])
    chunks = make_chunks('d', 'j', elements, 400, 0)
    segment = next(part for chunk in chunks for part in chunk['table_segments'] if part['value'] == '12')
    assert segment['label'] == 'Group / First Line' and segment['label_origin'] == 'inferred'
    assert any(source['text'] == 'First\n  Line' for chunk in chunks for source in chunk['source_segments'])


def test_common_table_source_ranges_follow_initial_and_embedding_splits():
    formatted = '  ' + 'e\u0301 한글 alpha\n    beta ' * 90 + '\n  '
    html = f'<table><tr><td style="white-space: pre-wrap">{formatted}</td><td>other cell</td></tr></table>'
    elements = normalize_elements([{'category': 'table', 'page': 1, 'content': {'html': html}}])
    initial = make_chunks('d', 'j', elements, 400, 0)
    assert len(initial) > 1
    adapted = [part for chunk in initial for part in (
        split_chunk(chunk, 120) if len(chunk['text'].encode()) > 120 else [chunk])]
    assert len(adapted) > len(initial)
    for chunks in (initial, adapted):
        sources = []
        for chunk in chunks:
            assert chunk['source_range_quality'] == 'table_cell'
            for origin, value in zip(chunk['source_segments'], chunk['table_segments'], strict=True):
                assert (origin['row'], origin['column']) == (value['row'], value['column'])
                assert embedding_text(clean(origin['text'])) == value['value']
                if origin['column'] == 1:
                    assert formatted[origin['start']:origin['end']] == origin['text']
                    sources.append(origin['text'])
                else:
                    assert origin['text'] == 'other cell'
        assert ''.join(sources) == formatted



def test_table_fragment_offsets_use_source_characters_through_unicode_normalization():
    raw = '  ' + '\u0344' * 37 + ' e\u0301\n  '
    parts = list(split_cell_source(raw, chars=13, max_bytes=17))
    assert len(parts) > 1 and ''.join(part['source_text'] for part in parts) == raw
    for part in parts:
        assert part['source_text'] == raw[part['source_start']:part['source_end']]
        assert embedding_text(clean(part['source_text'])) == part['text']
        assert len(part['text'].encode()) <= 17


def test_plain_html_cells_keep_inline_text_and_ordinary_search_labels():
    html = '<table><tr><th>Role</th><th>Value</th></tr><tr><td>Fee</td><td><b>pre</b>paid &amp; ready</td></tr></table>'
    elements = normalize_elements([{'category': 'table', 'page': 1, 'content': {'html': html}}])
    chunks = make_chunks('d', 'j', elements, 400, 0)
    segments = [part for chunk in chunks for part in chunk['table_segments']]
    assert any(part['label'] == 'Fee / Value' and part['value'] == 'prepaid & ready' for part in segments)
    assert 'prepaid & ready' in elements[0]['text']


def test_paddle_textless_image_is_reviewable(tmp_path):
    path = tmp_path / 'a.pdf'
    path.write_bytes(b'%PDF')
    raw = {'errorCode': 0, 'result': {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
        {'block_label': 'image', 'block_content': ''}]}}]}}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))) as client:
        result = PaddleVLParser(ParserConfig('paddle_vl', 'http://example', ''), client).parse(ParseRequest(path, 'pdf', page_count=1))
    assert not result.incomplete and result.raw['warnings']


@pytest.mark.parametrize('connection', [False, True])
def test_upstage_sync_errors_have_specific_code(tmp_path, connection):
    path = tmp_path / 'a.pdf'
    path.write_bytes(b'%PDF')
    def reply(request):
        if connection:
            raise httpx.ConnectError('not connected', request=request)
        return httpx.Response(200, text='not json')
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        parser = UpstageParser(ParserConfig('upstage', 'https://example', 'key'), client)
        with pytest.raises(IndexerError) as error:
            parser.parse(ParseRequest(path, 'pdf', page_count=1))
    assert error.value.code == ('PARSER_CONNECTION_FAILED' if connection else 'PARSE_RESPONSE_INVALID')


@pytest.mark.parametrize('first, second', [('Ⅰ. 서론', 'Ⅱ. 본론'), ('I. 서론', 'II. 본론'), ('제1장 총칙', '제2장 사업')])
def test_chapter_headings_remain_top_level(first, second):
    elements = normalize_elements([row('heading1', text, 1) for text in [first, '1. 배경', second, '1. 현황']])
    assert elements[-1]['heading_path'] == [second, '1. 현황']


def test_year_title_is_preserved_as_document_heading():
    elements = normalize_elements([row('heading1', '2024 사업계획', 1), row('heading1', '1. 개요', 1)])
    assert elements[-1]['heading_path'] == ['2024 사업계획', '1. 개요']


@pytest.mark.parametrize('label', [
    'IDC 구축 현황', 'LLM 도입', 'ML 적용', 'CD 계획', 'DC 운영', 'CI 개선', 'MD 활용',
    '2024 매출 현황', '2025년 추진 계획', 'AI 인프라',
])
def test_ambiguous_flat_heading_does_not_reset_numbered_ancestry(label):
    elements = normalize_elements([row('heading1', text, 1) for text in
                                   ['Ⅰ. 사업 현황', '1. 인프라', label, '2. 서비스']])
    assert elements[2]['heading_path'] == ['Ⅰ. 사업 현황', '1. 인프라', label]
    assert elements[2]['heading_path_origin'] == 'inferred'
    assert elements[3]['heading_path'] == ['Ⅰ. 사업 현황', '2. 서비스']


@pytest.mark.parametrize(('first', 'second'), [
    ('I. 서론', 'II. 본론'), ('Ⅲ. 계획', 'Ⅳ. 성과'), ('제1장 총칙', '제2장 사업'),
    ('V. 연구', 'VI. 개발'),
])
def test_recognized_top_level_markers_replace_previous_branch(first, second):
    elements = normalize_elements([row('heading1', text, 1) for text in
                                   [first, '1. 배경', '1.1 현황', second, '1. 목표']])
    assert elements[3]['heading_path'] == [second]
    assert elements[4]['heading_path'] == [second, '1. 목표']


def test_parser_document_title_can_reset_inside_a_flat_document():
    elements = normalize_elements([row('heading1', text, 1, _source_label=label) for text, label in [
        ('Ⅰ. 기존', 'doc_title'), ('1. 현황', 'paragraph_title'),
        ('2024 사업계획', 'doc_title'), ('1. 개요', 'paragraph_title')]])
    assert elements[2]['heading_path'] == ['2024 사업계획']
    assert elements[3]['heading_path'] == ['2024 사업계획', '1. 개요']


def test_explicit_multilevel_headings_are_not_reinterpreted():
    elements = normalize_elements([row('heading1', 'Ⅰ. 실적', 1), row('heading2', '1. 매출', 1),
                                   row('heading2', '2024 매출 현황', 1), row('heading1', 'Ⅱ. 계획', 1)])
    assert elements[2]['heading_path'] == ['Ⅰ. 실적', '2024 매출 현황']
    assert elements[3]['heading_path'] == ['Ⅱ. 계획']
    assert all(element['heading_level_origin'] == 'parser' for element in elements)


def test_ambiguous_heading_under_chapter_preserves_chapter_and_marks_inferred():
    elements = normalize_elements([row('heading1', text, 1) for text in ['Ⅰ. 서론', '배경', '본문 제목']])
    assert elements[1]['heading_path'] == ['Ⅰ. 서론', '배경']
    assert elements[1]['heading_path_origin'] == 'inferred'


def test_paddle_headings_keep_context_through_chunks(tmp_path):
    path = tmp_path / 'document.pdf'
    path.write_bytes(b'%PDF')
    blocks = [
        ('doc_title', 'Ⅰ. 사업 현황'), ('paragraph_title', '1. 인프라'),
        ('paragraph_title', 'IDC 구축 현황'), ('text', 'IDC 증설 내용'),
        ('paragraph_title', '2. 서비스'), ('text', '서비스 운영 내용'),
        ('doc_title', 'Ⅱ. 운영 계획'), ('paragraph_title', '1. 예산'), ('text', '예산 본문')]
    payload = {'errorCode': 0, 'result': {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
        {'block_label': label, 'block_content': text} for label, text in blocks]}}]}}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        parsed = PaddleVLParser(ParserConfig('paddle_vl', 'http://example', ''), client).parse(ParseRequest(path, 'pdf', page_count=1))
    chunks = make_chunks('d', 'j', normalize_elements(parsed.elements), 400, 0)
    expected = {'IDC 증설 내용': ['Ⅰ. 사업 현황', '1. 인프라', 'IDC 구축 현황'],
                '서비스 운영 내용': ['Ⅰ. 사업 현황', '2. 서비스'],
                '예산 본문': ['Ⅱ. 운영 계획', '1. 예산']}
    for text, path in expected.items():
        chunk = next(item for item in chunks if text in item['body_text'])
        assert chunk['heading_path'] == path


def test_table_local_subheadings_keep_source_relations_when_split():
    html = ('<table><tr><th colspan="2">의견수렴</th><th colspan="2">조정</th></tr>'
            '<tr><th>학생</th><th>교직원</th><th>공청회</th><th>전략</th></tr>'
            '<tr>' + ''.join('<td>' + value * 15 + '</td>' for value in ('학생내용', '교직원내용', '공청회내용', '전략내용')) + '</tr>'
            '<tr><td>산업체</td><td>지역사회</td><td>사업 조정</td><td>성과공유회</td></tr>'
            '<tr>' + ''.join('<td>' + value * 15 + '</td>' for value in ('산업체내용', '지역사회내용', '사업내용', '공유내용')) + '</tr></table>')
    elements = normalize_elements([{'category': 'table', 'page': 8, 'content': {'html': html}}])
    chunks = make_chunks('d', 'j', elements, 400, 0)
    segment = next(s for c in chunks for s in c['table_segments'] if '산업체내용' in s['value'])
    assert '산업체' in segment['label'] and '학생' not in segment['label']
    assert any(r['row'] == 4 and r['column'] == 1 and r['status'] == 'unverified'
               and r['element_id'] == elements[0]['element_id'] for r in segment['label_relations'])
    adapted = [part for c in chunks for part in (split_chunk(c, 256) if len(c['text'].encode()) > 256 else [c])]
    assert any(r['row'] == 4 and r['column'] == 1 for c in adapted for s in c['table_segments']
               if '산업체내용' in s['value'] for r in s['label_relations'])


def test_row_label_relation_is_marked_as_inferred():
    html = '<table><tr><th scope="col">대상</th><th scope="col">방법</th></tr><tr><td>학생</td><td>설문</td></tr></table>'
    elements = normalize_elements([{'category': 'table', 'page': 1, 'content': {'html': html}}])
    chunks = make_chunks('d', 'j', elements, 400, 0)
    part = next(s for c in chunks for s in c['table_segments'] if s['value'] == '설문')
    assert part['label_origin'] == 'inferred'
    assert any(r['kind'] == 'row' and r['row'] == 2 and r['column'] == 1 and r['status'] == 'unverified'
               for r in part['label_relations'])


def test_poc_q14_saved_parser_table_has_local_candidate_headers():
    from pathlib import Path

    html = (Path(__file__).parent / 'fixtures' / 'poc_q14_table.html').read_text(encoding='utf8')
    elements = normalize_elements([{'category': 'table', 'page': 8, 'content': {'html': html}}])
    chunks = make_chunks('d', 'j', elements, 1000, 150)
    parts = [part for chunk in chunks for part in chunk['table_segments']]
    industry = next(part for part in parts if '현장실습 만족도 조사, 학과 교육과정위원회' in part['value'])
    assert '산업체' in industry['label'] and '학생' not in industry['label']
    assert any(rel['text'] == '산업체' and rel['row'] == 4 and rel['status'] == 'unverified'
               for rel in industry['label_relations'])
    assert all(not (part['value'] == '지역사회' and '산업체' in part['label']) for part in parts)


def test_scoped_headers_survive_short_data_row_between_long_rows():
    html = ('<table><tr><th colspan="2">직원 기록</th></tr>'
            '<tr><th scope="col">이름</th><th scope="col">직무</th></tr>'
            '<tr><td>' + '긴 직원 이름 ' * 10 + '</td><td>기획</td></tr>'
            '<tr><td>김철수</td><td>개발자</td></tr>'
            '<tr><td>' + '다른 긴 직원 이름 ' * 10 + '</td><td>디자이너</td></tr></table>')
    elements = normalize_elements([{'category': 'table', 'content': {'html': html}}])
    parts = [part for chunk in make_chunks('d', 'j', elements, 400, 0)
             for part in chunk['table_segments']]
    role = next(part for part in parts if part['value'] == '디자이너')
    assert '직무' in role['label'] and '개발자' not in role['label']
    assert any(rel['text'] == '직무' and rel['status'] == 'explicit' for rel in role['label_relations'])


def test_legacy_xlsx_label_coordinates_use_original_offsets():
    html = ('<table><tr><th scope="col">Name</th><th scope="col">Result</th></tr>'
            '<tr><td>A</td><td>Pass</td></tr></table>')
    elements = normalize_elements([{'category': 'table', 'content': {'html': html},
                                    '_layout': {'space': 'xlsx_region', 'row_offset': 10, 'column_offset': 3},
                                    '_origin': 'native_extraction'}])
    parts = [part for chunk in make_chunks('d', 'j', elements, 400, 0)
             for part in chunk['table_segments']]
    result = next(part for part in parts if part['value'] == 'Pass')
    assert (result['row'], result['column']) == (12, 5)
    assert {(rel['row'], rel['column']) for rel in result['label_relations']} == {(12, 4), (11, 5)}
