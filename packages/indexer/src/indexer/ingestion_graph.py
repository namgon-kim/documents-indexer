"""새 시스템의 문서 검사와 파싱을 연결하는 LangGraph 기본 흐름."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from indexer.chunking import make_chunks, normalize_elements
from indexer.contracts import IndexerError
from indexer.contracts import ParseResult as QualityParseResult
from indexer.documents import CHUNK_DEFAULTS, DocumentProfile, select_profile
from indexer.inspection import Inspection, copy_pages, inspect_pdf
from indexer.inspection import inspect as inspect_document
from indexer.office_inspection import inspect_office
from indexer.parse_workflow import inspect_parse, merge_retry
from indexer.parsing.contracts import Parser, ParseRequest, ParseResult


class ProcessingState(TypedDict, total=False):
    path: str
    requested_profile: DocumentProfile | None
    profile: str
    profile_origin: str
    profile_reason: str
    elements: list[dict[str, Any]]
    parser_result: dict[str, Any]
    parser_provider: str
    effective_ocr: str
    document_id: str
    job_id: str
    normalized: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    embedding_max_input_bytes: int
    settings: dict[str, Any]
    reused_parse: dict[str, Any]
    image_edit: dict[str, Any]
    provider_attempt: str
    inspection: dict[str, Any]
    office_details: dict[str, Any]
    coverage: dict[str, Any]
    provider_checkpoints: dict[str, Any]
    recovered_parse: dict[str, Any]
    provider_failed: list[int]
    provider_unavailable: list[int]
    provider_incomplete: bool
    provider_terminal_unavailable: list[int]
    recovery_mode: str
    resume_index: bool
    publication: dict[str, Any]


def make_graph(parser: Parser, on_parsed=None, on_represented=None, on_quality=None,
               max_pages=2000, retry_pages=200, on_provider=None, on_progress=None, on_inspected=None,
               on_embedding=None, on_index=None, on_image=None):
    def inspect(state: ProcessingState):
        path = Path(state["path"])
        if not path.is_file():
            raise ValueError("문서 파일을 찾을 수 없습니다")
        selection = select_profile(path, state.get("requested_profile"))
        details = inspect_office(path)
        if details and on_inspected:
            on_inspected(state, details)
        kind = path.suffix.lower().lstrip(".")
        inspection = inspect_pdf(path, max_pages) if kind == "pdf" else None
        if kind in {"hwp", "hwpx"}:
            inspect_document(path, kind, max_pages)
        provider_max = getattr(parser, "max_pdf_pages", None)
        if inspection and provider_max and inspection.pages > provider_max and not state.get("reused_parse"):
            raise IndexerError("PARSER_PAGE_LIMIT", f"현재 파서는 PDF {provider_max}쪽까지 처리합니다. 더 긴 문서 처리는 지원하지 않습니다.", 413)
        info = {"total": inspection.pages, "blank": sorted(inspection.blank),
                "scan_candidates": sorted(inspection.scan_candidates)} if inspection else {}
        return {"profile": selection.profile, "profile_origin": selection.origin,
                "profile_reason": selection.reason, "inspection": info, "office_details": details or {'format': kind}}

    def parse(state: ProcessingState):
        path = Path(state["path"])
        settings = state.get("settings", {})
        inspection = state.get("inspection", {})
        nonblank = (inspection.get("total") or 0) - len(inspection.get("blank", []))
        all_scanned = nonblank > 0 and len(inspection.get("scan_candidates", [])) == nonblank
        force_supported = getattr(parser, "supports_force_ocr", True)
        request = ParseRequest(path, path.suffix.lower().lstrip("."),
                               ocr="force" if force_supported and (state["profile"] == "scanned" or settings.get("ocr_policy") == "force" or all_scanned) else "auto",
                               dpi=settings.get("dpi"), page_count=inspection.get("total"),
                               checkpoint=state.get("provider_checkpoints", {}).get("initial"),
                               persist=(lambda event, data: on_provider(state, state.get('provider_attempt', 'initial'), event, data)) if on_provider else None,
                               progress=(lambda done, total: on_progress(state, done, total)) if on_progress else None)
        reused = state.get("reused_parse") or state.get("recovered_parse")
        result = ParseResult(reused["provider"], reused["elements"], reused["raw"],
                             failed=set(reused.get("failed", [])), unavailable=set(reused.get("unavailable", [])),
                             incomplete=reused.get("incomplete", False),
                             terminal_unavailable=set(reused.get("terminal_unavailable", []))) if reused else None
        if result is None and state.get('image_edit'):
            from indexer.parsing.image_actions import apply_image_actions
            edit = state['image_edit']
            result = apply_image_actions(edit, edit['actions'], parser, edit['image_path'],
                                         request.checkpoint, request.persist, request.progress, edit.get('descriptions'))
        if result is None:
            mode = settings.get('extraction_mode', 'parser')
            if mode != 'parser':
                from indexer.parsing.native_documents import NativeExtractor
                save_image = (lambda image_id, data: on_image(state, image_id, data)) if on_image else None
                result = NativeExtractor(parser, mode, request.checkpoint, request.persist, request.progress, save_image).extract(path, request.format)
            else:
                result = parser.parse(request)
        if on_parsed:
            on_parsed(state, result)
        return {"elements": result.elements, "parser_result": result.raw, "parser_provider": result.provider,
                "effective_ocr": request.ocr, "provider_failed": sorted(result.failed),
                "provider_unavailable": sorted(result.unavailable), "provider_incomplete": result.incomplete,
                "provider_terminal_unavailable": sorted(result.terminal_unavailable)}

    def quality(state: ProcessingState):
        reused = state.get("reused_parse") or state.get('image_edit')
        if reused:
            coverage = deepcopy(reused.get("coverage") or {"total_pages": None, "missing_pages": [], "unknown_scope": True})
            if state.get('image_edit'):
                coverage['warnings'] = state.get('parser_result', {}).get('warnings', [])
                coverage['limitations'] = state.get('parser_result', {}).get('limitations', [])
                coverage['provider_incomplete'] = state.get('provider_incomplete', False)
            if on_quality:
                on_quality(state, state["elements"], coverage, None)
            return {"coverage": coverage}
        info = state.get("inspection", {})
        if not info:
            failed = set(state.get("provider_failed", []))
            unavailable = set(state.get("provider_unavailable", []))
            coverage = {"total_pages": None, "provider_total_pages": state.get("parser_result", {}).get("total_pages"),
                        "missing_pages": sorted(failed | unavailable), "unavailable_pages": sorted(unavailable),
                        "initial_unavailable_pages": sorted(unavailable),
                        "terminal_unavailable_pages": state.get("provider_terminal_unavailable", []),
                        "provider_incomplete": state.get("provider_incomplete", False),
                        "unknown_scope": True, "attempts": [],
                        "warnings": state.get('parser_result', {}).get('warnings', []),
                        "limitations": state.get('parser_result', {}).get('limitations', [])}
            if on_quality:
                on_quality(state, state["elements"], coverage, None)
            return {"coverage": coverage}
        elements = state["elements"]
        if any(not isinstance(item, dict) or type(item.get("page")) is not int or item["page"] < 1 for item in elements):
            raise ValueError("PDF 파싱 결과의 페이지 정보를 확인할 수 없습니다")
        inspection = Inspection(info["total"], set(info["blank"]), set(info["scan_candidates"]))
        first = QualityParseResult(elements, {item["page"] for item in elements},
                                   failed=set(state.get("provider_failed", [])), total=info["total"],
                                   unavailable=set(state.get("provider_unavailable", [])),
                                   incomplete=state.get("provider_incomplete", False))
        report = inspect_parse(first, inspection, max_pages)
        missing = report["missing"]
        initial_missing = list(missing)
        initial_unavailable = list(report["unavailable"])
        terminal_unavailable = set(state.get("provider_terminal_unavailable", []))
        attempt = {"mode": "initial", "pages": None, "ocr": state["effective_ocr"],
                   "dpi": state.get("settings", {}).get("dpi"), "result": state["parser_result"]}
        attempts = [attempt]

        def coverage_value():
            return {"total_pages": info["total"], "blank_pages": info["blank"],
                    "initial_missing_pages": initial_missing, "initial_unavailable_pages": initial_unavailable,
                    "retry_page_limit": retry_pages,
                    "missing_pages": report["missing"], "unavailable_pages": report["unavailable"],
                    "terminal_unavailable_pages": sorted(terminal_unavailable & set(report["unavailable"])),
                    "unknown_scope": False, "provider_incomplete": report["incomplete"],
                    "ocr_missing_pages": report["ocr_missing"],
                    "visual_without_text_pages": report.get("visual_without_text", []),
                    "warnings": state.get("parser_result", {}).get("warnings", []),
                    "attempts": [{key: value for key, value in item.items() if key != "result"} for item in attempts]}

        if missing:
            unavailable = set(report["unavailable"])
            force_supported = getattr(parser, "supports_force_ocr", True)
            present = {item.get('page') for item in elements}
            ocr_only = set(report.get("ocr_missing", [])) & present if not force_supported else set()
            confirmed = [page for page in missing if page not in unavailable and page not in ocr_only]
            if ocr_only:
                attempts.append({"mode": "partial", "pages": sorted(ocr_only), "status": "skipped", "reason": "ocr_force_unsupported"})
            full = len(confirmed) * 2 > info["total"] and not ocr_only
            selected = (list(range(1, info["total"] + 1)) if not unavailable else []) if full else confirmed[:retry_pages]
            checkpoint = state.get("provider_checkpoints", {}).get("retry", {})
            stored_plan = checkpoint.get("plan")
            if stored_plan:
                selected = stored_plan.get("pages", [])
                full = stored_plan.get("mode") == "full"
                if (not isinstance(selected, list) or not selected or len(set(selected)) != len(selected)
                    or any(type(page) is not int or not 1 <= page <= info["total"] for page in selected)
                    or stored_plan.get("mode") not in {"full", "partial"}
                    or (full and selected != list(range(1, info["total"] + 1)))):
                    raise IndexerError("RETRY_PLAN_CHANGED", "저장된 다시 읽기 범위를 확인할 수 없습니다.", 409)
            wait_for_results = bool(unavailable and full and not stored_plan)
            supported = full or getattr(parser, "supports_pdf_page_subset", False)
            unknown_submission = bool(stored_plan and not checkpoint.get("result") and not checkpoint.get("request_id"))
            if wait_for_results or not selected or (full and len(selected) > retry_pages) or not supported or unknown_submission:
                reason = ("results_unavailable" if wait_for_results or (not confirmed and unavailable) else
                          "retry_outcome_unknown" if unknown_submission else
                          "retry_limit" if not selected or (full and len(selected) > retry_pages) else "unsupported")
                if not (ocr_only and not confirmed and not unavailable and not stored_plan):
                    attempts.append({"mode": "full" if full else "partial", "pages": selected,
                                     "status": "skipped", "reason": reason})
            else:
                attempt = {"mode": "full" if full else "partial", "pages": selected, "ocr": "force" if force_supported else "auto",
                           "dpi": state.get("settings", {}).get("dpi"), "status": "requested"}
                attempts.append(attempt)
                if on_quality:
                    on_quality(state, elements, coverage_value(), attempts)
                try:
                    plan = {"pages": selected, "mode": "full" if full else "partial"}
                    if on_provider and not stored_plan:
                        on_provider(state, "retry", "plan", {"plan": plan})
                    saved = checkpoint.get("result")
                    refresh = bool(saved and saved.get("unavailable") and checkpoint.get("request_id")
                                   and state.get("recovery_mode") == "retry")
                    if saved and not refresh:
                        result = ParseResult(saved["provider"], saved["elements"], saved["raw"],
                                             failed=set(saved.get("failed", [])),
                                             unavailable=set(saved.get("unavailable", [])),
                                             incomplete=saved.get("incomplete", False),
                                             terminal_unavailable=set(saved.get("terminal_unavailable", [])))
                        attempt["reused"] = True
                    else:
                        if on_provider and not checkpoint.get("submission_started"):
                            on_provider(state, "retry", "submission_started", {})
                        with TemporaryDirectory(dir=Path(state["path"]).parent) as directory:
                            path = Path(state["path"])
                            if not full:
                                sliced = Path(directory) / "retry.pdf"
                                copy_pages(path, sliced, selected)
                                path = sliced
                            result = parser.parse(ParseRequest(path, "pdf", ocr="force" if force_supported else "auto", dpi=state.get("settings", {}).get("dpi"),
                                page_count=len(selected), checkpoint=checkpoint,
                                persist=(lambda event, data: on_provider(state, "retry", event, data)) if on_provider else None,
                                progress=(lambda done, total: on_progress(state, done, total)) if on_progress else None))
                    retry_elements = deepcopy(result.elements)
                    for item in retry_elements:
                        page = item.get("page") if isinstance(item, dict) else None
                        if type(page) is not int or not 1 <= page <= len(selected):
                            raise ValueError("재시도 파싱 결과의 페이지 정보가 유효하지 않습니다")
                        if not full:
                            item["page"] = selected[page - 1]
                    def source_pages(pages):
                        if any(type(page) is not int or not 1 <= page <= len(selected) for page in pages):
                            raise ValueError("재시도 페이지 범위가 유효하지 않습니다")
                        return {page if full else selected[page - 1] for page in pages}
                    retry = QualityParseResult(retry_elements, {item["page"] for item in retry_elements},
                                               failed=source_pages(result.failed), total=len(selected),
                                               unavailable=source_pages(result.unavailable), incomplete=result.incomplete)
                    if on_provider and (not saved or refresh):
                        on_provider(state, "retry", "result", {"provider": result.provider,
                            "elements": result.elements, "raw": result.raw, "failed": sorted(result.failed),
                            "unavailable": sorted(result.unavailable), "incomplete": result.incomplete,
                            "terminal_unavailable": sorted(result.terminal_unavailable)})
                    terminal_unavailable.update(source_pages(result.terminal_unavailable))
                    report = merge_retry(report, retry, "pdf")
                    attempt.update({"status": "completed", "result": result.raw,
                                    "unavailable_pages": sorted(source_pages(result.unavailable)),
                                    "terminal_unavailable_pages": sorted(source_pages(result.terminal_unavailable))})
                except (httpx.HTTPError, IndexerError, ValueError) as error:
                    if isinstance(error, IndexerError) and error.code in {"PARSE_RESULTS_UNAVAILABLE", "PROVIDER_TIMEOUT", "RETRY_PLAN_CHANGED"}:
                        raise
                    if isinstance(error, IndexerError) and error.code == "PARSE_REQUEST_TERMINATED":
                        attempt.update({"status": "terminated", "reason": "remote_terminated"})
                    else:
                        attempt.update({"status": "failed", "error_type": type(error).__name__})
        coverage = coverage_value()
        if on_quality:
            on_quality(state, report["elements"], coverage, attempts)
        return {"elements": report["elements"], "coverage": coverage}

    def represent(state: ProcessingState):
        elements = normalize_elements(state["elements"])
        from indexer.office_locations import apply_source_locations
        from indexer.visual_relations import apply_visual_relations, preserve_visual_relations
        apply_source_locations(elements, state["elements"], state.get("office_details", {}))
        apply_visual_relations(elements)
        if state.get('image_edit'):
            preserve_visual_relations(elements, state['image_edit'].get('previous_elements', []))
        elif state.get('reused_parse', {}).get('previous_elements'):
            previous = state['reused_parse']['previous_elements']
            if len(previous) != len(elements):
                raise ValueError('기존 요소와 재사용한 파싱 결과가 일치하지 않습니다')
            for element, old in zip(elements, previous, strict=True):
                element['previous_element_id'] = old['element_id']
            preserve_visual_relations(elements, previous)
        settings = state.get("settings", {})
        chunk_defaults = CHUNK_DEFAULTS[state.get("profile", "general")]
        chunks = make_chunks(state["document_id"], state["job_id"], elements,
                             settings.get("target_chars", chunk_defaults["target_chars"]), settings.get("overlap_chars", chunk_defaults["overlap_chars"]), state.get("profile", "general"),
                             state.get("embedding_max_input_bytes", 3800))
        if on_represented:
            on_represented(state, elements, chunks)
        return {"normalized": elements, "chunks": chunks}

    def embedding(state: ProcessingState):
        return on_embedding(state) if on_embedding else {}

    def index(state: ProcessingState):
        return on_index(state) if on_index else {}

    graph = StateGraph(ProcessingState)
    graph.add_node("inspect", inspect)
    graph.add_node("parse", parse)
    graph.add_node("quality", quality)
    graph.add_node("represent", represent)
    graph.add_node("embedding", embedding)
    graph.add_node("index", index)
    graph.add_conditional_edges(START, lambda state: "embedding" if state.get("resume_index") else "inspect",
                                {"embedding": "embedding", "inspect": "inspect"})
    graph.add_edge("inspect", "parse")
    graph.add_edge("parse", "quality")
    graph.add_edge("quality", "represent")
    graph.add_edge("represent", "embedding")
    graph.add_edge("embedding", "index")
    graph.add_edge("index", END)
    return graph.compile(name="document_ingestion")
