import csv
import io
import os
import re

import httpx

from indexer.parsing.contracts import ParserConfig

KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
RESERVED = {"model", "mode", "document", "ocr", "dpi", "api_key", "authorization", "base_url"}


def parse_options(value: str) -> dict[str, str]:
    """세미콜론 분리, 큰따옴표로 값 전체를 감싸며 중복 키는 거부한다."""
    options = {}
    if not value:
        return options
    try:
        items = next(csv.reader(io.StringIO(value), delimiter=";", strict=True))
    except (csv.Error, StopIteration) as error:
        raise ValueError("파서 추가 옵션 형식이 올바르지 않습니다") from error
    for item in items:
        key, separator, option = item.partition("=")
        key = key.strip()
        if not separator or not KEY.fullmatch(key) or not option.strip():
            raise ValueError("파서 추가 옵션은 key=value 형식이어야 합니다")
        if key.lower() in RESERVED or key in options:
            raise ValueError(f"파서 추가 옵션의 공통 필드 또는 중복 키: {key}")
        options[key] = option.strip()
    return options


def from_env() -> ParserConfig:
    base_url = os.getenv("PARSER_BASE_URL", "https://api.upstage.ai/v1").strip()
    try:
        url = httpx.URL(base_url)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("invalid URL")
    except (httpx.InvalidURL, ValueError) as error:
        raise ValueError("PARSER_BASE_URL을 확인해 주세요") from error
    return ParserConfig(provider=os.getenv("PARSER_PROVIDER", "upstage"), base_url=base_url.rstrip("/"),
                        api_key=os.getenv("PARSER_API_KEY", ""), model=os.getenv("PARSER_MODEL", "document-parse-260930"),
                        mode=os.getenv("PARSER_MODE", "enhanced"), options=parse_options(os.getenv("PARSER_OPTIONS", "")),
                        sync_timeout=int(os.getenv("PARSER_SYNC_TIMEOUT_SECONDS", "900")))
