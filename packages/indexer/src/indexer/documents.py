"""문서 형식과 처리 프로필에 대한 공통 정책."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DocumentProfile = Literal["general", "report", "manual", "spreadsheet", "slides", "scanned"]
SUPPORTED_FORMATS = {"pdf", "hwp", "hwpx", "xlsx", "pptx", "docx"}
CHUNK_DEFAULTS = {
    "general": {"target_chars": 1000, "overlap_chars": 150},
    "report": {"target_chars": 1200, "overlap_chars": 200},
    "manual": {"target_chars": 800, "overlap_chars": 100},
    "scanned": {"target_chars": 1200, "overlap_chars": 200},
    "spreadsheet": {"target_chars": 1500, "overlap_chars": 0},
    "slides": {"target_chars": 1000, "overlap_chars": 100},
}


@dataclass(frozen=True)
class ProfileSelection:
    profile: DocumentProfile
    origin: Literal["user", "format", "default"]
    reason: str


def select_profile(path: Path, requested: DocumentProfile | None = None) -> ProfileSelection:
    kind = path.suffix.lower().lstrip(".")
    if kind not in SUPPORTED_FORMATS:
        raise ValueError("지원하지 않는 문서 형식입니다")
    if requested is not None:
        return ProfileSelection(requested, "user", "사용자 지정")
    if kind == "xlsx":
        return ProfileSelection("spreadsheet", "format", "스프레드시트 파일")
    if kind == "pptx":
        return ProfileSelection("slides", "format", "슬라이드 파일")
    return ProfileSelection("general", "default", "범용 기본값")
