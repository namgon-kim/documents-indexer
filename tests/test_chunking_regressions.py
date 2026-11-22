
import itertools

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


def test_plain_unscoped_header_has_explicit_column_relation():
    html = '<table><tr><th>이름</th><th>직무</th></tr><tr><td>김철수</td><td>개발자</td></tr></table>'
    elements = normalize_elements([{'category': 'table', 'content': {'html': html}}])
    chunks = make_chunks('d', 'j', elements, 400, 0)
    parts = [part for chunk in chunks for part in chunk['table_segments']]
    name = next(part for part in parts if part['value'] == '김철수')
    assert name['label_origin'] == 'parser'
    assert name['label_relations'][0]['text'] == '이름'
    assert name['label_relations'][0]['status'] == 'explicit'
    role = next(part for part in parts if part['value'] == '개발자')
    assert next(rel for rel in role['label_relations'] if rel['text'] == '직무')['status'] == 'explicit'
    # A data cell used as a row label remains a separate, inferred relation.
    assert next(rel for rel in role['label_relations'] if rel['text'] == '김철수')['status'] == 'unverified'


def test_single_column_unscoped_header_does_not_lower_quality():
    html = '<table><tr><th>이름</th></tr><tr><td>김철수</td></tr></table>'
    elements = normalize_elements([{'category': 'table', 'content': {'html': html}}])
    chunk = make_chunks('d', 'j', elements, 400, 0)[0]
    assert chunk['quality'] == 'extracted'
    value = next(part for part in chunk['table_segments'] if part['value'] == '김철수')
    assert value['label_relations'][0]['status'] == 'explicit'


def test_saved_q4_figure_removes_duplicate_rendering_and_preserves_coverage():
    import json
    from pathlib import Path

    from indexer.representation_audit import audit_representation

    raw = json.loads((Path(__file__).parent / 'fixtures' / 'poc_q4_figure.json').read_text(encoding='utf8'))
    old_html, old_text = raw['content']['html'], raw['content']['text']
    elements = normalize_elements([raw])
    assert raw['content']['html'] == old_html and raw['content']['text'] == old_text
    normalized = elements[0]['text']
    assert normalized.count('Figure Type: flowchart') == 1
    assert '/image/placeholder' not in normalized
    assert all(value in normalized for value in ('학위', '수료증', '인증서', '자율전공학과'))
    chunks = make_chunks('d', 'j', elements, 400, 90)
    assert len(chunks) > 1 and not audit_representation(elements, chunks)['missing_text_terms']
    for chunk in chunks:
        [source] = chunk['source_segments']
        assert source['text'] == normalized[source['start']:source['end']]
        assert chunk['source_range_quality'] == 'normalized_element_text'
        assert '/image/placeholder' not in chunk['text']
    assert any(a['source_segments'][0]['end'] > b['source_segments'][0]['start']
               for a, b in itertools.pairwise(chunks))


def test_figure_keeps_unique_information_in_both_renderings():
    raw = {'category': 'figure', 'content': {'html': '<figure><figcaption><p>공통 설명 문구입니다</p><p>HTML 전용 수치 10</p></figcaption><img alt="대체 설명"></figure>',
           'text': '![image](/image/placeholder)\n- 공통 설명 문구입니다\n- TEXT 전용 수치 20'}}
    text = normalize_elements([raw])[0]['text']
    assert text.count('공통 설명 문구입니다') == 1
    assert all(value in text for value in ('HTML 전용 수치 10', 'TEXT 전용 수치 20', '대체 설명'))
    assert '/image/placeholder' not in text


def test_overlap_starts_at_word_boundaries_and_ranges_are_exact():
    from indexer.embedding_input import split_text_ranges

    text = ' '.join(['교육과정', '인증서', '프로그램', '학위'] * 100)
    ranges = list(split_text_ranges(text, chars=70, max_bytes=210, overlap=17))
    assert len(ranges) > 2
    for part, start, end in ranges:
        assert part == text[start:end]
        assert start == 0 or text[start - 1].isspace()
        assert end == len(text) or text[end].isspace()
    assert all(a[2] >= b[1] for a, b in itertools.pairwise(ranges))


