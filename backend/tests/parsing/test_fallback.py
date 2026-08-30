from unittest.mock import Mock

import pymupdf as fitz

from app.parsing import fallback
from app.parsing.fallback import extract_fallback_sections, split_sentences


def _pdf_bytes(*lines: tuple[str, float]) -> bytes:
    document = fitz.open()
    page = document.new_page()
    y = 72
    for text, size in lines:
        page.insert_text((72, y), text, fontsize=size)
        y += max(size * 1.8, 18)
    data = document.tobytes()
    document.close()
    return data


def test_split_sentences() -> None:
    assert split_sentences("This is one. This is two! Is this three? Yes.") == [
        "This is one.",
        "This is two!",
        "Is this three?",
        "Yes.",
    ]
    assert split_sentences("") == []
    assert split_sentences("No terminal punctuation here") == [
        "No terminal punctuation here"
    ]
    assert split_sentences("Line one.\nLine two.") == ["Line one.", "Line two."]


def test_pymupdf_path_detects_large_heading() -> None:
    pdf = _pdf_bytes(
        ("Introduction", 16),
        ("This is sentence one. This is sentence two.", 10),
        ("A final sentence.", 10),
    )
    sections = extract_fallback_sections(pdf)

    assert sections
    introduction = next(s for s in sections if s["heading"] == "Introduction")
    assert introduction["paragraphs"][0]["sentences"]


def test_uniform_font_degrades_to_one_untitled_section() -> None:
    pdf = _pdf_bytes(("All body text.", 10), ("More body text.", 10))
    sections = extract_fallback_sections(pdf)

    assert len(sections) == 1
    assert sections[0]["heading"] is None
    assert sections[0]["type"] is None
    assert "All body text" in sections[0]["paragraphs"][0]["text"]


def test_returns_none_if_both_pdf_libraries_fail(monkeypatch) -> None:
    monkeypatch.setattr(fallback.fitz, "open", Mock(side_effect=ValueError("bad")))
    monkeypatch.setattr(
        fallback.pdfplumber, "open", Mock(side_effect=ValueError("bad"))
    )
    assert extract_fallback_sections(b"not a pdf") is None


def test_pdfplumber_path_has_compatible_shape(monkeypatch) -> None:
    pdf = _pdf_bytes(("Introduction", 16), ("Body sentence.", 10))
    monkeypatch.setattr(fallback.fitz, "open", Mock(side_effect=ValueError("bad")))

    sections = extract_fallback_sections(pdf)

    assert sections
    assert set(sections[0]) == {"heading", "type", "paragraphs"}
    assert set(sections[0]["paragraphs"][0]) == {"text", "sentences"}
