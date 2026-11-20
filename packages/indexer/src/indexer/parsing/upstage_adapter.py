import re
import time
from ipaddress import ip_address

import httpx

from indexer.contracts import IndexerError
from indexer.parsing.contracts import ParserConfig, ParseRequest, ParseResult


class UpstageParser:
    """Document Parse 요청과 장문 결과 조회를 담당하는 어댑터."""

    supports_pdf_page_subset = True
    supported_formats = frozenset({'pdf', 'hwp', 'hwpx', 'xlsx', 'pptx'})
    max_pdf_pages = 2000

    def __init__(self, config: ParserConfig, client: httpx.Client, *, remote_timeout=90, job_timeout=900):
        self.config = config
        self.client = client
        self.remote_timeout, self.job_timeout = remote_timeout, job_timeout

    def parse(self, request: ParseRequest) -> ParseResult:
        if not self.config.api_key:
            raise ValueError("PARSER_API_KEY를 설정해 주세요")
        if request.format.lower() not in {"pdf", "hwp", "hwpx", "xlsx", "pptx", "xls", "ppt"}:
            raise ValueError("지원하지 않는 문서 형식입니다")
        if request.ocr not in {"auto", "force"} or (request.dpi is not None and not 50 <= request.dpi <= 600):
            raise ValueError("OCR 또는 DPI 설정을 확인해 주세요")
        if request.format.lower() == "pdf" and request.page_count and request.page_count > self.max_pdf_pages:
            raise ValueError("문서 페이지가 파서의 처리 범위를 초과했습니다")
        data = {"model": self.config.model, "mode": self.config.mode, "ocr": request.ocr,
                "output_formats": '["html","text"]', "coordinates": "true", **self.config.options}
        if request.dpi is not None:
            data["dpi"] = str(request.dpi)
        if request.format.lower() in {"hwp", "hwpx"} or (request.format.lower() == "pdf" and request.page_count and request.page_count > 100):
            return self._parse_long(request, data)
        try:
            with request.path.open("rb") as stream:
                response = self.client.post(self.config.base_url + "/document-digitization",
                    headers={"Authorization": f"Bearer {self.config.api_key}"}, data=data,
                    files={"document": (request.path.name, stream)}, timeout=self.config.sync_timeout)
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise IndexerError("PARSER_SYNC_TIMEOUT", "문서 읽기 응답이 지연되었습니다. 접수 여부를 확인할 수 없으며 다시 요청하면 비용이 중복될 수 있습니다.", 503) from error
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            advice = ("파서 인증 정보를 확인해 주세요." if code in {401, 403} else
                      "파서 API 주소와 요청 경로를 확인해 주세요." if code == 404 else
                      "문서가 파서 또는 프록시의 크기 제한을 초과했는지 확인해 주세요." if code == 413 else
                      "파서 요청 한도에 도달했습니다. 잠시 후 다시 확인해 주세요." if code == 429 else
                      "파서 또는 프록시의 오류를 확인해 주세요." if code >= 500 else
                      "문서 형식과 파서 설정을 확인해 주세요.")
            raise IndexerError("PARSER_HTTP_ERROR", f"문서 읽기 요청에 오류가 발생했습니다 (HTTP {code}). {advice}", 502) from error
        except httpx.HTTPError as error:
            raise IndexerError("PARSER_CONNECTION_FAILED", "파서 연결에 실패했습니다. 요청 접수 여부를 확인한 후 다시 요청해 주세요.", 503) from error
        try:
            result = response.json()
        except ValueError as error:
            raise IndexerError("PARSE_RESPONSE_INVALID", "파서 응답 형식을 확인할 수 없습니다. 파서와 프록시 설정을 확인해 주세요.", 502) from error
        if not isinstance(result, dict) or not isinstance(result.get("elements"), list):
            raise IndexerError("PARSE_RESPONSE_INVALID", "파싱 응답에 문서 요소가 없습니다. 파서와 프록시 설정을 확인해 주세요.", 502)
        return ParseResult(provider="upstage", elements=result["elements"], raw=result)

    def _parse_long(self, request, data):
        checkpoint = request.checkpoint or {}
        request_id = checkpoint.get("request_id")
        save = request.persist or (lambda *_: None)
        if not request_id:
            if checkpoint.get("started"):
                raise IndexerError("REMOTE_SUBMISSION_UNKNOWN", "문서 접수 여부를 확인할 수 없습니다. 작업 내역을 확인해 주세요.", 503)
            save("started", {})
            try:
                with request.path.open("rb") as stream:
                    response = self.client.post(self.config.base_url + "/document-digitization/async",
                        headers={"Authorization": f"Bearer {self.config.api_key}"}, data=data,
                        files={"document": (request.path.name, stream)}, timeout=self.remote_timeout)
                response.raise_for_status()
                request_id = response.json().get("request_id")
            except httpx.HTTPStatusError as error:
                if error.response.status_code in {400, 401, 403, 413, 415, 422, 429}:
                    code = error.response.status_code
                    message = ("파서 인증 정보를 확인해 주세요." if code in {401, 403} else
                               "파서 또는 프록시의 요청 크기 제한을 확인해 주세요." if code == 413 else
                               "파서 요청 한도에 도달했습니다. 잠시 후 다시 요청해 주세요." if code == 429 else
                               "파서가 문서 또는 설정을 거절했습니다. 파일과 설정을 확인해 주세요.")
                    raise IndexerError("PARSER_REQUEST_REJECTED", message, 503) from error
                raise IndexerError("REMOTE_SUBMISSION_UNKNOWN", "문서 접수 여부를 확인할 수 없습니다. 작업 내역을 확인해 주세요.", 503) from error
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                raise IndexerError("PARSER_CONNECTION_FAILED", "파서에 연결하지 못했습니다. 연결 설정을 확인한 후 다시 요청해 주세요.", 503) from error
            except (httpx.HTTPError, ValueError, AttributeError) as error:
                raise IndexerError("REMOTE_SUBMISSION_UNKNOWN", "문서 접수 여부를 확인할 수 없습니다. 작업 내역을 확인해 주세요.", 503) from error
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
                raise IndexerError("PARSE_RESPONSE_INVALID", "문서 처리 접수 정보를 확인할 수 없습니다.", 503)
            save("request_id", {"request_id": request_id})
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise IndexerError("PARSE_RESPONSE_INVALID", "문서 처리 접수 정보가 올바르지 않습니다.", 503)

        batches = {}
        expected_pages = request.page_count
        page_limit = expected_pages if expected_pages is not None else self.max_pdf_pages
        for item in checkpoint.get("batches", []):
            start, end = item.get("start"), item.get("end")
            payload = item.get("result")
            rows = payload.get("elements") if isinstance(payload, dict) else None
            if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= page_limit
                or not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get("page")) is not int
                                                   or not start <= row["page"] <= end for row in rows)
                or any(start <= old_end and old_start <= end for old_start, old_end in batches)):
                raise IndexerError("PARSE_RESPONSE_INVALID", "저장된 문서 처리 결과를 확인할 수 없습니다.", 503)
            batches[start, end] = payload
        deadline = time.monotonic() + self.job_timeout
        download_errors = {}
        poll_errors = 0
        while time.monotonic() < deadline:
            try:
                response = self.client.get(self.config.base_url + "/document-digitization/requests/" + request_id,
                    headers={"Authorization": f"Bearer {self.config.api_key}"}, timeout=self.remote_timeout)
                response.raise_for_status()
                state = response.json()
            except httpx.HTTPError as error:
                code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                if code == 410:
                    raise IndexerError("PARSE_REQUEST_TERMINATED", "원격 처리 결과가 더 이상 제공되지 않습니다. 저장된 결과를 사용하거나 문서를 다시 읽어 주세요.", 410) from error
                transient = code is None or code == 429 or 500 <= code < 600
                poll_errors += 1
                if transient and poll_errors <= 3 and time.monotonic() < deadline:
                    time.sleep(min(2 ** (poll_errors - 1), max(0, deadline - time.monotonic())))
                    continue
                message = "파서 인증 정보를 확인해 주세요." if code in {401, 403} else "문서 처리 결과를 확인하지 못했습니다. 이어서 확인할 수 있습니다."
                raise IndexerError("PARSE_RESULTS_UNAVAILABLE", message, 503) from error
            except ValueError as error:
                raise IndexerError("PARSE_RESPONSE_INVALID", "문서 처리 상태 응답을 확인할 수 없습니다.", 503) from error
            poll_errors = 0
            if not isinstance(state, dict) or not isinstance(state.get("status"), str) or not isinstance(state.get("batches"), list):
                raise IndexerError("PARSE_RESPONSE_INVALID", "문서 처리 상태를 확인할 수 없습니다.", 503)
            provider_pages = state.get("total_pages")
            if provider_pages not in (None, 0):
                if type(provider_pages) is not int or not 1 <= provider_pages <= self.max_pdf_pages:
                    raise IndexerError("PARSER_PAGE_LIMIT", "파서가 처리 가능한 페이지 범위를 초과했습니다.", 413)
                if expected_pages is not None and provider_pages != expected_pages:
                    raise IndexerError("PARSE_RESPONSE_INVALID", "원격 페이지 수가 원본 또는 이전 결과와 일치하지 않습니다.", 503)
                expected_pages = provider_pages
                page_limit = expected_pages
                if any(end > page_limit for _, end in batches):
                    raise IndexerError("PARSE_RESPONSE_INVALID", "저장된 배치가 원격 페이지 범위를 벗어났습니다.", 503)
            spans = set()
            records = []
            for batch in state["batches"]:
                if not isinstance(batch, dict):
                    raise IndexerError("PARSE_RESPONSE_INVALID", "문서 처리 범위가 올바르지 않습니다.", 503)
                start, end = batch.get("start_page"), batch.get("end_page")
                if type(start) is not int or type(end) is not int or not 1 <= start <= end <= page_limit or not isinstance(batch.get("status"), str):
                    raise IndexerError("PARSE_RESPONSE_INVALID", "문서 처리 범위가 올바르지 않습니다.", 503)
                pages = set(range(start, end + 1))
                if spans & pages:
                    raise IndexerError("PARSE_RESPONSE_INVALID", "문서 처리 범위가 중복되었습니다.", 503)
                if any(start <= old_end and old_start <= end and (start, end) != (old_start, old_end)
                       for old_start, old_end in batches):
                    raise IndexerError("PARSE_RESPONSE_INVALID", "이전에 받은 문서 처리 범위가 변경되었습니다.", 503)
                spans |= pages
                records.append((start, end, batch))
                if (start, end) in batches or batch["status"] != "completed" or download_errors.get((start, end), 0) >= 3:
                    continue
                try:
                    url = httpx.URL(batch.get("download_url", ""))
                    base = httpx.URL(self.config.base_url)
                    same_origin = (url.scheme == base.scheme and url.host == base.host and url.port == base.port)
                    trusted_proxy = same_origin and base.scheme == "http"
                    if (not url.host or url.userinfo or url.fragment or
                        (not trusted_proxy and (url.scheme != "https" or url.host in {"localhost", "localhost.localdomain"}
                                                or url.host.endswith(".localhost")))):
                        raise ValueError("invalid result URL")
                    try:
                        address = ip_address(url.host)
                    except ValueError:
                        address = None
                    if not trusted_proxy and address is not None and not address.is_global:
                        raise ValueError("invalid result URL")
                    headers = {"Authorization": f"Bearer {self.config.api_key}"} if same_origin else {}
                    downloaded = self.client.get(url, headers=headers, timeout=self.remote_timeout, follow_redirects=False)
                    downloaded.raise_for_status()
                    payload = downloaded.json()
                    rows = payload.get("elements") if isinstance(payload, dict) else None
                    if not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get("page")) is not int or not start <= row["page"] <= end for row in rows):
                        raise ValueError("invalid result")
                except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError):
                    download_errors[(start, end)] = download_errors.get((start, end), 0) + 1
                    continue
                save("batch", {"start": start, "end": end, "result": payload})
                batches[(start, end)] = payload
            if request.progress:
                request.progress(sum(end - start + 1 for start, end in batches), expected_pages)
            finished = state["status"] in {"completed", "failed", "cancelled", "canceled"}
            pending = any(batch["status"] == "completed" and (start, end) not in batches and download_errors.get((start, end), 0) < 3 for start, end, batch in records)
            if finished and not pending:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        else:
            raise IndexerError("PROVIDER_TIMEOUT", "문서 읽기가 오래 걸리고 있습니다. 이어서 확인할 수 있습니다.", 503)

        downloaded_pages = {page for start, end in batches for page in range(start, end + 1)}
        failed = {page for start, end, batch in records if batch["status"] == "failed" for page in range(start, end + 1)} - downloaded_pages
        if expected_pages is None:
            if state["status"] in {"failed", "cancelled", "canceled"} and not batches:
                raise IndexerError("PARSE_REQUEST_TERMINATED", "원격 처리가 종료되어 문서 결과를 받을 수 없습니다. 파일과 파서 설정을 확인해 주세요.", 410)
            raise IndexerError("PARSE_RESULTS_UNAVAILABLE", "문서 페이지 수를 확인하지 못했습니다. 이어서 확인할 수 있습니다.", 503)
        unavailable = set(range(1, expected_pages + 1)) - downloaded_pages - failed
        rows = [row for span in sorted(batches) for row in batches[span]["elements"]]
        downloadable = {page for start, end, batch in records if batch["status"] == "completed"
                        for page in range(start, end + 1)}
        terminal = (unavailable - downloadable if state["status"] in {"failed", "cancelled", "canceled"} else set())
        if not batches and unavailable and terminal == unavailable:
            raise IndexerError("PARSE_REQUEST_TERMINATED", "원격 처리가 종료되어 결과를 받을 수 없습니다. 문서를 다시 읽어 주세요.", 410)
        if not batches and unavailable:
            raise IndexerError("PARSE_RESULTS_UNAVAILABLE", "문서 처리 결과를 가져오지 못했습니다. 이어서 확인할 수 있습니다.", 503)
        return ParseResult("upstage", rows, {"request_id": request_id, "total_pages": expected_pages, "status": state["status"], "batches": [
            {"start_page": start, "end_page": end, "result": batches[start, end]} for start, end in sorted(batches)]},
            failed=failed, unavailable=unavailable, incomplete=bool(unavailable or state["status"] != "completed"),
            terminal_unavailable=terminal)
