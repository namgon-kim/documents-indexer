from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from indexer.search_engine import MAX_RESULT_LIMIT


class IndexerError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


class ProcessingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    extraction_mode: Literal["parser", "native_text", "native_visual"] = "parser"
    ocr_policy: Literal["automatic", "force"] = "automatic"
    dpi: int | None = Field(default=None, ge=50, le=600)
    target_chars: int = Field(default=1000, ge=400, le=4000)
    overlap_chars: int = Field(default=150, ge=0, le=400)

    @model_validator(mode="before")
    @classmethod
    def default_overlap(cls, values):
        if isinstance(values, dict) and "overlap_chars" not in values:
            target = values.get("target_chars", 1000)
            if type(target) is int:
                return {**values, "overlap_chars": min(150, target // 4)}
        return values

    @model_validator(mode="after")
    def check_overlap(self):
        if self.overlap_chars > self.target_chars // 4:
            raise ValueError("겹침 길이는 청크 길이의 25% 이하여야 합니다.")
        return self


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=2000)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    top_k: int | None = Field(default=None, ge=1, le=MAX_RESULT_LIMIT, strict=True)
    document_id: UUID | None = None
    index_version: UUID | None = None
    method: Literal["bm25", "dense", "hybrid"] = "hybrid"
    rerank: bool = False


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)
    citation_numbers: list[int] = Field(default_factory=list, max_length=MAX_RESULT_LIMIT + 5)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=50)
    document_id: UUID | None = None
    index_version: UUID | None = None
    top_k: int | None = Field(default=None, ge=1, le=MAX_RESULT_LIMIT, strict=True)
    xlsx_full_table: bool = False
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    method: Literal["bm25", "dense", "hybrid"] = "hybrid"
    rerank: bool = False
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)


@dataclass
class ParseResult:
    elements: list[dict]
    covered: set[int] = field(default_factory=set)
    failed: set[int] = field(default_factory=set)
    total: int | None = None
    incomplete: bool = False
    unavailable: set[int] = field(default_factory=set)
