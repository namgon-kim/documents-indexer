import re
from unicodedata import normalize as unicode_normalize

# UTF-8 bytes are a local guardrail, not the provider tokenizer's token count.
MAX_INPUT_BYTES = 3800
PREPROCESSING_VERSION = "nfc-structure-v2"


def embedding_text(text: str) -> str:
    return unicode_normalize("NFC", text).strip()


def byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def context_prefix(text: str, budget: int) -> str:
    text = embedding_text(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    if budget < 7:
        return encoded[:max(0, budget)].decode("utf-8", "ignore").rstrip()
    # Units and qualifiers often appear at the end of a table caption.
    half = (budget - 5) // 2
    return (
        encoded[:half].decode("utf-8", "ignore").rstrip()
        + " … "
        + encoded[-half:].decode("utf-8", "ignore").lstrip()
    )


def split_text_ranges(text: str, *, chars: int, max_bytes: int, overlap: int = 0):
    """Yield text and offsets in the NFC-normalized input, with word-safe overlap."""
    source = embedding_text(text)
    start = 0
    while start < len(source):
        remaining = source[start:]
        limited = remaining[:chars].encode('utf-8')[:max_bytes].decode('utf-8', 'ignore')
        if not limited:
            raise ValueError('Insufficient input budget for one character')
        if len(limited) == len(remaining):
            yield remaining, start, len(source)
            return
        boundaries = list(re.finditer(r'\s+', limited))
        end = (boundaries[-1].start() if boundaries and boundaries[-1].start() >= len(limited) // 2
               else len(limited))
        # If the limit falls in a normal word, prefer its preceding boundary.
        if (end == len(limited) and end < len(remaining) and not remaining[end].isspace()
                and boundaries and boundaries[-1].start() > 0):
            end = boundaries[-1].start()
        part = remaining[:end].rstrip()
        if not part:
            raise ValueError('Insufficient input budget for non-whitespace text')
        end = len(part)
        yield part, start, start + end
        desired = max(0, end - min(overlap, end // 4))
        if desired and not remaining[desired - 1].isspace() and not remaining[desired].isspace():
            boundary = re.search(r'\s+', remaining[desired:end])
            desired = desired + boundary.end() if boundary else end
        if desired == 0 and overlap == 0:
            desired = end
        # Ensure progress even if no word boundary exists in an oversized token.
        desired = max(1, desired)
        while desired < len(remaining) and remaining[desired].isspace():
            desired += 1
        start += desired


def split_text(text: str, *, chars: int, max_bytes: int, overlap: int = 0):
    for part, _, _ in split_text_ranges(text, chars=chars, max_bytes=max_bytes, overlap=overlap):
        yield part


def render_embedding(context: str, parts: list[dict]) -> str:
    lines = [" / ".join(value for value in (part.get("label", ""), part["text"]) if value) for part in parts]
    return embedding_text("\n".join(filter(None, [context, *lines])))
