"""Convert GROBID TEI XML into ScholarLens's parsed-document structure."""

from xml.etree import ElementTree as ET

NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = " ".join("".join(element.itertext()).split())
    return value or None


def extract_sections(tei_xml: str) -> list[dict]:
    """Extract body sections, preserving GROBID's sentence boundaries.

    ``xml.etree.ElementTree`` does not fetch or expand external entities, which
    keeps this semi-trusted service response away from XXE-prone parser modes.
    """
    root = ET.fromstring(tei_xml)
    sections: list[dict] = []
    for div in root.findall(".//tei:text/tei:body/tei:div", NS):
        paragraphs = []
        for paragraph in div.findall("tei:p", NS):
            text = _text(paragraph) or ""
            sentences = [
                sentence
                for sentence in (_text(node) for node in paragraph.findall("tei:s", NS))
                if sentence
            ]
            paragraphs.append({"text": text, "sentences": sentences})
        sections.append(
            {
                "heading": _text(div.find("tei:head", NS)),
                "type": div.get("type"),
                "paragraphs": paragraphs,
            }
        )
    return sections


def _author_name(author: ET.Element) -> str | None:
    pers_name = author.find("tei:persName", NS)
    if pers_name is None:
        return None
    parts = [
        value
        for value in (_text(node) for node in pers_name.findall("tei:forename", NS))
        if value
    ]
    surname = _text(pers_name.find("tei:surname", NS))
    if surname:
        parts.append(surname)
    return " ".join(parts) or None


def extract_backmatter(tei_xml: str) -> dict:
    """Extract typed acknowledgement and bibliography back matter separately."""
    root = ET.fromstring(tei_xml)
    acknowledgement_div = root.find(".//tei:back/tei:div[@type='acknowledgement']", NS)
    references = []
    for entry in root.findall(
        ".//tei:back/tei:div[@type='references']//tei:biblStruct", NS
    ):
        title = _text(entry.find("tei:analytic/tei:title", NS)) or _text(
            entry.find("tei:monogr/tei:title", NS)
        )
        authors = [
            name
            for name in (
                _author_name(author) for author in entry.findall(".//tei:author", NS)
            )
            if name
        ]
        references.append({"title": title, "authors": authors})
    return {
        "acknowledgement": _text(acknowledgement_div),
        "references": references,
    }
