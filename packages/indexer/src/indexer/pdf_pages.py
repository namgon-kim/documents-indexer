"""Recognize explicit physical PDF page references, never printed page labels."""

import re

PAGE_REFERENCE = re.compile(
    r'(?<!\w)PDF\s*(?:(?:문서|파일)(?:의)?\s*|의\s*)?'
    r'(?:(\d{1,5})\s*(?:쪽|페이지)|(?:p(?:age)?\.?|페이지)\s*(\d{1,5}))', re.IGNORECASE)
CONTINUATION = re.compile(r'\s*(?:,|와|과|및|and)\s*(?:PDF\s*)?(\d{1,5})\s*(?:쪽|페이지)', re.IGNORECASE)


def physical_pdf_pages(question):
    pages = []
    for match in PAGE_REFERENCE.finditer(question):
        # References explicitly described as printed pages cannot safely be
        # translated into physical page numbers.
        prefix = question[max(0, match.start() - 12):match.start()]
        if re.search(r'(?:인쇄|본문|printed)\s*(?:된|의)?\s*$', prefix, re.IGNORECASE):
            continue
        number = int(match.group(1) or match.group(2))
        if number not in pages:
            pages.append(number)
        cursor = match.end()
        while continuation := CONTINUATION.match(question, cursor):
            number = int(continuation.group(1))
            if number not in pages:
                pages.append(number)
            cursor = continuation.end()
    return pages
