from pathlib import Path

from app.parsing.tei_parser import extract_backmatter, extract_sections

FIXTURE = Path(__file__).parents[1] / "fixtures" / "grobid_sample_fulltext.tei.xml"


def test_extract_sections_from_real_grobid_fixture() -> None:
    sections = extract_sections(FIXTURE.read_text(encoding="utf-8"))

    assert [section["heading"] for section in sections] == [
        "Introduction",
        "Background",
        "Model Architecture",
    ]
    assert all(section["type"] is None for section in sections)
    assert all(section["paragraphs"] for section in sections)
    assert all(paragraph["sentences"] for paragraph in sections[0]["paragraphs"])


def test_extract_backmatter_separately() -> None:
    xml = FIXTURE.read_text(encoding="utf-8")
    backmatter = extract_backmatter(xml)

    assert "Acknowledgements" in backmatter["acknowledgement"]
    assert backmatter["references"]
    assert all(
        "title" in reference and "authors" in reference
        for reference in backmatter["references"]
    )
    assert "Acknowledgements" not in [
        section["heading"] for section in extract_sections(xml)
    ]


def test_extract_sections_handles_missing_heading_and_sentence_tags() -> None:
    xml = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
    <div><p>Some text with no s tags.</p></div>
    </body></text></TEI>"""

    sections = extract_sections(xml)

    assert sections == [
        {
            "heading": None,
            "type": None,
            "paragraphs": [{"text": "Some text with no s tags.", "sentences": []}],
        }
    ]