def test_figure_resplit_retains_normalized_source_offsets():
    html = '<figure><figcaption>' + '항목 인증서 교육 프로그램 ' * 70 + '</figcaption></figure>'
    elements = normalize_elements([{'category': 'figure', 'content': {'html': html}}])
    chunks = make_chunks('d', 'j', elements, 400, 0)
    parts = [part for chunk in chunks for part in split_chunk(chunk, 256)]
    for part in parts:
        source = part['source_segments'][0]
        assert part['source_range_quality'] == 'normalized_element_text'
        assert source['text'] == elements[0]['text'][source['start']:source['end']]


def test_figure_dedup_keeps_changed_claim_and_literal_markdown_content():
    raw = {'category': 'figure', 'content': {
        'html': '<figure><figcaption><p>공통 핵심 수치 10</p><p>공통 설명</p></figcaption></figure>',
        'text': '- 공통 핵심 수치 20\n- **공통 설명**\n- 함수 __init__ 및 [설정](https://example.test/config)'}}
    text = normalize_elements([raw])[0]['text']
    assert '공통 핵심 수치 10' in text and '공통 핵심 수치 20' in text
    assert text.count('공통 설명') == 1
    assert '__init__' in text and 'https://example.test/config' in text


def test_oversized_single_word_splits_only_when_necessary():
    from indexer.embedding_input import split_text_ranges

    text = '인증서' * 100
    ranges = list(split_text_ranges(text, chars=100, max_bytes=60, overlap=10))
    assert len(ranges) > 1
    assert all(part == text[start:end] and len(part.encode()) <= 60 for part, start, end in ranges)
    assert ''.join(part for part, _, _ in ranges) == text


def test_figure_deduplicates_complete_sentences_and_retains_extra_sentence():
    raw = {'category': 'figure', 'content': {
        'html': '<figure><figcaption>공통 설명입니다.</figcaption></figure>',
        'text': '공통 설명입니다. 독립적인 추가 설명입니다.'}}
    text = normalize_elements([raw])[0]['text']
    assert text.count('공통 설명입니다.') == 1
    assert '독립적인 추가 설명입니다.' in text


def test_figure_caption_and_offsets_survive_embedding_resplit():
    caption = row('caption', '그림 1 운영 흐름', 1)
    figure = {'category': 'figure', 'page': 1, 'content': {
        'html': '<figure><figcaption>' + '항목 인증서 교육 프로그램 ' * 50 + '</figcaption></figure>'}}
    elements = normalize_elements([caption, figure])
    chunks = [chunk for chunk in make_chunks('d', 'j', elements, 400, 0) if chunk['kind'] == 'figure']
    assert chunks and '그림 1 운영 흐름' in chunks[0]['text']
    split = split_chunk(chunks[0], 256)
    for part in split:
        source = part['source_segments'][0]
        assert '그림 1 운영 흐름' in part['text']
        assert source['text'] == elements[1]['text'][source['start']:source['end']]
        assert part['context_element_ids'] == [elements[0]['element_id']]


def test_q4_search_recall_before_and_after_figure_dedup(tmp_path):
    import json
    from pathlib import Path

    from indexer.bm25_index import BM25Index

    raw = json.loads((Path(__file__).parent / 'fixtures' / 'poc_q4_figure.json').read_text(encoding='utf8'))
    new_elements = normalize_elements([raw])
    old_elements = normalize_elements([raw])
    old_elements[0]['text'] = (' '.join(parse_table_html(raw['content']['html']).stripped_strings)
                               + '\n' + raw['content']['text'].strip())
    for name, elements in [('old', old_elements), ('new', new_elements)]:
        chunks = make_chunks('d', name, elements, 400, 0)
        index = BM25Index(tmp_path / f'{name}.sqlite')
        index.replace_document('d', chunks)
        for query in ('인증서', '수료증', '프로그램', '학위'):
            found = index.search(query, 5)
            assert any(query in chunk['text'] for chunk in chunks if chunk['chunk_id'] in {hit.chunk_id for hit in found})
        if name == 'new':
            assert all('/image/placeholder' not in chunk['text'] for chunk in chunks)
            assert sum(chunk['text'].count('Figure Type') for chunk in chunks) == 1


