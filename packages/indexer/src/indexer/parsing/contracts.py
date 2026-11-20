from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ParserConfig:
    provider: str
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    mode: str = ""
    options: dict[str, str] = field(default_factory=dict)
    sync_timeout: int = 900


@dataclass(frozen=True)
class ParseRequest:
    path: Path
    format: str
    ocr: str = "auto"
    dpi: int | None = None
    page_count: int | None = None
    checkpoint: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    persist: Callable[[str, dict], None] | None = field(default=None, repr=False, compare=False)
    progress: Callable[[int, int | None], None] | None = field(default=None, repr=False, compare=False)
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class ParseResult:
    """Parser elements may include source_location: {kind: xlsx_cell, sheet, cell}
    or {kind: pptx_slide, slide}. Such locations are provider claims, not verified mappings.
    """
    provider: str
    elements: list[dict[str, Any]]
    raw: dict[str, Any]
    failed: set[int] = field(default_factory=set)
    unavailable: set[int] = field(default_factory=set)
    incomplete: bool = False
    terminal_unavailable: set[int] = field(default_factory=set)


class Parser(Protocol):
    def parse(self, request: ParseRequest) -> ParseResult: ...
