"""Best-effort PDF parser used only when GROBID cannot parse a document.

Both extraction tiers return the same section/paragraph/sentence shape as
``tei_parser.extract_sections`` so the orchestration layer is parser-neutral.
"""

import io
import re
from collections import Counter

import pdfplumber
import pymupdf as fitz


def split_sentences(text: str) -> list[str]:
    """Apply a deliberately lightweight sentence split to fallback text."""
    normalized = " ".join(text.split())
    if not normalized:
        return []
    return [
        part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()
    ]


def _body_size(lines: list[tuple[str, float]]) -> float:
    sizes = [round(size, 1) for text, size in lines if text.strip()]
    return Counter(sizes).most_common(1)[0][0]


def _to_sections(lines: list[tuple[str, float]]) -> list[dict] | None:
    lines = [(" ".join(text.split()), size) for text, size in lines if text.strip()]
    if not lines:
        return None
    body_size = _body_size(lines)
    threshold = body_size * 1.15
    has_heading = any(size > threshold for _, size in lines)
    if not has_heading:
        text = " ".join(value for value, _ in lines)
        return [
            {
                "heading": None,
                "type": None,
                "paragraphs": [{"text": text, "sentences": split_sentences(text)}],
            }
        ]

    sections: list[dict] = []
    current_heading: str | None = None
    current_body: list[str] = []

    def flush() -> None:
        nonlocal current_body
        if current_heading is None and not current_body:
            return
        text = " ".join(current_body).strip()
        sections.append(
            {
                "heading": current_heading,
                "type": None,
                "paragraphs": (
                    [{"text": text, "sentences": split_sentences(text)}] if text else []
                ),
            }
        )
        current_body = []

    for text, size in lines:
        if size > threshold:
            flush()
            current_heading = text
        else:
            current_body.append(text)
    flush()
    return sections or None


def _pymupdf_lines(pdf_bytes: bytes) -> list[tuple[str, float]]:
    lines: list[tuple[str, float]] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        for page in document:
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    text = " ".join(span.get("text", "") for span in spans).strip()
                    if text:
                        size = max(float(span.get("size", 0.0)) for span in spans)
                        lines.append((text, size))
    return lines


def _pdfplumber_lines(pdf_bytes: bytes) -> list[tuple[str, float]]:
    lines: list[tuple[str, float]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as document:
        for page in document.pages:
            words = page.extract_words(extra_attrs=["size"])
            grouped: dict[float, list[dict]] = {}
            for word in words:
                grouped.setdefault(round(float(word["top"]), 1), []).append(word)
            for top in sorted(grouped):
                row = sorted(grouped[top], key=lambda word: float(word["x0"]))
                text = " ".join(str(word["text"]) for word in row).strip()
                size = max(float(word.get("size", 0.0)) for word in row)
                if text:
                    lines.append((text, size))
    return lines


def extract_fallback_sections(pdf_bytes: bytes) -> list[dict] | None:
    """Try PyMuPDF, then pdfplumber, returning ``None`` only if both fail."""
    try:
        sections = _to_sections(_pymupdf_lines(pdf_bytes))
        if sections is not None:
            return sections
    except Exception:  # noqa: BLE001 -- PDF backends expose many parse errors
        # PDF libraries raise several backend-specific exception types for
        # corrupt input. The fallback boundary intentionally contains them.
        sections = None

    try:
        return _to_sections(_pdfplumber_lines(pdf_bytes))
    except Exception:  # noqa: BLE001 -- contain all secondary parser failures
        return None
