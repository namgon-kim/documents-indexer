from io import BytesIO

from indexer.artifact_store import ArtifactStore
from indexer.chunking import make_chunks, normalize_elements
from indexer.ingestion_graph import make_graph
from indexer.ingestion_service import IngestionService
from indexer.parsing.contracts import ParseResult
from pypdf import PdfWriter


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_workflow_selects_format_profile_and_uses_parser(tmp_path):
    path = tmp_path / "계획.xlsx"
    from test_office_inspection import write_xlsx
    write_xlsx(path)
    class Parser:
        def parse(self, request):
            assert request.format == "xlsx"
            return ParseResult("fake", [{"page": 1, "content": {"text": "계획"}}], {"elements": []})
    result = make_graph(Parser()).invoke({"path": str(path), "document_id": "doc", "job_id": "job"})
    assert result["profile"] == "spreadsheet"
    assert result["profile_origin"] == "format"
    assert result["parser_provider"] == "fake"


def test_user_profile_overrides_default(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_bytes())
    class Parser:
        def parse(self, request):
            assert request.ocr == "force"
            return ParseResult("fake", [], {"elements": []})
    result = make_graph(Parser()).invoke({"path": str(path), "requested_profile": "scanned", "document_id": "doc", "job_id": "job"})
    assert result["profile_origin"] == "user"


def test_service_applies_profile_defaults_and_preserves_explicit_chunk_settings(tmp_path):
    class Parser:
        pass
    service = IngestionService(ArtifactStore(tmp_path / "store"), Parser())
    for filename, profile, supplied, expected in [
        ("a.xlsx", None, {}, (1500, 0)),
        ("b.pptx", None, {}, (1000, 100)),
        ("c.pdf", "manual", {}, (800, 100)),
        ("d.pdf", "report", {"target_chars": 600}, (600, 150)),
        ("e.pdf", "report", {"target_chars": 900, "overlap_chars": 25}, (900, 25)),
    ]:
        path = tmp_path / filename
        path.write_bytes(filename.encode())
        job = service.enqueue(path, profile, supplied)
        assert (job["settings"]["target_chars"], job["settings"]["overlap_chars"]) == expected


def test_service_persists_source_and_artifacts_in_isolated_namespace(tmp_path):
    import json
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes())
    class Parser:
        def parse(self, request):
            element = {"page": 1, "category": "paragraph", "content": {"text": "본문"}}
            return ParseResult("fake", [element], {"elements": [element]})
    store = ArtifactStore(tmp_path)
    result = IngestionService(store, Parser()).ingest(source)
    assert result["status"] == "parsed"
    folder = store.document_dir(result["document_id"])
    assert (folder / "source.pdf").read_bytes() == source.read_bytes()
    chunks = json.loads((folder / "jobs" / result["job_id"] / "chunks.json").read_text())
    assert chunks[0]["text"] == "본문" and chunks[0]["page"] == 1


def test_table_header_marked_data_is_preserved_with_cell_location():
    from indexer.chunking import make_chunks, normalize_elements

    html = """<table><tr><th>비전</th><th colspan="2">미래인재</th></tr>
    <tr><th>발전전략</th><th>교육혁신</th><th>산학협력</th></tr>
    <tr><th>방향</th><th>창의융합교육강화</th><td>생태계 성장</td></tr></table>"""
    elements = normalize_elements([{"page": 4, "category": "table", "content": {"html": html}}])
    chunks = make_chunks("doc", "job", elements)
    text = "\n".join(item["text"] for item in chunks)
    assert "교육혁신 / 창의융합교육강화" in text
    assert "산학협력 / 생태계 성장" in text
    assert any({"row": 3, "column": 2} in item["table_cells"] for item in chunks)