def test_figure_alt_is_not_lost_as_partial_substring():
    raw = {'category': 'figure', 'content': {
        'html': '<figure><figcaption>100건</figcaption><img alt="10"></figure>'}}
    text = normalize_elements([raw])[0]['text']
    assert text.splitlines() == ['100건', '10']


def test_figure_keeps_provider_numbered_items_and_years():
    raw = {'category': 'figure', 'content': {'html': '<figure></figure>',
           'text': '2026. 추진 계획\n1. 학위\n2. 수료증\n01) 첫째 항목\n- 별도 내용'}}
    source = raw['content']['text']
    elements = normalize_elements([raw])
    text = elements[0]['text']
    assert all(item in text for item in ('2026. 추진 계획', '1. 학위', '2. 수료증', '01) 첫째 항목', '- 별도 내용'))
    assert raw['content']['text'] == source
    chunks = make_chunks('d', 'j', elements, 400, 0)
    assert all(item in ' '.join(c['text'] for c in chunks) for item in ('2026', '1. 학위', '2. 수료증', '01) 첫째 항목'))
    for chunk in chunks:
        source_part = chunk['source_segments'][0]
        assert source_part['text'] == text[source_part['start']:source_part['end']]


def test_figure_dedup_does_not_drop_numbers_when_html_lacks_them():
    raw = {'category': 'figure', 'content': {
        'html': '<figure><figcaption><p>추진 계획</p><p>학위</p><p>수료증</p></figcaption></figure>',
        'text': '2026. 추진 계획\n1. 학위\n2. 수료증'}}
    text = normalize_elements([raw])[0]['text']
    assert all(item in text for item in ('2026. 추진 계획', '1. 학위', '2. 수료증'))


def test_figure_dedup_handles_numbered_and_unnumbered_sources_safely():
    raw = {'category': 'figure', 'content': {
        'html': '<figure><figcaption><p>1. 설치 단계</p><p>2. 검증 단계</p><p>공통 설명</p><p>US</p></figcaption></figure>',
        'text': '1. 설치 단계\n2. 검증 단계\n- 공통 설명\n- us\n3. 독립 단계'}}
    text = normalize_elements([raw])[0]['text']
    assert text.count('1. 설치 단계') == text.count('2. 검증 단계') == 1
    assert text.count('공통 설명') == 1
    assert 'US' in text and '- us' in text and '3. 독립 단계' in text


def test_numbered_figure_items_remain_searchable_with_source_ranges(tmp_path):
    from indexer.bm25_index import BM25Index

    raw = {'category': 'figure', 'content': {'html': '<figure><figcaption>추진 계획 학위 수료증</figcaption></figure>',
           'text': '2026. 추진 계획\n1. 학위\n2. 수료증\n01) 첫째 항목'}}
    element = normalize_elements([raw])[0]
    chunks = make_chunks('d', 'j', [element], 400, 0)
    index = BM25Index(tmp_path / 'numbered.sqlite')
    index.replace_document('d', chunks)
    for query in ('2026', '01', '1. 학위', '2. 수료증'):
        hits = index.search(query, 5)
        assert hits
        assert all(chunk['source_segments'][0]['text'] == element['text'][
            chunk['source_segments'][0]['start']:chunk['source_segments'][0]['end']]
            for chunk in chunks if chunk['chunk_id'] in {hit.chunk_id for hit in hits})


