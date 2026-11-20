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


def split_text(text: str, *, chars: int, max_bytes: int, overlap: int = 0):
    remaining = embedding_text(text)
    while remaining:
        limited = remaining[:chars].encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
        if not limited:
            raise ValueError("Insufficient input budget for one character")
        if len(limited) == len(remaining):
            yield remaining
            return
        # Prefer a boundary between words. A period can belong to a decimal.
        boundaries = list(re.finditer(r"\s+", limited))
        end = (
            boundaries[-1].start()
            if boundaries and boundaries[-1].start() >= len(limited) // 2
            else len(limited)
        )
        yield remaining[:end].rstrip()
        remaining = remaining[max(1, end - min(overlap, end // 4)) :].lstrip()


def render_embedding(context: str, parts: list[dict]) -> str:
    lines = [" / ".join(value for value in (part.get("label", ""), part["text"]) if value) for part in parts]
    return embedding_text("\n".join(filter(None, [context, *lines])))
