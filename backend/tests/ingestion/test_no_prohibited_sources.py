"""Permanent regression test for INGEST-09 — IEEE Xplore, Springer, and ACM
Digital Library must never appear anywhere in the ingestion codebase.
"""

import re
from pathlib import Path

PROHIBITED_PATTERN = re.compile(r"ieee|springer|acm\.org", re.IGNORECASE)


def test_no_prohibited_platform_references() -> None:
    ingestion_dir = Path(__file__).parent.parent.parent / "app" / "ingestion"

    for py_file in ingestion_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert not PROHIBITED_PATTERN.search(
            text
        ), f"{py_file} references a prohibited platform (IEEE/Springer/ACM)"


def test_only_permitted_source_modules_exist() -> None:
    sources_dir = Path(__file__).parent.parent.parent / "app" / "ingestion" / "sources"

    actual = {p.name for p in sources_dir.glob("*.py")}
    expected = {"__init__.py", "arxiv.py", "semantic_scholar.py", "acl_anthology.py"}

    assert actual == expected
