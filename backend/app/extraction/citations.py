"""Citation markers in a statement's text (D-S3-CITE-01).

Stage 2 flattens GROBID's `<ref type="bibr">` elements into the sentence text,
so the markers survive verbatim ("[31]", "(Smith et al., 2020)"). Finding a
marker is reliable; deciding whether the sentence reports the *cited* work's
finding rather than the paper's own is not (measured: 1 of 7 marked gap
statements on the live corpus is the paper's own limitation citing a term).
So this returns markers, for a reader to judge -- never a "cited claim" label.
Derived from stored text: nothing is re-parsed or re-extracted.
"""

import re

# [12]  [3, 4]  [12-14]  [12–14]
_NUMERIC = r"\[\s*\d{1,3}(?:\s*(?:,|-|–|—)\s*\d{1,3})*\s*\]"
_NAME = r"[A-Z][A-Za-z'’\-]+"
_AUTHOR = rf"{_NAME}(?: et al\.| (?:and|&) {_NAME})?"
_YEAR = r"(?:19|20)\d{2}[a-z]?"
# (Smith et al., 2020)  (Smith and Lee, 2021; Wu, 2019)
_PARENTHETICAL = rf"\(\s*{_AUTHOR},? {_YEAR}(?:\s*;\s*{_AUTHOR},? {_YEAR})*\s*\)"
# Smith et al. (2020)
_NARRATIVE = rf"\b{_AUTHOR} \({_YEAR}\)"

_MARKER = re.compile(f"{_NUMERIC}|{_PARENTHETICAL}|{_NARRATIVE}")


def citation_markers(text: str | None) -> list[str]:
    """Every distinct citation marker in `text`, in order of appearance."""
    return list(dict.fromkeys(_MARKER.findall(text or "")))
