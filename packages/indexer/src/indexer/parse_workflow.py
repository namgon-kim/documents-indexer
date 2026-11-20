from bs4 import BeautifulSoup

from indexer.contracts import IndexerError
from indexer.inspection import is_page_label
from indexer.parsing.normalization import FIGURES, PAGE_FURNITURE, image_alternatives


def retry_plan(total: int, missing: set[int], page_budget: int, *, allow_full=True, priority=()):
    if not missing or page_budget <= 0:
        return None, []
    if allow_full and 2 * len(missing) > total and total <= page_budget:
        return "full", list(range(1, total + 1))
    priority = set(priority)
    selected = sorted(missing, key=lambda page: (page not in priority, page))[:page_budget]
    return "partial", sorted(selected)


def extractable_text(raw, *, body_only=False):
    content = raw.get("content") or {}
    category = raw.get("category")
    category = category if isinstance(category, str) else "other"
    if body_only and category in PAGE_FURNITURE | {"caption"}:
        return False
    text = (content.get("text") or "").strip()
    if body_only and category in FIGURES:
        html = BeautifulSoup(content.get("html") or "", "html.parser")
        if any(image_alternatives(html)):
            return True
        captions = html.find_all("figcaption")
        caption_text = " ".join(" ".join(node.stripped_strings) for node in captions)
        for caption in captions:
            caption.decompose()
        if text:
            text = " ".join(text.split())
            if caption_text:
                text = text.removeprefix(caption_text).strip()
        return any(value and not is_page_label(value) for value in (text, html.get_text(" ", strip=True)))
    if text and (not body_only or category == "table" or not is_page_label(text)):
        return True
    html = BeautifulSoup(content.get("html") or "", "html.parser")
    text = html.get_text(" ", strip=True)
    if text and (not body_only or category == "table" or not is_page_label(text)):
        return True
    return category in FIGURES and any(image_alternatives(html))


def missing_ocr_body(elements, scan_candidates):
    return scan_candidates - {
        row["page"]
        for row in elements
        if row["page"] in scan_candidates and extractable_text(row, body_only=True)
    }


def inspect_parse(result, inspection, max_pages):
    total = inspection.pages if inspection.pages is not None else result.total
    if total is not None and (type(total) is not int or not 1 <= total <= max_pages):
        raise IndexerError("PAGE_LIMIT", "문서 페이지 범위가 서버 제한을 초과했습니다.", 413)
    present = {row["page"] for row in result.elements}
    unavailable = result.unavailable - inspection.blank
    missing = result.failed | unavailable
    if total is not None:
        if any(page > total for page in present | missing):
            raise IndexerError("PARSE_RESPONSE_INVALID", "파싱 페이지가 원본 범위를 벗어났습니다.", 503)
        missing |= set(range(1, total + 1)) - present
    ocr_missing = missing_ocr_body(result.elements, inspection.scan_candidates) - inspection.blank
    missing = (missing | ocr_missing) - inspection.blank
    return {
        "elements": sorted(result.elements, key=lambda row: row["page"]),
        "total": total,
        "missing": sorted(missing),
        "ocr_missing": sorted(ocr_missing),
        "visual_without_text": sorted({row["page"] for row in result.elements
            if row.get("category") in FIGURES | {"image"}
            and not extractable_text(row, body_only=True)}),
        "unavailable": sorted(unavailable),
        "blank": sorted(inspection.blank),
        "incomplete": result.incomplete,
        "issues": [],
    }


def merge_retry(report, retry, kind):
    unavailable = set(report["unavailable"])
    missing = set(report["missing"])
    ocr_missing = set(report.get("ocr_missing", []))
    recovered = {
        row["page"]
        for row in retry.elements
        if row["page"] in missing - unavailable - retry.failed - retry.unavailable
        and extractable_text(row, body_only=row["page"] in ocr_missing)
    }
    unavailable |= retry.unavailable & missing
    report["unavailable"] = sorted(unavailable)
    if kind == "pdf":
        report["elements"] = sorted(
            [row for row in report["elements"] if row["page"] not in recovered]
            + [row for row in retry.elements if row["page"] in recovered],
            key=lambda row: row["page"],
        )
        report["missing"] = sorted(missing - recovered)
        report["ocr_missing"] = sorted(ocr_missing - recovered)
        report["visual_without_text"] = sorted(set(report.get("visual_without_text", [])) - recovered)
    elif recovered and not retry.incomplete and not retry.failed:
        previous_pages = {row["page"] for row in report["elements"]}
        new_pages = {row["page"] for row in retry.elements}
        if previous_pages <= new_pages and report["total"] == retry.total:
            # Rendered HWP page coordinates belong to one conversion.
            report["elements"] = sorted(retry.elements, key=lambda row: row["page"])
            report["missing"] = sorted(missing - new_pages)
            report["incomplete"] = bool(report["missing"])
    return report
