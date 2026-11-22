import os
import re
from dataclasses import dataclass
from pathlib import Path

from indexer.search_engine import DEFAULT_RERANK_TOP_N, DEFAULT_RETRIEVAL_TOP_N


def flag(key: str) -> bool:
    return os.getenv(key, "").lower() in {"1", "true", "yes"}


def qdrant_http_url(value: str) -> str:
    value = value.strip()
    if value and "://" not in value:
        return "http://" + value
    return value


def base_path(value: str) -> str:
    """Public path prefix such as /ai/labs/app; empty when the app is served at the root."""
    value = value.strip().rstrip("/")
    if value and not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", value):
        raise ValueError("PUBLIC_BASE_PATH must look like /path/to/app")
    return value


@dataclass(frozen=True)
class Config:
    data_dir: Path
    parser_provider: str = ""
    parser_base_url: str = "https://api.upstage.ai/v1"
    parser_api_key: str = ""
    parser_model: str = "document-parse-260930"
    parser_mode: str = "enhanced"
    parser_options: str = ""
    embedding_base_url: str = "https://api.upstage.ai/v1"
    embedding_api_key: str = ""
    embedding_profile: str = "solar-2-v1"
    embedding_document_model: str = "solar-embedding-2-passage"
    embedding_query_model: str = "solar-embedding-2-query"
    embedding_dimensions: int = 1024
    embedding_max_input_bytes: int = 3800
    embedding_batch_size: int = 24
    retrieval_top_n: int = DEFAULT_RETRIEVAL_TOP_N
    rerank_top_n: int = DEFAULT_RERANK_TOP_N
    chat_evidence_bytes: int = 32768
    chat_query_rewrite_enabled: bool = False
    reranker_provider: str = "disabled"
    reranker_base_url: str = "https://api.cohere.com/v2"
    reranker_api_key: str = ""
    reranker_model: str = "rerank-v3.5"
    qdrant_url: str = ""
    qdrant_key: str = ""
    external_processing: bool = False
    max_upload: int = 50 * 1024 * 1024
    max_pages: int = 2000
    retry_pages: int = 200
    parser_sync_timeout: int = 900
    remote_timeout: int = 90
    job_timeout: int = 900
    access_key: str = ""
    dev_auth: bool = False
    frame_origins: tuple[str, ...] = ()
    public_origin: str = ""
    public_base_path: str = ""
    embed_secret: str = ""
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-6.1-terra"
    llm_models: tuple[str, ...] = ()
    llm_context_tokens: int = 131072
    xlsx_structure_enabled: bool = False
    xlsx_structure_model: str = ""
    xlsx_structure_max_input_bytes: int = 64000
    xlsx_structure_max_output_tokens: int = 4096
    xlsx_structure_max_sheets: int = 3
    xlsx_structure_timeout: int = 20
    default_target_chars: int = 1000
    default_overlap_chars: int = 150
    default_ocr_policy: str = "automatic"
    default_dpi: int | None = None
    web_dir: Path = Path("apps/web/dist")

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            parser_provider=os.getenv("PARSER_PROVIDER", ""),
            parser_base_url=os.getenv("PARSER_BASE_URL", "https://api.upstage.ai/v1"),
            parser_api_key=os.getenv("PARSER_API_KEY", ""),
            parser_model=os.getenv("PARSER_MODEL", "document-parse-260930"),
            parser_mode=os.getenv("PARSER_MODE", "enhanced"),
            parser_options=os.getenv("PARSER_OPTIONS", ""),
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", "https://api.upstage.ai/v1"),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
            embedding_profile=os.getenv("EMBEDDING_PROFILE", "solar-2-v1"),
            embedding_document_model=os.getenv("EMBEDDING_DOCUMENT_MODEL", "solar-embedding-2-passage"),
            embedding_query_model=os.getenv("EMBEDDING_QUERY_MODEL", "solar-embedding-2-query"),
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "1024")),
            embedding_max_input_bytes=int(os.getenv("EMBEDDING_MAX_INPUT_BYTES", "3800")),
            embedding_batch_size=int(os.getenv("EMBEDDING_BATCH_SIZE", "24")),
            retrieval_top_n=int(os.getenv("RETRIEVAL_TOP_N", str(DEFAULT_RETRIEVAL_TOP_N))),
            rerank_top_n=int(os.getenv("RERANK_TOP_N", str(DEFAULT_RERANK_TOP_N))),
            chat_evidence_bytes=int(os.getenv("CHAT_EVIDENCE_BYTES", "32768")),
            chat_query_rewrite_enabled=flag("CHAT_QUERY_REWRITE_ENABLED"),
            reranker_provider=os.getenv("RERANKER_PROVIDER", "disabled"),
            reranker_base_url=os.getenv("RERANKER_BASE_URL", "https://api.cohere.com/v2"),
            reranker_api_key=os.getenv("RERANKER_API_KEY", ""),
            reranker_model=os.getenv("RERANKER_MODEL", "rerank-v3.5"),
            qdrant_url=qdrant_http_url(os.getenv("QDRANT_URL", "")),
            qdrant_key=os.getenv("QDRANT_API_KEY", ""),
            external_processing=flag("ALLOW_EXTERNAL_PROCESSING"),
            max_upload=int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
            max_pages=int(os.getenv("MAX_PAGES", "2000")),
            retry_pages=int(os.getenv("MAX_RETRY_PAGES", "200")),
            parser_sync_timeout=int(os.getenv("PARSER_SYNC_TIMEOUT_SECONDS", "900")),
            remote_timeout=int(os.getenv("REMOTE_TIMEOUT_SECONDS", "90")),
            job_timeout=int(os.getenv("JOB_TIMEOUT_SECONDS", "900")),
            access_key=os.getenv("INDEXER_ACCESS_KEY", ""),
            dev_auth=flag("INDEXER_DEV_AUTH"),
            frame_origins=tuple(filter(None, os.getenv("ALLOWED_FRAME_ANCESTORS", "").split())),
            public_origin=os.getenv("PUBLIC_ORIGIN", ""),
            public_base_path=base_path(os.getenv("PUBLIC_BASE_PATH", "")),
            embed_secret=os.getenv("EMBED_SIGNING_SECRET", ""),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_model=os.getenv("LLM_MODEL", "gpt-6.1-terra"),
            llm_models=tuple(dict.fromkeys(os.getenv("LLM_MODELS", "").replace(",", " ").split())),
            llm_context_tokens=int(os.getenv("LLM_CONTEXT_TOKENS", "131072")),
            default_target_chars=int(os.getenv("DEFAULT_TARGET_CHARS", "1000")),
            default_overlap_chars=int(os.getenv("DEFAULT_OVERLAP_CHARS", "150")),
            default_ocr_policy=os.getenv("DEFAULT_OCR_POLICY", "automatic"),
            default_dpi=int(os.environ["DEFAULT_DPI"]) if os.getenv("DEFAULT_DPI") else None,
            web_dir=Path(os.getenv("WEB_DIR", "apps/web/dist")).resolve(),
        )
