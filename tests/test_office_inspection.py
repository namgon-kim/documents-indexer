from zipfile import ZipFile

import pytest
from indexer.contracts import IndexerError
from indexer.office_inspection import inspect_office


def write_xlsx(path):
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="계획" sheetId="1" r:id="rId1"/><sheet name="숨김" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>''')
        archive.writestr("xl/worksheets/sheet1.xml", '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="B2"><f>SUM(A1:A2)</f><v>12</v></c><c r="C2"><f>SUM(A1:A3)</f><v/></c></row></sheetData></worksheet>''')
        archive.writestr("xl/worksheets/sheet2.xml", '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>''')


def write_pptx(path):
    with ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", '''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId r:id="rId1"/><p:sldId r:id="rId2"/></p:sldIdLst></p:presentation>''')
        archive.writestr("ppt/_rels/presentation.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="slides/slide2.xml"/><Relationship Id="rId2" Target="slides/slide1.xml"/></Relationships>''')
        archive.writestr("ppt/slides/slide2.xml", '''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" show="0"/>''')
        archive.writestr("ppt/slides/slide1.xml", '''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>''')
        archive.writestr("ppt/slides/_rels/slide2.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="note" Target="../notesSlides/notesSlide1.xml"/></Relationships>''')
        archive.writestr("ppt/notesSlides/notesSlide1.xml", "<notes/>")


def test_excel_formula_and_hidden_sheet_are_recorded_without_page_guess(tmp_path):
    path = tmp_path / "report.xlsx"
    write_xlsx(path)
    result = inspect_office(path)
    assert result["parser_page_mapping"] == "unknown"
    assert result["sheets"][1]["visibility"] == "hidden"
    formulas = result["sheets"][0]["formulas"]
    assert formulas[0]["cell"] == "B2" and formulas[0]["formula"] == "SUM(A1:A2)"
    assert formulas[0]["cached_value"] == "12" and formulas[1]["cached_value"] is None


def test_slides_follow_presentation_order_and_keep_visibility_and_notes(tmp_path):
    path = tmp_path / "slides.pptx"
    write_pptx(path)
    result = inspect_office(path)
    assert result["parser_page_mapping"] == "unknown"
    assert result["slides"][0]["source_path"].endswith("slide2.xml")
    assert result["slides"][0]["hidden"] and result["slides"][0]["notes_present"]
    assert not result["slides"][1]["hidden"]


def test_rejects_unsafe_zip_paths_without_reading_them(tmp_path):
    path = tmp_path / "unsafe.xlsx"
    with ZipFile(path, "w") as archive:
        archive.writestr("../outside", "not allowed")
    with pytest.raises(IndexerError, match="문서 내부 구조"):
        inspect_office(path)


@pytest.mark.parametrize(("suffix", "writer", "key"), [
    (".xlsx", write_xlsx, "sheets"), (".pptx", write_pptx, "slides"),
])
def test_ingestion_persists_original_structure_separately_from_parser(tmp_path, suffix, writer, key):
    from indexer.artifact_store import ArtifactStore
    from indexer.ingestion_service import IngestionService
    from indexer.parsing.contracts import ParseResult

    path = tmp_path / ("source" + suffix)
    writer(path)
    class Parser:
        def parse(self, request):
            rows = [{"page": 1, "content": {"text": "문서 내용"}}]
            return ParseResult("fake", rows, {"elements": rows})
    store = ArtifactStore(tmp_path)
    result = IngestionService(store, Parser()).ingest(path)
    details = store.load_job(result["document_id"], result["job_id"], "source_details")
    assert details["parser_page_mapping"] == "unknown" and details[key]
    assert result["source_summary"][key] and "source_path" not in result["source_summary"][key][0]


def test_parser_office_locations_are_validated_without_guessing():
    from indexer.chunking import normalize_elements
    from indexer.office_locations import apply_source_locations
    raw = [
        {"page": 1, "content": {"text": "예산"}, "source_location": {"kind": "xlsx_cell", "sheet": "예산", "cell": "B5"}},
        {"page": 2, "content": {"text": "잘못된 시트"}, "source_location": {"kind": "xlsx_cell", "sheet": "없는 시트", "cell": "A1"}},
        {"page": 3, "content": {"text": "페이지뿐"}},
    ]
    elements = normalize_elements(raw)
    apply_source_locations(elements, raw, {"format": "xlsx", "sheets": [{"name": "예산"}]})
    assert elements[0]["office_location"] == {"kind": "xlsx_cell", "sheet": "예산", "cell": "B5", "origin": "parser", "status": "unverified"}
    assert "office_location" not in elements[1] and "office_location" not in elements[2]
    raw = [{"page": 1, "content": {"text": "슬라이드"}, "source_location": {"kind": "pptx_slide", "slide": 2}}]
    elements = normalize_elements(raw)
    apply_source_locations(elements, raw, {"format": "pptx", "slides": [{"number": 2}]})
    assert elements[0]["office_location"]["slide"] == 2
