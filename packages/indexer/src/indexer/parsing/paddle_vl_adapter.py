"""PaddleOCR-VL serving API adapter."""

import base64
import json
import re
from html import escape

import httpx
from bs4 import BeautifulSoup, NavigableString

from indexer.contracts import IndexerError
from indexer.parsing.contracts import ParseResult
from indexer.parsing.normalization import parse_table_html


class PaddleVLParser:
    supports_pdf_page_subset = True
    supported_formats = frozenset({"pdf"})
    supports_dpi = False
    supports_force_ocr = False

    def __init__(self, config, client):
        self.config = config
        self.client = client
        try:
            self.image_timeout_seconds = float(config.options.get('image_timeout_seconds', 90))
            if not 1 <= self.image_timeout_seconds <= 3600:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise IndexerError('INVALID_SETTINGS', '이미지 요청 제한 시간은 1~3,600초로 설정해 주세요.', 422) from error
        self.image_timeout_seconds = min(self.image_timeout_seconds, config.sync_timeout)

    def parse(self, request):
        if request.format.lower() not in {"pdf", "png", "jpg", "jpeg"}:
            raise IndexerError("PARSER_FORMAT_UNSUPPORTED", "PaddleOCR-VL은 현재 PDF만 지원합니다. 다른 형식은 다른 파서를 사용해 주세요.", 415)
        if request.dpi is not None:
            raise IndexerError("DPI_UNSUPPORTED", "PaddleOCR-VL PDF 요청은 DPI 설정을 지원하지 않습니다. DPI를 비워 주세요.", 422)
        options = {}
        for key, value in self.config.options.items():
            if key in {"endpoint_path", "image_timeout_seconds"}:
                continue
            if key.lower() in {"file", "filetype"}:
                raise IndexerError("INVALID_SETTINGS", "파일과 파일 형식은 파서 옵션으로 지정할 수 없습니다.", 422)
            try:
                options[key] = json.loads(value)
            except (ValueError, TypeError):
                options[key] = value
        if request.format.lower() in {'png', 'jpg', 'jpeg'}:
            # Standalone visual analysis must request recognition within image
            # and chart blocks; provider defaults can leave both unprocessed.
            options.setdefault('useOcrForImageBlock', True)
            options.setdefault('useChartRecognition', True)
        payload = {**options, "file": base64.b64encode(request.path.read_bytes()).decode("ascii"),
                   "fileType": 0 if request.format.lower() == "pdf" else 1, "visualize": options.get("visualize", False)}
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
        endpoint_path = self.config.options.get("endpoint_path", "/ocr/vl/layout-parsing")
        if not endpoint_path.startswith("/") or endpoint_path.startswith("//") or "?" in endpoint_path or "#" in endpoint_path:
            raise IndexerError("INVALID_SETTINGS", "파서 요청 경로를 확인해 주세요.", 422)
        try:
            response = self.client.post(self.config.base_url.rstrip("/") + endpoint_path,
                                        headers=headers, json=payload, timeout=request.timeout_seconds or self.config.sync_timeout)
            response.raise_for_status()
            raw = response.json()
        except httpx.TimeoutException as error:
            raise IndexerError("PARSER_SYNC_TIMEOUT", "파서 응답 시간이 초과되었습니다. 서버 작업 상태를 확인한 뒤 다시 요청해 주세요.", 503) from error
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            advice = ("인증 설정을 확인해 주세요." if code in (401, 403) else
                      "파서 API의 전체 요청 주소를 확인해 주세요. 게이트웨이 문서 주소가 아닌 파싱 경로가 필요합니다."
                      if code in (301, 302, 303, 307, 308) else "파서 주소와 서버 상태를 확인해 주세요.")
            raise IndexerError("PARSER_HTTP_ERROR", f"문서 파싱 요청에 실패했습니다 (HTTP {code}). {advice}", 502) from error
        except (ValueError, httpx.HTTPError) as error:
            raise IndexerError("PARSE_RESPONSE_INVALID", "파서 응답 또는 연결을 확인할 수 없습니다.", 503) from error
        if not isinstance(raw, dict) or raw.get("errorCode") != 0 or not isinstance(raw.get("result"), dict):
            raise IndexerError("PARSE_RESPONSE_INVALID", "파서가 유효한 결과를 반환하지 않았습니다.", 502)
        pages = raw["result"].get("layoutParsingResults")
        if not isinstance(pages, list) or (request.page_count is not None and len(pages) > request.page_count):
            raise IndexerError("PARSE_RESPONSE_INVALID", "파싱 결과의 페이지 범위가 올바르지 않습니다.", 502)
        elements = []
        for page_number, page in enumerate(pages, 1):
            parsed = page.get("prunedResult") if isinstance(page, dict) else None
            blocks = parsed.get("parsing_res_list") if isinstance(parsed, dict) else None
            if not isinstance(blocks, list):
                raise IndexerError("PARSE_RESPONSE_INVALID", "페이지의 파싱 요소를 확인할 수 없습니다.", 502)
            for block in blocks:
                if not isinstance(block, dict) or not isinstance(block.get("block_content"), str) or not isinstance(block.get("block_label"), str):
                    raise IndexerError("PARSE_RESPONSE_INVALID", "파싱 요소 형식이 올바르지 않습니다.", 502)
                label, content = block["block_label"], block["block_content"]
                category = {"doc_title": "heading", "paragraph_title": "heading", "text": "paragraph",
                            "table": "table", "image": "figure", "chart": "chart",
                            "header": "page_header", "footer": "page_footer", "number": "page_number"}.get(label, label)
                if category == 'figure' and (re.fullmatch(r'!\[[^]]*\]\([^)]+\)', content.strip())
                        or re.fullmatch(r'(?:[\w./-]+)\.(?:png|jpe?g|webp)', content.strip(), re.IGNORECASE)):
                    content = ''
                html = content if category == "table" and "<table" in content.lower() else ""
                if html and "\\n" in html:
                    soup = parse_table_html(html)
                    for node in list(soup.find_all(string=True)):
                        if "\\n" in node:
                            node.replace_with(NavigableString(str(node).replace("\\n", "\n")))
                    html = str(soup)
                if category == "heading":
                    html = f"<h1>{escape(content)}</h1>"
                elements.append({"id": block.get("block_id"), "page": page_number, "category": category,
                                 "content": {"text": content, "html": html}, "coordinates": block.get("block_bbox"),
                                 "_provider": "paddle_vl", "_source_label": label,
                                 "_page_width": parsed.get("width"), "_page_height": parsed.get("height")})
            if request.progress:
                request.progress(page_number, request.page_count)
        # The API returns ordered results, but does not identify omitted source pages.
        # When the count differs, positional page assignments cannot be verified.
        unavailable = (set(range(1, request.page_count + 1))
                       if request.page_count and len(pages) != request.page_count else set())
        if unavailable:
            elements = []
        textless = any(item["category"] in {"figure", "chart"} and not BeautifulSoup(item["content"]["text"], "html.parser").get_text(" ", strip=True) for item in elements)
        if textless:
            raw = {**raw, "warnings": ["글자가 확인되지 않은 이미지가 있습니다. 그림 속 글자는 파서 설정과 인식 결과에 따라 누락될 수 있습니다."]}
        return ParseResult("paddle_vl", elements, raw, unavailable=unavailable, incomplete=bool(unavailable))
