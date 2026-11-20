import fcntl
import json
import tempfile
import threading
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from indexer.artifact_store import ArtifactStore
from indexer.auth_store import AuthStore
from indexer.chat import RagChat
from indexer.config import Config
from indexer.contracts import ChatRequest, IndexerError, ProcessingSettings, SearchRequest
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.documents import CHUNK_DEFAULTS, select_profile
from indexer.embedding_profiles import EmbeddingProfiles
from indexer.index_publication import IndexPublisher
from indexer.ingestion_service import DuplicateDocument, IngestionFailure, IngestionService
from indexer.parser_profiles import FIELDS as PARSER_PROFILE_FIELDS
from indexer.parser_profiles import ParserProfiles
from indexer.parsing.contracts import ParserConfig
from indexer.parsing.registry import build_registry
from indexer.parsing.settings import parse_options
from indexer.published_retrieval import PublishedRetrieval
from indexer.reranker import CohereReranker
from indexer.settings import (
    ServerSettings,
    apply_overrides,
    choices,
    effective_values,
    public_settings,
    save_overrides,
    values,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from qdrant_client import QdrantClient
from starlette.concurrency import run_in_threadpool

from apps.api.auth import Auth, EmbedExchange, Login
from apps.api.base_path import StripBasePath, with_base_tag

SUFFIXES = {"pdf", "hwp", "hwpx"}


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list, max_length=50)


class IngestionSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    method: Literal["bm25", "dense", "hybrid"] = "hybrid"
    limit: int = Field(default=5, ge=1, le=20)
    document_id: UUID | None = None
    rerank: bool = False
    index_version: UUID | None = None
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)


class ReprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_job_id: UUID
    mode: Literal["reparse", "rechunk", "complete_images"]
    settings: dict = Field(default_factory=dict)
    embedding_profile_id: str | None = Field(default=None, min_length=1, max_length=64)


class ImageActionsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actions: dict[str, Literal['analyze', 'original', 'both', 'exclude', 'reuse', 'describe']] = Field(min_length=1, max_length=100)
    descriptions: dict[str, str] = Field(default_factory=dict, max_length=100)
    embedding_profile_id: str | None = Field(default=None, min_length=1, max_length=64)


def error_response(request, error):
    return JSONResponse(
        status_code=error.status,
        content={
            "error": {"code": error.code, "message": error.message, "request_id": request.state.request_id}
        },
    )