def test_merged_group_preserves_inferred_columns_for_content_and_organizations():
    from indexer.chunking import make_chunks, normalize_elements

    html = ('<table><tr><td rowspan="3">업무</td><td>준비</td><td>실행</td></tr>'
            '<tr><td>자료 조사</td><td>사업 진행</td></tr>'
            '<tr><td>기획팀</td><td>운영팀</td></tr></table>')
    elements = normalize_elements([{"page": 1, "category": "table", "content": {"html": html}}])
    chunks = make_chunks('doc', 'job', elements, 400, 0)
    segments = [s for c in chunks for s in c['table_segments']]
    for value, label in [('자료 조사', '준비'), ('사업 진행', '실행'), ('기획팀', '준비'), ('운영팀', '실행')]:
        segment = next(s for s in segments if s['value'] == value)
        assert label in segment['label'] and segment['label_origin'] == 'inferred'
    assert all(c['quality'] == 'unverified' for c in chunks)


def test_table_preserves_each_cell_even_when_text_repeats_or_is_substring():
    from indexer.chunking import make_chunks, normalize_elements

    html = "<table><tr><th>목표</th><th>성과목표</th><th>성과목표</th></tr><tr><td>교육</td><td>성과목표</td><td>목표</td></tr></table>"
    elements = normalize_elements([{"page": 1, "category": "table", "content": {"html": html}}])
    chunks = make_chunks("doc", "job", elements)
    cells = {(cell["row"], cell["column"]) for chunk in chunks for cell in chunk["table_cells"]}
    assert cells == {(row, column) for row in (1, 2) for column in (1, 2, 3)}


def test_neighboring_paragraphs_are_grouped_without_crossing_section_or_page():
    from indexer.chunking import make_chunks, normalize_elements

    rows = [
        {"page": 1, "category": "heading1", "content": {"html": "<h1>발전 계획</h1>"}},
        {"page": 1, "category": "paragraph", "content": {"text": "첫 번째 문단입니다."}},
        {"page": 1, "category": "paragraph", "content": {"text": "두 번째 문단입니다."}},
        {"page": 2, "category": "paragraph", "content": {"text": "다음 페이지 문단입니다."}},
        {"page": 2, "category": "heading2", "content": {"html": "<h2>다른 절</h2>"}},
        {"page": 2, "category": "paragraph", "content": {"text": "다른 절의 문단입니다."}},
    ]
    chunks = make_chunks("doc", "job", normalize_elements(rows), 400, 0)
    assert len(chunks) == 3
    assert chunks[0]["text"] == "발전 계획\n첫 번째 문단입니다.\n두 번째 문단입니다."
    assert chunks[0]["element_ids"] == ["e2", "e3"] and chunks[0]["heading_element_ids"] == ["e1"]
    assert chunks[1]["page"] == 2 and "next_chunk_id" not in chunks[0]
    assert chunks[2]["heading_path"] == ["발전 계획", "다른 절"]


def test_list_intro_and_nested_items_remain_in_search_text():
    from indexer.chunking import make_chunks, normalize_elements

    html = "<div>운영 기준<ul><li>첫 단계<ul><li>세부 작업</li></ul></li><li>완료 확인</li></ul>끝 안내</div>"
    elements = normalize_elements([{"page": 1, "category": "list", "content": {"html": html}}])
    chunk = make_chunks("doc", "job", elements)[0]
    assert all(text in chunk["text"] for text in ("운영 기준", "첫 단계", "세부 작업", "완료 확인", "끝 안내"))


def test_chunking_respects_overlap_and_utf8_byte_limit():
    from indexer.chunking import make_chunks, normalize_elements
    from indexer.embedding_input import MAX_INPUT_BYTES

    text = " ".join(f"항목{i} 테스트" for i in range(1000))
    elements = normalize_elements([{"category": "paragraph", "content": {"text": text}}])
    chunks = make_chunks("doc", "job", elements, target_chars=1200, overlap_chars=120)
    assert len(chunks) > 1
    assert all(len(item["text"].encode()) <= MAX_INPUT_BYTES for item in chunks)
    assert chunks[0]["text"][-60:] in chunks[1]["text"]


