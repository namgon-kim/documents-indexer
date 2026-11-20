"""문서·질의 모델과 연결을 하나의 임베딩 공간으로 관리한다."""

import hashlib
import json
import re
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from indexer.artifact_store import atomic_json
from indexer.dense_index import EmbeddingClient, EmbeddingProfile
from indexer.index_publication import IndexPublisher


class ProfileDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    base_url: str
    api_key: str = Field(default="", max_length=4096, repr=False)
    document_model: str = Field(min_length=1, max_length=128)
    query_model: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(ge=1, le=65536)
    max_input_bytes: int = Field(default=3800, ge=256, le=1000000)
    batch_size: int = Field(default=24, ge=1, le=512)
    enabled: bool = True

    @field_validator("profile_id")
    @classmethod
    def check_id(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise ValueError("프로필 ID는 영문, 숫자, 하이픈, 밑줄, 점을 사용해 주세요")
        return value

    @field_validator("name", "document_model", "query_model")
    @classmethod
    def check_text(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError("이름과 모델을 확인해 주세요")
        return value

    @field_validator("base_url")
    @classmethod
    def check_url(cls, value):
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as error:
            raise ValueError("임베딩 주소를 확인해 주세요") from error
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("임베딩 주소는 HTTP(S) 주소여야 합니다")
        return value.rstrip("/")


class EmbeddingProfiles:
    def __init__(self, data_dir: Path, default_publisher: IndexPublisher, client: httpx.Client):
        self.path = data_dir.resolve() / "embedding-profiles.json"
        self.default = default_publisher
        self.client = client
        self.lock = default_publisher.lock

    @property
    def default_id(self):
        return self.default.embedder.profile.profile_id

    def _read(self):
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("저장된 임베딩 모델 목록을 확인할 수 없습니다")  # noqa: TRY004
        try:
            profiles = [ProfileDefinition.model_validate(item) for item in payload]
        except ValidationError as error:
            raise ValueError("저장된 임베딩 모델 설정을 확인해 주세요") from error
        if len({item.profile_id for item in profiles}) != len(profiles):
            raise ValueError("저장된 임베딩 모델 ID가 중복되었습니다")
        if any(item.profile_id == self.default_id for item in profiles):
            raise ValueError("서버 기본 모델 ID가 등록된 모델과 중복됩니다")
        return {item.profile_id: item for item in profiles}

    def _used(self, profile_id):
        for path in self.default.root.glob("*/manifest.json"):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get("embedding_profile", {}).get("profile_id") == profile_id:
                return True
        return False

    def _pending(self, profile_id):
        for path in (self.path.parent / "ingestion" / "documents").glob("*/jobs/*/processing.json"):
            status = json.loads(path.read_text(encoding="utf-8"))
            if status.get("embedding_profile_id") == profile_id and status.get("status") in {"queued", "processing"}:
                return True
        return False

    def list(self):
        with self.lock:
            default = self.default.embedder
            rows = [{"profile_id": self.default_id, "name": default.profile.document_model, "base_url": default.base_url,
                     "document_model": default.profile.document_model, "query_model": default.profile.query_model,
                     "dimensions": default.profile.dimensions, "max_input_bytes": default.profile.max_input_bytes, "batch_size": default.profile.batch_size, "enabled": True, "default": True,
                     "api_key_configured": bool(default.api_key), "has_index": self._used(self.default_id)}]
            for profile in self._read().values():
                rows.append({**profile.model_dump(exclude={"api_key"}), "default": False,
                             "api_key_configured": bool(profile.api_key), "has_index": self._used(profile.profile_id)})
            return rows

    def save(self, supplied):
        with self.lock:
            data = dict(supplied)
            profile_id = data.get("profile_id")
            profiles = self._read()
            if profile_id == self.default_id:
                raise ValueError("기본 모델은 서버 설정에서 변경해 주세요")
            old = profiles.get(profile_id)
            if old and "api_key" not in data:
                data["api_key"] = old.api_key
            profile = ProfileDefinition.model_validate(data)
            identity = ("base_url", "document_model", "query_model", "dimensions")
            if old and (self._used(profile_id) or self._pending(profile_id)) and any(getattr(old, key) != getattr(profile, key) for key in identity):
                raise ValueError("검색 내용이 있는 모델의 연결·모델·차원은 바꿀 수 없습니다. 새 ID로 등록해 주세요")
            if old and self._pending(profile_id) and any(getattr(old, key) != getattr(profile, key) for key in ("max_input_bytes", "batch_size")):
                raise ValueError("이 모델을 사용하는 작업이 진행 중입니다")
            if old and not profile.enabled and self._pending(profile_id):
                raise ValueError("이 모델을 사용하는 작업이 진행 중입니다")
            profiles[profile_id] = profile
            atomic_json(self.path, [value.model_dump() for value in profiles.values()])
            return next(row for row in self.list() if row["profile_id"] == profile_id)

    def delete(self, profile_id):
        with self.lock:
            profiles = self._read()
            if profile_id not in profiles:
                raise ValueError("모델을 찾을 수 없습니다")
            if self._used(profile_id) or self._pending(profile_id):
                raise ValueError("검색 버전에서 사용 중인 모델입니다. 사용 중지할 수 있습니다")
            del profiles[profile_id]
            atomic_json(self.path, [value.model_dump() for value in profiles.values()])

    def publisher(self, profile_id=None, *, for_ingest=False):
        with self.lock:
            if not profile_id or profile_id == self.default_id:
                return self.default
            profile = self._read().get(profile_id)
            if not profile:
                raise ValueError("선택한 임베딩 모델을 찾을 수 없습니다")
            if for_ingest and not profile.enabled:
                raise ValueError("사용 중지된 임베딩 모델입니다")
            embedder = EmbeddingClient(self.client, profile.base_url, profile.api_key,
                EmbeddingProfile(profile.profile_id, profile.document_model, profile.query_model, profile.dimensions, profile.max_input_bytes, profile.batch_size))
            active = "active-" + hashlib.sha256(profile.profile_id.encode()).hexdigest()[:24] + ".json"
            return IndexPublisher(self.default.root.parent, self.default.qdrant, embedder,
                                  self.default.reranker_factory, active_name=active, lock=self.lock,
                                  search_lock=self.default.search_lock)