def test_saved_page_headers_reset_chapters_and_footers_do_not_index():
    import copy
    import json
    from pathlib import Path

    rows = json.loads((Path(__file__).parent / 'fixtures' / 'poc_page_headings.json').read_text(encoding='utf8'))
    rows.insert(0, {'page': 1, 'category': 'heading1', 'content': {'text': 'I 대학 중장기 발전전략'}})
    original_rows = copy.deepcopy(rows)
    elements = normalize_elements(rows, source_format='pdf')
    chunks = make_chunks('d', 'j', elements, 400, 0)
    assert rows == original_rows
    second = next(e for e in elements if e.get('provider_id') == 48)
    third = next(e for e in elements if e.get('provider_id') == 115)
    assert second['heading_path'][0].startswith('II ')
    assert third['heading_path'][0].startswith('Ⅲ ')
    assert second['heading_path_origin'] == third['heading_path_origin'] == 'inferred'
    for provider_id in (55, 118):
        element = next(e for e in elements if e.get('provider_id') == provider_id)
        assert element['text'] in {'17', '32'} and element['search_exclusion'] == 'page_label'
        assert all(element['element_id'] not in chunk['element_ids'] for chunk in chunks)


def test_repeated_chapter_header_and_numeric_body_are_not_misclassified():
    coords = [{'x': 0.1, 'y': 0.02}, {'x': 0.4, 'y': 0.05}]
    rows = [{'category': 'header', 'page': page, 'coordinates': coords, 'content': {'text': 'II 보고서'}}
            for page in (1, 2)]
    rows.insert(1, {'category': 'heading2', 'page': 1, 'content': {'html': '<h2>세부 제목</h2>'}})
    rows.append({'category': 'heading2', 'page': 2, 'content': {'html': '<h2>다른 제목</h2>'}})
    rows.append({'category': 'paragraph', 'page': 2, 'content': {'text': '17'}})
    elements = normalize_elements(rows, source_format='pdf')
    assert all(e['kind'] == 'header' for e in elements if e['text'] == 'II 보고서')
    assert all(e['heading_level'] == 2 and e['heading_level_origin'] == 'parser'
               for e in elements if e['kind'] == 'heading')
    chunks = make_chunks('d', 'j', elements, 400, 0)
    assert any('17' in c['text'] for c in chunks)


def test_promoted_chapter_and_mixed_explicit_heading_levels_coexist():
    coords = [{'x': 0.1, 'y': 0.02}, {'x': 0.4, 'y': 0.05}]
    rows = [{'page': 1, 'category': 'heading1', 'content': {'text': 'I 이전 장'}},
            {'page': 2, 'category': 'header', 'coordinates': coords, 'content': {'text': 'II 새 장'}},
            {'page': 2, 'category': 'heading1', 'content': {'text': '1.4 실행 계획'}},
            {'page': 2, 'category': 'heading2', 'content': {'html': '<h2>명시적 세부 제목</h2>'}},
            {'page': 2, 'category': 'heading3', 'content': {'html': '<h3>명시적 하위 제목</h3>'}},
            {'page': 2, 'category': 'paragraph', 'content': {'text': '실제 본문'}}]
    elements = normalize_elements(rows, source_format='pdf')
    assert elements[2]['heading_path'] == ['II 새 장', '1.4 실행 계획']
    assert elements[3]['heading_level'] == 2 and elements[3]['heading_level_origin'] == 'parser'
    assert elements[4]['heading_level'] == 3 and elements[4]['heading_level_origin'] == 'parser'
    assert elements[-1]['heading_path'] == ['II 새 장', '명시적 세부 제목', '명시적 하위 제목']


def test_chapter_header_promotion_requires_pdf_source():
    coords = [{'x': 0.1, 'y': 0.02}, {'x': 0.4, 'y': 0.05}]
    rows = [{'page': 1, 'category': 'heading1', 'content': {'text': 'I 이전 장'}},
            {'page': 2, 'category': 'header', 'coordinates': coords, 'content': {'text': 'II 새 장'}},
            {'page': 2, 'category': 'heading2', 'content': {'text': '세부 제목'}},
            {'page': 2, 'category': 'paragraph', 'content': {'text': '본문'}}]
    assert normalize_elements(rows, source_format='pdf')[1]['kind'] == 'heading'
    for source_format in (None, 'docx', 'pptx', 'hwpx'):
        elements = normalize_elements(rows, source_format=source_format)
        assert elements[1]['kind'] == 'header'
        assert elements[-1]['heading_path'] == ['I 이전 장', '세부 제목']