def test_workflow_applies_ocr_dpi_and_chunk_settings(tmp_path):
    path = tmp_path / "document.pdf"
    path.write_bytes(pdf_bytes())
    class Parser:
        def parse(self, request):
            assert (request.ocr, request.dpi) == ("force", 400)
            return ParseResult("fake", [{"page": 1, "content": {"text": "가" * 1000}}], {"elements": []})
    state = make_graph(Parser()).invoke({"path": str(path), "document_id": "doc", "job_id": "job",
                                        "settings": {"ocr_policy": "force", "dpi": 400,
                                                     "target_chars": 400, "overlap_chars": 0}})
    assert len(state["chunks"]) == 3
    assert all(len(chunk["text"]) <= 400 for chunk in state["chunks"])


def test_rechunk_requires_the_original_parser_contract(tmp_path):
    from types import SimpleNamespace

    import pytest

    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes())
    class Parser:
        config = SimpleNamespace(provider="fake", base_url="https://one.test", model="one", mode="standard", options={})
        def parse(self, request):
            element = {"page": 1, "content": {"text": "본문"}}
            return ParseResult("fake", [element], {"elements": [element]})
    parser = Parser()
    service = IngestionService(ArtifactStore(tmp_path), parser)
    result = service.ingest(source, settings={"target_chars": 1200, "overlap_chars": 120, "ocr_policy": "automatic", "dpi": None})
    parser.config.model = "two"
    with pytest.raises(ValueError, match="파서 설정"):
        service.enqueue_reprocess(result["document_id"], result["job_id"], "rechunk",
                                  {"target_chars": 400, "overlap_chars": 0, "ocr_policy": "automatic", "dpi": None})
    retry = service.enqueue_reprocess(result["document_id"], result["job_id"], "reparse",
                                      {"target_chars": 400, "overlap_chars": 0, "ocr_policy": "automatic", "dpi": None})
    assert service.run_job(result["document_id"], retry["job_id"])["status"] == "parsed"


def test_figure_preserves_alt_and_provider_text():
    elements = normalize_elements([{"page": 1, "category": "figure", "content": {
        "html": '<figure><figcaption>추진 현황</figcaption><img alt="진행률 27%"></figure>',
        "text": "사업 완료 3건"}}])
    chunks = make_chunks("doc", "job", elements)
    assert "추진 현황" in chunks[0]["text"]
    assert "진행률 27%" in chunks[0]["text"]
    assert "사업 완료 3건" in chunks[0]["text"]
    assert chunks[0]["quality"] == "unverified"


def test_long_merged_table_preserves_all_text_and_cell_coordinates():
    value = "설비 교체 비용 15.25억 원 " * 250 + "마지막 항목"
    html = ('<table><tr><th>항목</th><th>연도</th></tr>'
            '<tr><td rowspan="2">설비비</td><td>2025년</td></tr>'
            f'<tr><td>{value}</td></tr></table>')
    chunks = make_chunks("doc", "job", normalize_elements([
        {"page": 1, "category": "table", "content": {"html": html}}
    ]), target_chars=400, overlap_chars=0)
    assert any("마지막 항목" in chunk["text"] for chunk in chunks)
    assert all(len(chunk["text"].encode("utf-8")) <= 3800 for chunk in chunks)
    assert any({"row": 3, "column": 2} in chunk["table_cells"] for chunk in chunks)
    assert any("설비비" in chunk["text"] for chunk in chunks)


