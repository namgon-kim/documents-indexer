import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

import olefile
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import DictionaryObject

from indexer.contracts import IndexerError


@dataclass
class Inspection:
    pages: int | None = None
    blank: set[int] = field(default_factory=set)
    scan_candidates: set[int] = field(default_factory=set)


def is_page_label(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*[-–—(\[]?\s*(?:page\s+)?\d{1,6}(?:\s*(?:/|of)\s*\d{1,6})?\s*(?:쪽|페이지)?\s*[-–—)\]]?\s*",
            text,
            flags=re.IGNORECASE,
        )
    )


def has_page_body_text(page) -> bool:
    resources = page.get("/Resources")
    resources = resources.get_object() if resources is not None else None
    objects = resources.get("/XObject") if isinstance(resources, DictionaryObject) else None
    objects = objects.get_object() if objects is not None else None
    has_image = isinstance(objects, DictionaryObject) and any(
        isinstance(item, DictionaryObject) and item.get("/Subtype") == "/Image"
        for item in (reference.get_object() for reference in objects.values())
    )
    if not has_image or page.rotation % 180:
        text = (page.extract_text() or "").strip()
        return bool(text and not is_page_label(text))

    bottom, top = float(page.cropbox.bottom), float(page.cropbox.top)
    margin = (top - bottom) * 0.12
    positions = []

    def locate(fragment, user_matrix, text_matrix, *_):
        if fragment.strip():
            positions.append(
                text_matrix[4] * user_matrix[1] + text_matrix[5] * user_matrix[3] + user_matrix[5]
            )

    text = (page.extract_text(visitor_text=locate) or "").strip()
    if not text or is_page_label(text):
        return False
    return not positions or any(bottom + margin <= y <= top - margin for y in positions)


def inspect(path: Path, kind: str, max_pages: int) -> Inspection:
    try:
        if kind == "pdf":
            return inspect_pdf(path, max_pages)
        if kind == "hwp":
            with olefile.OleFileIO(str(path)) as document:
                header = document.openstream("FileHeader").read(40)
                if not header.startswith(b"HWP Document File") or len(header) < 40:
                    raise ValueError("invalid HWP header")
                flags = int.from_bytes(header[36:40], "little")
                if flags & 0b10:
                    raise IndexerError("ENCRYPTED_FILE", "암호가 설정된 문서입니다.")
        elif kind == "hwpx":
            inspect_hwpx(path)
        else:
            raise ValueError("unknown format")
    except (BadZipFile, PdfReadError, ValueError, KeyError, IndexError, OSError) as exc:
        raise IndexerError("INVALID_DOCUMENT", "파일 형식이 맞지 않거나 파일이 손상되었습니다.", 415) from exc
    return Inspection()


def inspect_pdf(path: Path, max_pages: int) -> Inspection:
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("invalid PDF header")
        stream.seek(0)
        pdf = PdfReader(stream)
        if pdf.is_encrypted:
            raise IndexerError("ENCRYPTED_FILE", "암호가 설정된 문서입니다.")
        count = len(pdf.pages)
        if not 0 < count <= max_pages:
            raise IndexerError("PAGE_LIMIT", "허용된 PDF 페이지 범위를 벗어났습니다.", 413)
        output = Inspection(pages=count)
        for number, page in enumerate(pdf.pages, 1):
            try:
                content = page.get_contents()
                if (content is None or not content.get_data().strip()) and not page.get("/Annots"):
                    output.blank.add(number)
                elif not has_page_body_text(page):
                    output.scan_candidates.add(number)
            except (PdfReadError, ValueError, KeyError, TypeError):
                # Upstage can still attempt extraction for pages pypdf cannot inspect.
                continue
        return output


def inspect_hwpx(path: Path):
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or sum(item.file_size for item in entries) > 200 * 1024 * 1024:
            raise IndexerError("ARCHIVE_LIMIT", "HWPX 내부 파일 크기 또는 개수 제한을 넘었습니다.", 413)
        names = {item.filename for item in entries}
        if len(names) != len(entries) or "Contents/content.hpf" not in names:
            raise ValueError("invalid HWPX package")
        if not any(re.fullmatch(r"Contents/section\d+\.xml", name) for name in names):
            raise ValueError("missing HWPX document sections")
        for item in entries:
            parts = PurePosixPath(item.filename)
            if parts.is_absolute() or ".." in parts.parts or "\\" in item.filename:
                raise ValueError("invalid archive path")
            if item.flag_bits & 1:
                raise IndexerError("ENCRYPTED_FILE", "암호가 설정된 문서입니다.")


def copy_pages(source: Path, target: Path, page_numbers: list[int]):
    with source.open("rb") as stream, target.open("wb") as output:
        reader = PdfReader(stream)
        writer = PdfWriter()
        for number in page_numbers:
            writer.add_page(reader.pages[number - 1])
        writer.write(output)
