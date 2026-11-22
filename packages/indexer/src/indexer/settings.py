"""Persist operator overrides separately from environment defaults."""

import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import get_args

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from indexer.config import Config, qdrant_http_url
from indexer.contracts import ProcessingSettings
from indexer.parsing.registry import provider_names
from indexer.parsing.settings import parse_options
from indexer.search_engine import MAX_RESULT_LIMIT, MAX_RETRIEVAL_TOP_N

RERANKER_PROVIDERS = ("disabled", "cohere")


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    data_dir: str = Field(min_length=1, max_length=1024)
    web_dir: str = Field(min_length=1, max_length=1024)
    parser_provider: str = Field(max_length=128)
    parser_base_url: str
    parser_api_key: str = Field(max_length=4096)
    parser_model: str = Field(min_length=1, max_length=128)
    parser_mode: str = Field(min_length=1, max_length=128)
    parser_options: str = Field(max_length=8192)
    embedding_base_url: str
    embedding_api_key: str = Field(max_length=4096)
    embedding_profile: str = Field(min_length=1, max_length=64)
    embedding_document_model: str = Field(min_length=1, max_length=128)
    embedding_query_model: str = Field(min_length=1, max_length=128)
    embedding_dimensions: int = Field(ge=1, le=65536)
    embedding_max_input_bytes: int = Field(ge=256, le=1000000)
    embedding_batch_size: int = Field(ge=1, le=512)
    retrieval_top_n: int = Field(ge=1, le=MAX_RETRIEVAL_TOP_N)
    rerank_top_n: int = Field(ge=1, le=MAX_RESULT_LIMIT)
    chat_evidence_bytes: int = Field(ge=4096, le=262144)
    reranker_provider: str
    reranker_base_url: str
    reranker_api_key: str = Field(max_length=4096)
    reranker_model: str = Field(min_length=1, max_length=128)
    llm_api_key: str = Field(max_length=4096)
    llm_base_url: str
    llm_model: str = Field(min_length=1, max_length=128)
    llm_models: list[str] = Field(max_length=100)
    xlsx_structure_enabled: bool
    xlsx_structure_model: str = Field(max_length=128)
    xlsx_structure_max_input_bytes: int = Field(ge=1024, le=256000)
    xlsx_structure_max_output_tokens: int = Field(ge=256, le=16000)
    xlsx_structure_max_sheets: int = Field(ge=1, le=20)
    xlsx_structure_timeout: int = Field(ge=1, le=90)
    qdrant_url: str
    qdrant_key: str = Field(max_length=4096)
    external_processing: bool
    max_upload: int = Field(ge=1, le=2**40)
    max_pages: int = Field(ge=1, le=100000)
    retry_pages: int = Field(ge=0, le=100000)
    parser_sync_timeout: int = Field(ge=1, le=86400)
    remote_timeout: int = Field(ge=1, le=86400)
    job_timeout: int = Field(ge=1, le=604800)
    access_key: str = Field(max_length=4096)
    dev_auth: bool
    frame_origins: list[str] = Field(max_length=100)
    public_origin: str
    embed_secret: str = Field(max_length=4096)
    default_target_chars: int = Field(ge=400, le=4000)
    default_overlap_chars: int = Field(ge=0, le=400)
    default_ocr_policy: str
    default_dpi: int | None = Field(ge=50, le=600)

    @model_validator(mode="after")
    def validate_processing_defaults(self):
        ProcessingSettings(target_chars=self.default_target_chars, overlap_chars=self.default_overlap_chars,
                           ocr_policy=self.default_ocr_policy, dpi=self.default_dpi)
        return self

    @field_validator("parser_base_url", "embedding_base_url", "reranker_base_url", "llm_base_url", "qdrant_url", "public_origin")
    @classmethod
    def validate_url(cls, value, info):
        if info.field_name == "qdrant_url":
            value = qdrant_http_url(value)
        if not value and info.field_name in {"qdrant_url", "public_origin"}:
            return value
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as error:
            raise ValueError("주소 형식을 확인해 주세요") from error
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("HTTP(S) 주소를 입력해 주세요")
        if info.field_name == "public_origin" and url.path not in {"", "/"}:
            raise ValueError("공개 출처에 경로를 포함할 수 없습니다")
        return value.rstrip("/")

    @field_validator("parser_provider")
    @classmethod
    def validate_parser_provider(cls, value):
        if value and value not in provider_names():
            raise ValueError("등록된 파서 제공자를 선택해 주세요")
        return value

    @field_validator("rerank_top_n")
    @classmethod
    def validate_rerank_top_n(cls, value, info):
        if "retrieval_top_n" in info.data and value > info.data["retrieval_top_n"]:
            raise ValueError("리랭킹 결과 수는 검색 후보 수 이하여야 합니다.")
        return value

    @field_validator("reranker_provider")
    @classmethod
    def validate_reranker_provider(cls, value):
        if value not in RERANKER_PROVIDERS:
            raise ValueError("지원하는 재순위 제공자를 선택해 주세요")
        return value

    @field_validator("embedding_profile")
    @classmethod
    def validate_embedding_profile(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise ValueError("임베딩 모델 ID를 확인해 주세요")
        return value

    @field_validator("parser_options")
    @classmethod
    def validate_parser_options(cls, value):
        parse_options(value)
        return value

    @field_validator("frame_origins")
    @classmethod
    def validate_origins(cls, values):
        for value in values:
            try:
                url = httpx.URL(value)
            except httpx.InvalidURL as error:
                raise ValueError("iframe 출처 형식을 확인해 주세요") from error
            if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment or url.path not in {"", "/"} or value.endswith("/"):
                raise ValueError("iframe 출처는 경로가 없는 HTTPS 출처여야 합니다")
        return list(dict.fromkeys(values))

    @field_validator("llm_models")
    @classmethod
    def validate_models(cls, values):
        if any(not value.strip() or len(value) > 128 or value != value.strip() for value in values):
            raise ValueError("모델 ID를 확인해 주세요")
        if len(values) != len(set(values)):
            raise ValueError("모델 ID가 중복됩니다")
        return values


SECRET_FIELDS = frozenset({"parser_api_key", "embedding_api_key", "reranker_api_key", "llm_api_key", "qdrant_key", "access_key", "embed_secret"})


def values(config):
    return ServerSettings.model_validate({key: str(getattr(config, key).resolve()) if key in {"data_dir", "web_dir"}
        else list(getattr(config, key)) if key in {"llm_models", "frame_origins"} else getattr(config, key)
        for key in ServerSettings.model_fields}).model_dump()


def settings_file(base: Config):
    return base.data_dir / "server-settings.json"


def read_overrides(base: Config):
    path = settings_file(base)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("Invalid saved server settings")
    retired = {"upstage_key", "upstage_parse_url", "upstage_embedding_url", "parse_model"}
    if set(data) - ServerSettings.model_fields.keys() - retired:
        raise ValueError("Invalid saved server settings")
    return {key: value for key, value in data.items() if key not in retired}


def effective_values(base: Config):
    return ServerSettings.model_validate(values(base) | read_overrides(base)).model_dump()


def apply_overrides(base: Config):
    data = effective_values(base)
    for key in ("data_dir", "web_dir"):
        data[key] = Path(data[key]).resolve()
    for key in ("llm_models", "frame_origins"):
        data[key] = tuple(data[key])
    return replace(base, **data)


def save_overrides(base: Config, supplied):
    current = effective_values(base)
    if set(supplied) - ServerSettings.model_fields.keys():
        raise ValueError("알 수 없는 설정 항목입니다")
    merged = current | supplied
    checked = ServerSettings.model_validate(merged).model_dump()
    defaults = values(base)
    overrides = {key: value for key, value in checked.items() if value != defaults[key]}
    path = settings_file(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".server-settings-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(overrides, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return checked


def choices():
    """Allowed values for settings the UI offers as a fixed list."""
    return {"parser_provider": ["", *sorted(provider_names())], "reranker_provider": list(RERANKER_PROVIDERS),
            "default_ocr_policy": list(get_args(ProcessingSettings.model_fields["ocr_policy"].annotation))}


def public_settings(settings):
    return {key: ({"configured": bool(value)} if key in SECRET_FIELDS else value)
            for key, value in settings.items()}