def test_separate_table_caption_and_explicit_continuation_keep_source_references():
    rows = [
        {"page": 1, "category": "caption", "content": {"text": "표 1 사업비 (억원)"}},
        {"page": 1, "category": "table", "content": {"html": '<table><tr><th>항목</th><th>2026년</th></tr><tr><td>설비비</td><td>12</td></tr></table>'}},
        {"page": 2, "category": "caption", "content": {"text": "표 1 사업비 (계속)"}},
        {"page": 2, "category": "table", "content": {"html": '<table><tr><td>인건비</td><td>27</td></tr></table>'}},
    ]
    chunks = make_chunks("doc", "job", normalize_elements(rows))
    first = next(c for c in chunks if c["kind"] == "table" and c["page"] == 1)
    second = next(c for c in chunks if c["kind"] == "table" and c["page"] == 2)
    assert "사업비 (억원)" in first["text"] and "e1" in first["context_element_ids"]
    assert "2026년 / 27" in second["text"] and "사업비 (억원)" in second["text"]
    assert set(second["context_element_ids"]) == {"e1", "e2", "e3"}
    assert second["quality"] == "unverified" and second["relations"][0]["status"] == "unverified"


def test_unmarked_next_page_table_does_not_inherit_previous_headers():
    rows = [
        {"page": 1, "category": "table", "content": {"html": '<table><tr><th>항목</th><th>2026년</th></tr><tr><td>설비비</td><td>12</td></tr></table>'}},
        {"page": 2, "category": "table", "content": {"html": '<table><tr><td>인력</td><td>27</td></tr></table>'}},
    ]
    second = next(c for c in make_chunks("doc", "job", normalize_elements(rows)) if c["page"] == 2)
    assert "2026년" not in second["text"] and not second["context_element_ids"]


def test_figure_caption_is_linked_but_relationship_is_marked_inferred():
    rows = [
        {"page": 1, "category": "caption", "content": {"text": "그림 1 추진 체계"}},
        {"page": 1, "category": "figure", "content": {"html": '<figure><img alt="교육혁신"></figure>'}},
    ]
    chunk = next(c for c in make_chunks("doc", "job", normalize_elements(rows)) if c["kind"] == "figure")
    assert "그림 1 추진 체계" in chunk["text"] and "교육혁신" in chunk["text"]
    assert chunk["context_element_ids"] == ["e1"]
    assert chunk["relations"][0]["status"] == "unverified"


def test_different_numbered_continuation_does_not_inherit_headers():
    rows = [
        {"page": 1, "category": "table", "content": {"html": '<table><caption>표 1 예산</caption><tr><th>항목</th><th>2026년</th></tr><tr><td>설비</td><td>12</td></tr></table>'}},
        {"page": 2, "category": "table", "content": {"html": '<table><caption>표 2 (계속)</caption><tr><td>인력</td><td>27</td></tr></table>'}},
    ]
    chunk = next(c for c in make_chunks("doc", "job", normalize_elements(rows)) if c["page"] == 2)
    assert "2026년" not in chunk["text"] and not chunk["context_element_ids"]


def test_document_profiles_apply_different_structure_policies():
    rows = [
        {"page": 1, "category": "paragraph", "content": {"text": "첫 문단"}},
        {"page": 1, "category": "list", "content": {"html": "<ul><li>실행 단계</li></ul>"}},
        {"page": 1, "category": "paragraph", "content": {"text": "다음 문단"}},
    ]
    elements = normalize_elements(rows)
    assert len(make_chunks("doc", "job", elements, profile="general")) == 1
    assert len(make_chunks("doc", "job", elements, profile="manual")) == 3
    assert len(make_chunks("doc", "job", elements, profile="slides")) == 3
    table = normalize_elements([{"page": 1, "category": "table", "content": {"html":
        "<table><tr><th>항목</th><th>값</th></tr><tr><td>교육</td><td>10</td></tr><tr><td>연구</td><td>20</td></tr></table>"}}])
    general = make_chunks("doc", "job", table, profile="general")
    sheet = make_chunks("doc", "job", table, profile="spreadsheet")
    assert len(sheet) > len(general)
    assert all(len({segment["row"] for segment in chunk["table_segments"]}) == 1 for chunk in sheet)