def settings_from(payload, kind, defaults=None):
    try:
        combined = (defaults or {}) | payload
        if defaults and "target_chars" in payload and "overlap_chars" not in payload and type(payload["target_chars"]) is int:
            combined["overlap_chars"] = min(defaults["overlap_chars"], payload["target_chars"] // 4)
        settings = ProcessingSettings.model_validate(combined)
    except ValidationError as exc:
        raise IndexerError("INVALID_SETTINGS", "OCR 설정과 청크 크기·겹침 범위를 확인해 주세요.") from exc
    if settings.ocr_policy == "force" and kind != "pdf":
        raise IndexerError("OCR_POLICY_UNSUPPORTED", "스캔 문서 지정은 PDF에서 사용할 수 있습니다.")
    if settings.dpi is not None and kind != "pdf":
        raise IndexerError("DPI_UNSUPPORTED", "DPI 설정은 PDF에서 사용할 수 있습니다.")
    return settings.model_dump()


def create_app(config: Config | None = None, *, ingestion_service=None):
    base_config = config or Config.from_env()
    config = apply_overrides(base_config)
    store = AuthStore(config.data_dir)
    auth = Auth(config, store)
    stop = threading.Event()
    wake = threading.Event()
    operation_lock = threading.RLock()
    settings_lock = threading.Lock()
    parser_profiles_lock = threading.RLock()
    processing_defaults = {"target_chars": config.default_target_chars, "overlap_chars": config.default_overlap_chars,
                           "ocr_policy": config.default_ocr_policy, "dpi": config.default_dpi, "extraction_mode": "parser"}

    def install_pipeline(app, candidate, *, recover=False):
        http = app.state.http
        def recreate_parser(snapshot):
            saved_config = ParserConfig(**snapshot["config"])
            return build_registry(http, remote_timeout=snapshot.get("remote_timeout") or candidate.remote_timeout,
                                  job_timeout=snapshot.get("job_timeout") or candidate.job_timeout).create(saved_config)
        parser_config = ParserConfig(candidate.parser_provider, candidate.parser_base_url, candidate.parser_api_key,
                                     candidate.parser_model, candidate.parser_mode, parse_options(candidate.parser_options),
                                     candidate.parser_sync_timeout)
        provider = build_registry(http, remote_timeout=candidate.remote_timeout, job_timeout=candidate.job_timeout).create(parser_config)
        profile = EmbeddingProfile(candidate.embedding_profile, candidate.embedding_document_model,
                                   candidate.embedding_query_model, candidate.embedding_dimensions,
                                   candidate.embedding_max_input_bytes, candidate.embedding_batch_size)
        embedding = EmbeddingClient(http, candidate.embedding_base_url, candidate.embedding_api_key, profile)
        reranker = None
        if candidate.reranker_provider == "cohere":
            reranker = lambda load_text: CohereReranker(
                http, candidate.reranker_base_url, candidate.reranker_api_key, candidate.reranker_model, load_text)
        connection = (candidate.qdrant_url, candidate.qdrant_key if candidate.qdrant_url else "", candidate.remote_timeout if candidate.qdrant_url else None)
        qdrant = app.state.qdrant_by_connection.get(connection)
        if qdrant is None:
            qdrant = (QdrantClient(url=candidate.qdrant_url, api_key=candidate.qdrant_key or None,
                                   timeout=candidate.remote_timeout) if candidate.qdrant_url
                      else QdrantClient(path=str(candidate.data_dir / "qdrant")))
            app.state.qdrant_by_connection[connection] = qdrant
            app.state.qdrant_clients.append(qdrant)
        previous = getattr(app.state, "index_publisher", None)
        publisher = IndexPublisher(candidate.data_dir / "ingestion", qdrant, embedding, reranker,
                                   lock=getattr(previous, "lock", None), search_lock=getattr(previous, "search_lock", None))
        service = IngestionService(ArtifactStore(candidate.data_dir), provider, publisher,
                                   max_pages=candidate.max_pages, retry_pages=candidate.retry_pages,
                                   parser_factory=recreate_parser)
        profiles = EmbeddingProfiles(candidate.data_dir, publisher, http)
        profiles.list()
        service.embedding_profiles = profiles
        retrieval = PublishedRetrieval(publisher, service.storage)
        if recover:
            service.recover()
        return service, publisher, profiles, retrieval

    def publish_pipeline(app, candidate, pipeline):
        service, publisher, profiles, retrieval = pipeline
        app.state.ingestion = service
        app.state.index_publisher = publisher
        app.state.embedding_profiles = profiles
        app.state.published_retrieval = retrieval
        app.state.published_chat = RagChat(candidate, retrieval, app.state.http)

    @asynccontextmanager
    async def lifespan(app):
        # API and the worker share one writable data directory and one active snapshot builder.
        lock = (config.data_dir / "instance.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("The data directory already has a running DocumentIndexer") from None
        http = httpx.Client(follow_redirects=False)
        thread = None
        cleanup_lock = threading.Lock()
        cleaned = False

        def cleanup():
            nonlocal cleaned
            with cleanup_lock:
                if cleaned:
                    return
                cleaned = True
                for client in getattr(app.state, "qdrant_clients", []):
                    client.close()
                http.close()
                lock.close()

        try:
            app.state.http = http
            app.state.qdrant_clients = []
            app.state.qdrant_by_connection = {}
            app.state.chat = RagChat(config, None, http)
            if ingestion_service is not None:
                app.state.ingestion = ingestion_service
                app.state.index_publisher = ingestion_service.publisher
                if isinstance(ingestion_service.publisher, IndexPublisher):
                    app.state.embedding_profiles = EmbeddingProfiles(config.data_dir, ingestion_service.publisher, http)
                    ingestion_service.embedding_profiles = app.state.embedding_profiles
                app.state.published_retrieval = PublishedRetrieval(ingestion_service.publisher, ingestion_service.storage)
                app.state.published_chat = RagChat(config, app.state.published_retrieval, http)
                ingestion_service.recover()
            elif config.parser_provider:
                publish_pipeline(app, config, install_pipeline(app, config, recover=True))
            stop.clear()

            def worker():
                try:
                    while not stop.is_set():
                        with operation_lock:
                            processed = (app.state.ingestion.process_next() if hasattr(app.state, "ingestion") else False)
                        if not processed:
                            wake.wait(0.5)
                            wake.clear()
                finally:
                    if stop.is_set():
                        cleanup()

            thread = threading.Thread(target=worker, name="indexer-worker", daemon=True)
            thread.start()
            try:
                yield
            finally:
                stop.set()
                wake.set()
                await run_in_threadpool(thread.join, 2)
        finally:
            if thread is None or not thread.is_alive():
                cleanup()

    app = FastAPI(title="DocumentIndexer", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(IndexerError)
    async def handle_indexer(request, error):
        return error_response(request, error)

    @app.exception_handler(DuplicateDocument)
    async def duplicate_document(request: Request, error: DuplicateDocument):
        return JSONResponse(status_code=409, content={"error": {"code": "DUPLICATE_DOCUMENT",
            "message": "동일한 파일이 이미 있습니다. 기존 문서를 확인하거나 새 문서로 추가해 주세요."},
            "document_id": error.document["document_id"], "filename": error.document["filename"]})

    @app.exception_handler(IngestionFailure)
    async def ingestion_failure(request: Request, error: IngestionFailure):
        return JSONResponse(status_code=502, content={"error": {"code": "INGESTION_FAILED",
                            "message": "문서 처리에 실패했습니다. 작업 상태를 확인해 주세요.",
                            "request_id": request.state.request_id},
                            "document_id": error.document_id, "job_id": error.job_id})

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request, error):
        return error_response(request, IndexerError("INVALID_REQUEST", "요청 입력값을 확인해 주세요."))

    @app.middleware("http")
    async def request_headers(request, call_next):
        request.state.request_id = str(uuid4())
        if request.method == "POST" and request.url.path == "/v1/ingest":
            length = request.headers.get("content-length", "")
            if not length.isdigit():
                return error_response(
                    request, IndexerError("LENGTH_REQUIRED", "업로드 크기 정보가 필요합니다.", 411)
                )
            if int(length) > config.max_upload + 65536:
                return error_response(
                    request, IndexerError("UPLOAD_LIMIT", "파일 크기 제한을 초과했습니다.", 413)
                )
            try:
                auth.require(request)
            except IndexerError as error:
                return error_response(request, error)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/auth/config")
    def auth_config(request: Request):
        return {
            "dev_login": auth.local(request) and not config.access_key,
            "frame_origins": list(config.frame_origins),
        }

    @app.post("/v1/auth/login")
    def login(request: Request, response: Response, body: Login):
        token, csrf, secure = auth.login(request, body.access_key)
        response.set_cookie(
            "indexer_session", token, httponly=True, secure=secure, samesite="lax", max_age=28800
        )
        return {"csrf_token": csrf}

    @app.get("/v1/auth/session")
    def session(request: Request):
        return {"csrf_token": auth.session(request)["csrf"]}

    @app.post("/v1/auth/embed/exchange")
    def embed_exchange(request: Request, body: EmbedExchange):
        return auth.exchange(request, body)

    def rerank_available(request: Request):
        custom_endpoint = httpx.URL(config.reranker_base_url).host != "api.cohere.com"
        return bool(config.external_processing and getattr(getattr(request.app.state, "index_publisher", None), "reranker_factory", None)
                    and (config.reranker_api_key or custom_endpoint))

    @app.get("/v1/processing-options", dependencies=[Depends(auth.require)])
    def options(request: Request):
        parser = getattr(getattr(request.app.state, "ingestion", None), "parser", None)
        supports_dpi = getattr(parser, "supports_dpi", True)
        supports_force_ocr = getattr(parser, "supports_force_ocr", True)
        defaults = {**processing_defaults}
        if not supports_dpi:
            defaults["dpi"] = None
        if not supports_force_ocr:
            defaults["ocr_policy"] = "automatic"
        return {
            'current_parser': {'provider': config.parser_provider,
                               'name': next((p['name'] for p in ParserProfiles(config.data_dir).list(config) if p['active']), config.parser_provider)},
            "formats": sorted(set(getattr(parser, "supported_formats", SUFFIXES | {"xlsx", "pptx"})) | {"xlsx", "pptx", "hwpx", "hwp", "docx"}),
            "parser_formats": sorted(getattr(parser, "supported_formats", SUFFIXES | {"xlsx", "pptx"})),
            "native_visual_supported": getattr(getattr(parser, 'config', None), 'provider', '') == 'paddle_vl',
            "ingestion_enabled": hasattr(request.app.state, "ingestion"),
            "pdf_max_pages": (min(config.max_pages, getattr(request.app.state.ingestion.parser, "max_pdf_pages", config.max_pages))
                              if hasattr(request.app.state, "ingestion") else None),
            "max_upload_bytes": config.max_upload,
            "external_processing": config.external_processing,
            "search_methods": ["bm25", "dense", "hybrid"] if config.external_processing else ["bm25"],
            "rerank_available": rerank_available(request),
            "profiles": [{"profile_id": config.embedding_profile, "name": config.embedding_document_model, "dimensions": config.embedding_dimensions}],
            "defaults": ProcessingSettings.model_validate(defaults).model_dump(),
            "chunk_profile_defaults": {**CHUNK_DEFAULTS, "general": {key: defaults[key] for key in ("target_chars", "overlap_chars")}},
            "limits": {"target_chars": [400, 4000], "overlap_chars": [0, 400], "overlap_ratio": 0.25, "dpi": [50, 600]},
            "force_ocr_formats": ["pdf"] if supports_force_ocr else [],
            "supports_dpi": supports_dpi,
        }

    @app.post("/v1/ingest", dependencies=[Depends(auth.require)])
    def ingest_new(request: Request, file: Annotated[UploadFile, File()], profile: Annotated[str | None, Form()] = None,
                   settings: Annotated[str, Form()] = "{}", deferred: Annotated[bool, Form()] = False,
                   embedding_profile_id: Annotated[str | None, Form()] = None,
                   allow_duplicate: Annotated[bool, Form()] = False):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서와 임베딩 연결을 설정해 주세요.", 503)
        if not config.external_processing:
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 문서 처리가 비활성화되어 있습니다.", 403)
        filename = (file.filename or "").replace("\\", "/").split("/")[-1]
        extension = Path(filename).suffix.lower()
        if extension not in {".pdf", ".hwp", ".hwpx", ".xlsx", ".pptx", ".docx"}:
            raise IndexerError("INVALID_FORMAT", "지원하지 않는 문서 형식입니다.", 415)
        if profile not in {None, "general", "report", "manual", "spreadsheet", "slides", "scanned"}:
            raise IndexerError("INVALID_PROFILE", "문서 처리 프로필을 확인해 주세요.", 422)
        try:
            request.app.state.ingestion.publisher_for(embedding_profile_id, for_ingest=True)
        except ValueError as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", str(error), 422) from error
        try:
            requested = json.loads(settings)
        except ValueError as error:
            raise IndexerError("INVALID_SETTINGS", "처리 설정을 확인해 주세요.", 422) from error
        if not isinstance(requested, dict):
            raise IndexerError("INVALID_SETTINGS", "처리 설정은 JSON 객체여야 합니다.", 422)
        kind = extension.lstrip(".")
        defaults = {**processing_defaults}
        selected_profile = select_profile(Path(filename), profile).profile
        if selected_profile != "general":
            defaults.update(CHUNK_DEFAULTS[selected_profile])
        parser = request.app.state.ingestion.parser
        if kind != "pdf" or not getattr(parser, "supports_dpi", True):
            defaults["dpi"] = None
        if kind != "pdf" or not getattr(parser, "supports_force_ocr", True):
            defaults["ocr_policy"] = "automatic"
        if requested.get("dpi") is not None and not getattr(parser, "supports_dpi", True):
            raise IndexerError("DPI_UNSUPPORTED", "선택한 파서는 DPI 설정을 지원하지 않습니다.", 422)
        if (requested.get("ocr_policy") == "force" or profile == "scanned") and not getattr(parser, "supports_force_ocr", True):
            raise IndexerError("OCR_POLICY_UNSUPPORTED", "선택한 파서는 별도의 OCR 강제 설정을 지원하지 않습니다.", 422)
        effective = settings_from(requested, kind, defaults)
        mode = effective['extraction_mode']
        supported = getattr(parser, 'supported_formats', None)
        if mode == 'parser' and supported is not None and kind not in supported:
            raise IndexerError('PARSER_FORMAT_UNSUPPORTED', '선택한 파서가 이 형식을 지원하지 않습니다. 직접 추출 방식을 선택해 주세요.', 415)
        if mode != 'parser' and kind not in {'xlsx', 'pptx', 'hwpx', 'hwp', 'docx'}:
            raise IndexerError('NATIVE_FORMAT_UNSUPPORTED', '이 형식은 직접 추출을 지원하지 않습니다.', 415)
        if profile == "scanned" and kind != "pdf":
            raise IndexerError("OCR_POLICY_UNSUPPORTED", "스캔 문서 지정은 PDF에서 사용할 수 있습니다.")
        with tempfile.TemporaryDirectory(dir=store.staging) as temporary:
            path = Path(temporary) / filename
            size = 0
            with path.open("wb") as output:
                while chunk := file.file.read(128 * 1024):
                    size += len(chunk)
                    if size > config.max_upload:
                        raise IndexerError("UPLOAD_LIMIT", "파일 크기 제한을 초과했습니다.", 413)
                    output.write(chunk)
            if not size:
                raise IndexerError("INVALID_FORMAT", "빈 파일입니다.", 415)
            with path.open("rb") as stream:
                signature = stream.read(8)
            expected = b"%PDF-" if extension == ".pdf" else bytes.fromhex("D0CF11E0A1B11AE1") if extension == ".hwp" else b"PK\x03\x04"
            if not signature.startswith(expected):
                raise IndexerError("INVALID_FORMAT", "파일 내용과 확장자가 일치하지 않습니다.", 415)
            if extension in {".xlsx", ".pptx", ".hwpx", ".docx"}:
                try:
                    with zipfile.ZipFile(path) as archive:
                        members = archive.infolist()
                        if len(members) > 10000 or sum(item.file_size for item in members) > 500 * 1024 * 1024:
                            raise IndexerError("UPLOAD_LIMIT", "압축 해제 크기 또는 파일 개수 제한을 초과했습니다.", 413)
                        names = set(archive.namelist())
                        if (len(names) != len(members) or any(item.flag_bits & 1 or '\\' in item.filename
                            or PurePosixPath(item.filename).is_absolute() or '..' in PurePosixPath(item.filename).parts
                            for item in members)):
                            raise IndexerError('INVALID_FORMAT', '문서 압축 구조를 확인할 수 없습니다.', 415)
                        marker = "xl/workbook.xml" if extension == ".xlsx" else "ppt/presentation.xml" if extension == ".pptx" else "word/document.xml" if extension == '.docx' else "mimetype"
                        if marker not in names:
                            raise IndexerError("INVALID_FORMAT", "문서 형식을 확인할 수 없습니다.", 415)
                except zipfile.BadZipFile as error:
                    raise IndexerError("INVALID_FORMAT", "문서 압축 형식이 올바르지 않습니다.", 415) from error
            with operation_lock:
                if deferred:
                    result = request.app.state.ingestion.enqueue(path, profile, effective, embedding_profile_id, allow_duplicate=allow_duplicate)
                    wake.set()
                    return result
                return request.app.state.ingestion.ingest(path, profile, effective, embedding_profile_id, allow_duplicate=allow_duplicate)

    @app.get("/v1/ingestion-documents", dependencies=[Depends(auth.require)])
    def ingestion_documents(request: Request, profile_id: str | None = None):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서 연결을 설정해 주세요.", 503)
        service = request.app.state.ingestion
        try:
            selected = service.publisher_for(profile_id)
            active = selected.active() if selected else None
        except ValueError as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", str(error), 422) from error
        deleted = service.publisher.deleted_documents() if service.publisher else set()
        documents = []
        for document in service.storage.list_documents():
            jobs = service.storage.list_jobs(document["document_id"])
            pending = document["document_id"] in deleted
            if jobs or pending:
                fallback = {"job_id": "00000000-0000-0000-0000-000000000000", "status": "failed", "stage": "deleting",
                            "settings": {}, "parsed_available": False}
                documents.append({**document, "latest_job": jobs[0] if jobs else fallback,
                                  "jobs": jobs,
                                  "deletion_pending": pending,
                                  "indexed": bool(active and document["document_id"] in active["document_ids"] and document["document_id"] not in deleted)})
        return {"documents": sorted(documents, key=lambda item: item["latest_job"].get("created_at", ""), reverse=True)}

    @app.delete("/v1/ingest/{document_id}", dependencies=[Depends(auth.require)])
    def delete_ingestion_document(request: Request, document_id: UUID):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서 연결을 설정해 주세요.", 503)
        try:
            with operation_lock:
                request.app.state.ingestion.delete_document(str(document_id))
        except FileNotFoundError as error:
            raise IndexerError("DOCUMENT_NOT_FOUND", "문서를 찾을 수 없습니다.", 404) from error
        except ValueError as error:
            raise IndexerError("DELETE_UNAVAILABLE", str(error), 409) from error
        except Exception as error:
            raise IndexerError("DELETE_FAILED", "문서 삭제가 완료되지 않았습니다. 삭제를 다시 시도해 주세요.", 503) from error
        return {"deleted": True}

    @app.delete("/v1/ingest/{document_id}/jobs/{job_id}", dependencies=[Depends(auth.require)])
    def delete_ingestion_job(request: Request, document_id: UUID, job_id: UUID):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서 연결을 설정해 주세요.", 503)
        try:
            with operation_lock:
                request.app.state.ingestion.delete_job(str(document_id), str(job_id))
        except FileNotFoundError as error:
            raise IndexerError("JOB_NOT_FOUND", "작업을 찾을 수 없습니다.", 404) from error
        except ValueError as error:
            raise IndexerError("DELETE_UNAVAILABLE", str(error), 409) from error
        return {"deleted": True}

    @app.post("/v1/ingest/{document_id}/jobs", status_code=202, dependencies=[Depends(auth.require)])
    def reprocess_ingestion(request: Request, document_id: UUID, body: ReprocessRequest):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서 연결을 설정해 주세요.", 503)
        if not config.external_processing:
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 문서 처리가 비활성화되어 있습니다.", 403)
        service = request.app.state.ingestion
        try:
            with operation_lock:
                document = service.storage.load_document(str(document_id))
                base = service.storage.load_job(str(document_id), str(body.base_job_id), "processing")
                settings = settings_from(body.settings, document["format"], base.get("settings", {}))
                result = service.enqueue_reprocess(str(document_id), str(body.base_job_id), body.mode, settings,
                                                   body.embedding_profile_id)
                wake.set()
                return result
        except FileNotFoundError as error:
            raise IndexerError("JOB_NOT_FOUND", "문서 또는 기준 작업을 찾을 수 없습니다.", 404) from error
        except ValueError as error:
            raise IndexerError("REPROCESS_UNAVAILABLE", str(error), 409) from error

    @app.get("/v1/ingest/{document_id}/jobs/{job_id}", dependencies=[Depends(auth.require)])
    def ingestion_job(request: Request, document_id: UUID, job_id: UUID):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서 연결을 설정해 주세요.", 503)
        try:
            return {"document_id": str(document_id), "job_id": str(job_id),
                    **request.app.state.ingestion.storage.load_job(str(document_id), str(job_id), "processing")}
        except FileNotFoundError as error:
            raise IndexerError("JOB_NOT_FOUND", "작업을 찾을 수 없습니다.", 404) from error

    @app.post("/v1/ingest/{document_id}/jobs/{job_id}/resume", dependencies=[Depends(auth.require)])
    def resume_ingestion(request: Request, document_id: UUID, job_id: UUID):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서 연결을 설정해 주세요.", 503)
        if not config.external_processing:
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 문서 처리가 비활성화되어 있습니다.", 403)
        try:
            with operation_lock:
                service = request.app.state.ingestion
                status = service.storage.load_job(str(document_id), str(job_id), "processing")
                if status.get("can_resume"):
                    result = service.resume_processing(str(document_id), str(job_id))
                    wake.set()
                    return result
                return service.resume_index(str(document_id), str(job_id))
        except FileNotFoundError as error:
            raise IndexerError("JOB_NOT_FOUND", "작업을 찾을 수 없습니다.", 404) from error
        except ValueError as error:
            raise IndexerError("RESUME_UNAVAILABLE", str(error), 409) from error

    @app.get("/v1/ingest/{document_id}/jobs/{job_id}/content-review", dependencies=[Depends(auth.require)])
    def content_review(request: Request, document_id: UUID, job_id: UUID):
        try:
            with operation_lock:
                return request.app.state.ingestion.content_review(str(document_id), str(job_id))
        except (FileNotFoundError, ValueError, AttributeError) as error:
            raise IndexerError("REVIEW_UNAVAILABLE", "비교할 검색 내용을 확인할 수 없습니다.", 409) from error

    @app.post("/v1/ingest/{document_id}/jobs/{job_id}/accept-content-changes", dependencies=[Depends(auth.require)])
    def accept_content_changes(request: Request, document_id: UUID, job_id: UUID):
        if not config.external_processing:
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 문서 처리가 비활성화되어 있습니다.", 403)
        try:
            with operation_lock:
                return request.app.state.ingestion.accept_content_changes(str(document_id), str(job_id))
        except (FileNotFoundError, ValueError, AttributeError) as error:
            raise IndexerError("REVIEW_UNAVAILABLE", str(error), 409) from error

    @app.post("/v1/ingest/{document_id}/jobs/{job_id}/accept-partial", dependencies=[Depends(auth.require)])
    def accept_partial(request: Request, document_id: UUID, job_id: UUID):
        if not config.external_processing:
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 문서 처리가 비활성화되어 있습니다.", 403)
        try:
            with operation_lock:
                return request.app.state.ingestion.accept_content_changes(str(document_id), str(job_id), accept_partial=True)
        except (FileNotFoundError, ValueError, AttributeError) as error:
            raise IndexerError("REVIEW_UNAVAILABLE", str(error), 409) from error

    @app.post("/v1/retrieve", dependencies=[Depends(auth.require)])
    def retrieve_new(request: Request, body: IngestionSearchRequest):
        if not hasattr(request.app.state, "index_publisher"):
            raise IndexerError("INDEX_NOT_READY", "검색 색인이 설정되지 않았습니다.", 503)
        if not config.external_processing and (body.method != "bm25" or body.rerank):
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 질의 처리가 비활성화되어 있습니다.", 403)
        if body.rerank and not rerank_available(request):
            raise IndexerError("RERANK_UNAVAILABLE", "재순위 서비스 설정을 확인해 주세요.", 503)
        retrieval = selected_retrieval(request, body.profile_id, body.index_version)
        return retrieval.search(SearchRequest(
            query=body.query, method=body.method, top_k=body.limit, document_id=body.document_id,
            rerank=body.rerank, index_version=body.index_version,
            **({"profile_id": body.profile_id} if body.profile_id else {})))

    def selected_retrieval(request: Request, profile_id, version):
        service = request.app.state.ingestion
        try:
            selected = service.publisher_for(profile_id)
            if version:
                manifest = selected.get_version(str(version))
                if not manifest or manifest["embedding_profile"]["profile_id"] != selected.embedder.profile.profile_id:
                    raise ValueError("선택한 검색 버전과 임베딩 모델이 일치하지 않습니다")
            return PublishedRetrieval(selected, service.storage)
        except ValidationError as error:
            raise IndexerError("SEARCH_UNAVAILABLE", "저장된 임베딩 모델 설정을 확인해 주세요.", 409) from error
        except ValueError as error:
            raise IndexerError("SEARCH_UNAVAILABLE", str(error), 409) from error

    @app.get("/v1/embedding-profiles", dependencies=[Depends(auth.require)])
    def embedding_profiles(request: Request):
        registry = getattr(request.app.state, "embedding_profiles", None)
        if not registry:
            raise IndexerError("INDEX_NOT_READY", "임베딩 모델 연결을 설정해 주세요.", 503)
        try:
            profiles = registry.list()
            if request.headers.get("authorization", "").startswith("Bearer "):
                profiles = [{key: item[key] for key in ("profile_id", "name", "enabled", "default")} for item in profiles]
            return {"default_profile_id": registry.default_id, "profiles": profiles}
        except ValidationError as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", "저장된 임베딩 모델 설정을 확인해 주세요.", 409) from error
        except (ValueError, TypeError) as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", str(error), 409) from error

    @app.put("/v1/embedding-profiles/{profile_id}", dependencies=[Depends(auth.require_settings)])
    def save_embedding_profile(request: Request, profile_id: str, body: dict):
        registry = getattr(request.app.state, "embedding_profiles", None)
        if not registry:
            raise IndexerError("INDEX_NOT_READY", "임베딩 모델 연결을 설정해 주세요.", 503)
        if "profile_id" in body and body["profile_id"] != profile_id:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", "모델 ID가 일치하지 않습니다.", 422)
        try:
            with operation_lock:
                return registry.save({**body, "profile_id": profile_id})
        except ValidationError as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", "모델 이름, 주소와 차원을 확인해 주세요.", 422) from error
        except ValueError as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", str(error), 409) from error

    @app.delete("/v1/embedding-profiles/{profile_id}", dependencies=[Depends(auth.require_settings)])
    def delete_embedding_profile(request: Request, profile_id: str):
        registry = getattr(request.app.state, "embedding_profiles", None)
        if not registry:
            raise IndexerError("INDEX_NOT_READY", "임베딩 모델 연결을 설정해 주세요.", 503)
        try:
            with operation_lock:
                registry.delete(profile_id)
        except ValidationError as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", "저장된 임베딩 모델 설정을 확인해 주세요.", 409) from error
        except ValueError as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", str(error), 409) from error
        return {"deleted": True}

    @app.post("/v1/embedding-profiles/{profile_id}/validate", dependencies=[Depends(auth.require_settings)])
    def validate_embedding_profile(request: Request, profile_id: str):
        if not config.external_processing:
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 문서 처리가 비활성화되어 있습니다.", 403)
        registry = getattr(request.app.state, "embedding_profiles", None)
        if not registry:
            raise IndexerError("INDEX_NOT_READY", "임베딩 모델 연결을 설정해 주세요.", 503)
        try:
            publisher = registry.publisher(profile_id)
            publisher.embedder.embed(["임베딩 연결 확인 문서"], query=False)
            publisher.embedder.embed(["임베딩 연결 확인 질문"], query=True)
        except (ValueError, httpx.HTTPError) as error:
            raise IndexerError("EMBEDDING_VALIDATION_FAILED", "문서·질문 모델의 연결, 응답 모델과 차원을 확인해 주세요.", 503) from error
        return {"profile_id": profile_id, "dimensions": publisher.embedder.profile.dimensions, "validated": True}

    @app.get("/v1/ingest/{document_id}/jobs/{job_id}/elements/{element_id}", dependencies=[Depends(auth.require)])
    def ingestion_evidence(request: Request, document_id: UUID, job_id: UUID, element_id: str):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서 연결을 설정해 주세요.", 503)
        if str(document_id) in request.app.state.index_publisher.deleted_documents():
            raise IndexerError("EVIDENCE_NOT_FOUND", "근거를 찾을 수 없습니다.", 404)
        try:
            elements = request.app.state.ingestion.storage.load_job(str(document_id), str(job_id), "elements")
            element = next(item for item in elements if item.get("element_id") == element_id)
            return {"content": {"text": element["text"]}, "page": element.get("page"),
                    "coordinates": element.get("coordinates"), "kind": element.get("kind")}
        except (FileNotFoundError, StopIteration, KeyError) as error:
            raise IndexerError("EVIDENCE_NOT_FOUND", "근거를 찾을 수 없습니다.", 404) from error

    @app.get('/v1/ingest/{document_id}/jobs/{job_id}/images', dependencies=[Depends(auth.require)])
    def ingestion_images(request: Request, document_id: UUID, job_id: UUID):
        publisher = getattr(request.app.state, 'index_publisher', None)
        if publisher and str(document_id) in publisher.deleted_documents():
            raise IndexerError('EVIDENCE_NOT_FOUND', '이미지를 찾을 수 없습니다.', 404)
        try:
            storage = request.app.state.ingestion.storage
            elements = storage.load_job(str(document_id), str(job_id), 'elements')
            processing = storage.load_job(str(document_id), str(job_id), 'processing')
            checkpoint = storage.provider_checkpoint(str(document_id), str(job_id), 'initial')
            visuals = {f'image-{item["index"]}': item for item in checkpoint.get('visuals', []) if type(item.get('index')) is int}
            analyzed = {element.get('visual_id') for element in elements if element.get('source_origin') == 'visual_analysis'}
            images = []
            for element in elements:
                if element.get('source_origin') != 'native_image' or not element.get('asset_id'):
                    continue
                visual_id = element.get('visual_id')
                saved = visuals.get(visual_id, {})
                status = ('suppressed' if element.get('image_policy') in {'original', 'exclude'} and visual_id in analyzed else
                          'completed' if visual_id is not None and visual_id in analyzed else
                          'skipped' if processing.get('settings', {}).get('extraction_mode') == 'native_text' else
                          'unknown' if saved.get('status') == 'started' else 'no_text' if saved.get('status') == 'completed' else
                          'skipped' if processing.get('mode') != 'rechunk' and visuals and visual_id else 'unverified')
                images.append({'element_id': element['element_id'], 'location': element.get('office_location'),
                               'policy': element.get('image_policy'), 'has_analysis': visual_id is not None and visual_id in analyzed,
                               'is_preview': bool(element.get('image_is_preview')),
                               'layout': element.get('layout'), 'format': element.get('image_format'), 'analysis_status': status,
                               'url': f'/v1/ingest/{document_id}/jobs/{job_id}/elements/{element["element_id"]}/image'})
            pending_review = processing.get('pending_review') or (not processing.get('index_version') and
                (processing.get('can_accept_partial') or processing.get('can_accept_content_changes')))
            parser = (request.app.state.ingestion._job_parser(str(document_id), str(job_id), processing)
                      if pending_review else request.app.state.ingestion.parser)
            return {'images': images, 'analysis_available': getattr(getattr(parser, 'config', None), 'provider', '') == 'paddle_vl'}
        except (FileNotFoundError, AttributeError) as error:
            raise IndexerError('EVIDENCE_NOT_FOUND', '이미지를 찾을 수 없습니다.', 404) from error

    @app.post('/v1/ingest/{document_id}/jobs/{job_id}/images/actions', status_code=202, dependencies=[Depends(auth.require)])
    def ingestion_image_actions(request: Request, document_id: UUID, job_id: UUID, body: ImageActionsRequest):
        if not config.external_processing:
            raise IndexerError('EXTERNAL_PROCESSING_DISABLED', '외부 문서 처리가 비활성화되어 있습니다.', 403)
        try:
            with operation_lock:
                result = request.app.state.ingestion.enqueue_image_actions(str(document_id), str(job_id), body.actions,
                                                                           body.embedding_profile_id, body.descriptions)
                wake.set()
                return result
        except (FileNotFoundError, AttributeError) as error:
            raise IndexerError('JOB_NOT_FOUND', '기준 작업을 찾을 수 없습니다.', 404) from error
        except ValueError as error:
            raise IndexerError('IMAGE_ACTION_UNAVAILABLE', str(error), 409) from error

    @app.get('/v1/ingest/{document_id}/jobs/{job_id}/elements/{element_id}/image', dependencies=[Depends(auth.require)])
    def ingestion_image(request: Request, document_id: UUID, job_id: UUID, element_id: str, preview: bool = False):
        publisher = getattr(request.app.state, 'index_publisher', None)
        if publisher and str(document_id) in publisher.deleted_documents():
            raise IndexerError('EVIDENCE_NOT_FOUND', '이미지를 찾을 수 없습니다.', 404)
        try:
            storage = request.app.state.ingestion.storage
            elements = storage.load_job(str(document_id), str(job_id), 'elements')
            element = next(item for item in elements if item.get('element_id') == element_id
                           and item.get('source_origin') == 'native_image' and item.get('asset_id'))
            path = storage.image_path(str(document_id), str(job_id), element['asset_id'])
            if not path.is_file():
                raise FileNotFoundError
            with path.open('rb') as stream:
                signature = stream.read(12)
            if preview and signature.startswith((b'BM', b'II*\x00', b'MM\x00*')):
                from indexer.parsing.native_documents import image_for_analysis
                converted = image_for_analysis(path.read_bytes(), 'bmp' if signature.startswith(b'BM') else 'tiff')
                if converted:
                    return Response(content=converted[0], media_type='image/png', headers={
                        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store'})
            mime = ('image/png' if signature.startswith(b'\x89PNG\r\n\x1a\n') else
                    'image/jpeg' if signature.startswith(b'\xff\xd8\xff') else
                    'image/bmp' if signature.startswith(b'BM') else
                    'image/gif' if signature.startswith((b'GIF87a', b'GIF89a')) else
                    'image/webp' if signature.startswith(b'RIFF') and signature[8:12] == b'WEBP' else
                    'application/octet-stream')
            return FileResponse(path, media_type=mime, headers={'X-Content-Type-Options': 'nosniff',
                                'Cache-Control': 'private, no-store', 'Content-Disposition': 'attachment' if mime == 'application/octet-stream' else 'inline'})
        except (FileNotFoundError, StopIteration, KeyError, ValueError, AttributeError) as error:
            raise IndexerError('EVIDENCE_NOT_FOUND', '이미지를 찾을 수 없습니다.', 404) from error

    @app.get("/v1/ingest/{document_id}/jobs/{job_id}/source-details", dependencies=[Depends(auth.require)])
    def ingestion_source_details(request: Request, document_id: UUID, job_id: UUID):
        if not hasattr(request.app.state, "ingestion"):
            raise IndexerError("PARSER_NOT_CONFIGURED", "파서 연결을 설정해 주세요.", 503)
        publisher = getattr(request.app.state, "index_publisher", None)
        if publisher and str(document_id) in publisher.deleted_documents():
            raise IndexerError("DOCUMENT_NOT_FOUND", "문서를 찾을 수 없습니다.", 404)
        try:
            return request.app.state.ingestion.storage.load_job(str(document_id), str(job_id), "source_details")
        except FileNotFoundError as error:
            raise IndexerError("SOURCE_DETAILS_NOT_FOUND", "원본 구조 정보를 찾을 수 없습니다.", 404) from error

    @app.get("/v1/ingest-index-versions", dependencies=[Depends(auth.require)])
    def ingestion_versions(request: Request, profile_id: str | None = None):
        if not hasattr(request.app.state, "index_publisher"):
            raise IndexerError("INDEX_NOT_READY", "검색 색인이 설정되지 않았습니다.", 503)
        try:
            publisher = request.app.state.ingestion.publisher_for(profile_id)
            active = publisher.active()
            items = []
            for row in publisher.versions():
                manifest = publisher.get_version(row["index_version"])
                if manifest and manifest["embedding_profile"]["profile_id"] == publisher.embedder.profile.profile_id:
                    available = manifest["embedding_profile"] == publisher.embedding_contract()
                    items.append({**row, "status": "active" if active and active["version"] == row["index_version"] else "retired",
                                  "dense_available": available,
                                  "unavailable_reason": None if available else "임베딩 모델 계약이 달라 BM25만 사용할 수 있습니다."})
            return {"versions": items}
        except ValueError as error:
            raise IndexerError("EMBEDDING_PROFILE_INVALID", str(error), 422) from error

    @app.get("/v1/ingest-index-status", dependencies=[Depends(auth.require_settings)])
    def ingestion_index_status(request: Request):
        if not hasattr(request.app.state, "index_publisher") or not request.app.state.index_publisher:
            raise IndexerError("INDEX_NOT_READY", "검색 연결을 설정해 주세요.", 503)
        publisher = request.app.state.index_publisher
        active = publisher.active()
        current = publisher.embedding_contract()
        return {"index_version": active["version"] if active else None,
                "chunk_count": active["chunk_count"] if active else 0,
                "index_profile": active["embedding_profile"]["profile_id"] if active else None,
                "current_profile": current["profile_id"],
                "profile_matches": active["embedding_profile"] == current if active else None}

    @app.post("/v1/ingest-index-rebuild", dependencies=[Depends(auth.require_settings)])
    def rebuild_ingestion_index(request: Request, profile_id: str | None = None):
        if not hasattr(request.app.state, "index_publisher") or not request.app.state.index_publisher:
            raise IndexerError("INDEX_NOT_READY", "검색 연결을 설정해 주세요.", 503)
        if not config.external_processing:
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 문서 처리가 비활성화되어 있습니다.", 403)
        try:
            with operation_lock:
                publisher = request.app.state.ingestion.publisher_for(profile_id, for_ingest=True)
                manifest = publisher.rebuild_with_current_profile()
        except ValueError as error:
            raise IndexerError("REBUILD_UNAVAILABLE", str(error), 409) from error
        except Exception as error:
            raise IndexerError("REBUILD_FAILED", "검색 준비에 실패했습니다. 기존 검색 버전은 유지됩니다.", 503) from error
        return {"index_version": manifest["version"], "chunk_count": manifest["chunk_count"]}

    @app.post("/v1/ingest-chat", dependencies=[Depends(auth.require)])
    def ingestion_chat(request: Request, body: ChatRequest):
        if not hasattr(request.app.state, "published_chat"):
            raise IndexerError("INDEX_NOT_READY", "검색 색인이 설정되지 않았습니다.", 503)
        if not config.external_processing:
            raise IndexerError("EXTERNAL_PROCESSING_DISABLED", "외부 질의 처리가 비활성화되어 있습니다.", 403)
        if body.rerank and not rerank_available(request):
            raise IndexerError("RERANK_UNAVAILABLE", "재순위 서비스 설정을 확인해 주세요.", 503)
        retrieval = selected_retrieval(request, body.profile_id, body.index_version)
        chat = request.app.state.published_chat
        return RagChat(chat.config, retrieval, chat.client).invoke(body)

    @app.get("/v1/chat/options", dependencies=[Depends(auth.require)])
    def chat_options(request: Request):
        chat_config = request.app.state.chat.config
        return {"models": request.app.state.chat.models(), "default_model": chat_config.llm_model,
                "configured": bool(chat_config.llm_api_key and chat_config.external_processing)}

    @app.get("/v1/settings", dependencies=[Depends(auth.require_settings)])
    def get_settings(request: Request):
        with settings_lock:
            saved = effective_values(base_config)
            active = values(config) | {"llm_model": request.app.state.chat.config.llm_model,
                                       "llm_models": list(request.app.state.chat.config.llm_models)}
        return {"values": public_settings(saved), "defaults": public_settings(values(base_config)),
                "choices": choices(), "restart_required": saved != active}

    @app.put("/v1/settings", dependencies=[Depends(auth.require_settings)])
    def put_settings(body: SettingsUpdate, request: Request):
        nonlocal config, processing_defaults
        try:
            defaults = values(base_config)
            if set(body.reset) - defaults.keys():
                raise ValueError("알 수 없는 설정 항목입니다")
            changes = {**{key: defaults[key] for key in body.reset}, **body.values}
            with operation_lock, settings_lock:
                checked = ServerSettings.model_validate(effective_values(base_config) | changes).model_dump()
                if any(checked[key] != values(config)[key] for key in ("data_dir", "web_dir")):
                    raise IndexerError("PATH_CHANGE_REQUIRES_RESTART", "데이터 및 웹 경로는 실행 중 변경할 수 없습니다. 환경 설정을 변경하고 서버를 다시 시작해 주세요.", 409)
                candidate_values = dict(checked)
                for key in ("data_dir", "web_dir"):
                    candidate_values[key] = Path(candidate_values[key]).resolve()
                for key in ("llm_models", "frame_origins"):
                    candidate_values[key] = tuple(candidate_values[key])
                candidate = replace(base_config, **candidate_values)
                registry = getattr(request.app.state, "embedding_profiles", None)
                if registry and any(item["profile_id"] == candidate.embedding_profile and not item["default"]
                                    for item in registry.list()):
                    raise ValueError("서버 기본 모델 ID가 등록된 모델과 중복됩니다")
                pipeline_fields = {"parser_provider", "parser_base_url", "parser_api_key", "parser_model", "parser_mode",
                                   "parser_options", "parser_sync_timeout", "job_timeout", "remote_timeout", "max_pages",
                                   "retry_pages", "embedding_profile", "embedding_base_url", "embedding_api_key",
                                   "embedding_document_model", "embedding_query_model", "embedding_dimensions",
                                   "embedding_max_input_bytes", "embedding_batch_size", "reranker_provider",
                                   "reranker_base_url", "reranker_api_key", "reranker_model", "qdrant_url", "qdrant_key"}
                changed = any(getattr(config, key) != getattr(candidate, key) for key in pipeline_fields)
                pipeline = None
                if changed and ingestion_service is None and candidate.parser_provider:
                    pipeline = install_pipeline(request.app, candidate, recover=not hasattr(request.app.state, "ingestion"))
                saved = save_overrides(base_config, changes)
                if pipeline:
                    publish_pipeline(request.app, candidate, pipeline)
                elif changed and ingestion_service is None and not candidate.parser_provider:
                    for name in ("ingestion", "index_publisher", "embedding_profiles", "published_retrieval", "published_chat"):
                        if hasattr(request.app.state, name):
                            delattr(request.app.state, name)
                config = candidate
                auth.config = candidate
                processing_defaults = {"target_chars": candidate.default_target_chars, "overlap_chars": candidate.default_overlap_chars,
                                       "ocr_policy": candidate.default_ocr_policy, "dpi": candidate.default_dpi}
                request.app.state.chat.config = candidate
                if hasattr(request.app.state, "published_chat"):
                    request.app.state.published_chat.config = candidate
                wake.set()
        except (ValueError, ValidationError, TypeError) as error:
            raise IndexerError("INVALID_SETTINGS", "설정 값을 확인해 주세요.", 422) from error
        return {"values": public_settings(saved), "restart_required": False}

    @app.get("/v1/parser-profiles", dependencies=[Depends(auth.require_settings)])
    def list_parser_profiles():
        with parser_profiles_lock:
            return {"profiles": ParserProfiles(config.data_dir).list(config),
                    "environment": ({**{key: getattr(base_config, key) for key in PARSER_PROFILE_FIELDS if key != 'parser_api_key'},
                                     'api_key_configured': bool(base_config.parser_api_key),
                                     'active': all(getattr(base_config, key) == getattr(config, key) for key in PARSER_PROFILE_FIELDS)}
                                    if base_config.parser_provider else None),
                    "current": {**{key: getattr(config, key) for key in PARSER_PROFILE_FIELDS if key != "parser_api_key"},
                                "api_key_configured": bool(config.parser_api_key)}}

    @app.put("/v1/parser-profiles/{profile_id}", dependencies=[Depends(auth.require_settings)])
    def save_parser_profile(profile_id: str, body: dict):
        try:
            with parser_profiles_lock:
                if profile_id == 'environment':
                    raise ValueError('예약된 파서 ID입니다')
                return ParserProfiles(config.data_dir).save(profile_id, body, config)
        except (ValueError, ValidationError, TypeError) as error:
            raise IndexerError("PARSER_PROFILE_INVALID", "파서 이름과 연결 설정을 확인해 주세요.", 422) from error

    @app.post("/v1/parser-profiles/{profile_id}/activate", dependencies=[Depends(auth.require_settings)])
    def activate_parser_profile(profile_id: str, request: Request):
        nonlocal config
        with parser_profiles_lock:
            profile = ({key: getattr(base_config, key) for key in PARSER_PROFILE_FIELDS}
                       if profile_id == 'environment' and base_config.parser_provider
                       else ParserProfiles(config.data_dir).read().get(profile_id))
        if not profile:
            raise IndexerError("PARSER_PROFILE_INVALID", "파서를 찾을 수 없습니다.", 404)
        with settings_lock:
            changes = {key: profile[key] for key in PARSER_PROFILE_FIELDS}
            old_service = getattr(request.app.state, "ingestion", None)
            if old_service and old_service.parser_factory:
                for document in old_service.storage.list_documents():
                    for job in old_service.storage.list_jobs(document["document_id"]):
                        snapshot = old_service.storage.document_dir(document["document_id"]) / "jobs" / job["job_id"] / "parser_config.json"
                        if not snapshot.exists() and job.get("parser_contract") == old_service.parser_contract():
                            old_service._save_parser(document["document_id"], job["job_id"], old_service.parser)
            checked = ServerSettings.model_validate(effective_values(base_config) | changes).model_dump()
            converted = {**checked, "data_dir": Path(checked["data_dir"]), "web_dir": Path(checked["web_dir"]),
                         "llm_models": tuple(checked["llm_models"]), "frame_origins": tuple(checked["frame_origins"])}
            candidate = replace(base_config, **converted)
            pipeline = install_pipeline(request.app, candidate, recover=old_service is None) if ingestion_service is None else None
            with parser_profiles_lock:
                registry = ParserProfiles(config.data_dir)
                if config.parser_provider and not any(row['active'] for row in registry.list(config)):
                    from uuid import uuid4
                    registry.save('parser-' + uuid4().hex, {'name': config.parser_provider + ' (이전 설정)'}, config)
            saved = save_overrides(base_config, changes)
            if pipeline:
                publish_pipeline(request.app, candidate, pipeline)
            config = candidate
            auth.config = candidate
            request.app.state.chat.config = candidate
            if hasattr(request.app.state, "published_chat"):
                request.app.state.published_chat.config = candidate
            wake.set()
        return {"values": public_settings(saved), "restart_required": False}

    @app.delete("/v1/parser-profiles/{profile_id}", dependencies=[Depends(auth.require_settings)])
    def delete_parser_profile(profile_id: str):
        try:
            with parser_profiles_lock:
                ParserProfiles(config.data_dir).delete(profile_id)
        except ValueError as error:
            raise IndexerError("PARSER_PROFILE_INVALID", str(error), 404) from error
        return {"deleted": True}

    if config.web_dir.is_dir():
        assets = config.web_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/embed", include_in_schema=False)
        @app.get("/chat", include_in_schema=False)
        @app.get("/search", include_in_schema=False)
        @app.get("/settings", include_in_schema=False)
        def web():
            ancestors = " ".join(["'self'", *config.frame_origins])
            document = (config.web_dir / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(
                with_base_tag(document, base_config.public_base_path),
                headers={
                    "Content-Security-Policy": f"frame-ancestors {ancestors}",
                    "Cache-Control": "no-cache",
                },
            )

    if base_config.public_base_path:
        app.add_middleware(StripBasePath, base_path=base_config.public_base_path)
    return app
