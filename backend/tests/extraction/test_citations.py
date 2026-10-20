"""Citation markers in statement text (attribution aid, D-S3-CITE-01).

A marker is detected reliably; whether the sentence reports the cited work's
finding or the paper's own is not. On the live corpus 7 of 113 gap statements
carry a marker and one of those 7 is the paper's own limitation that merely
cites a term, so markers are surfaced as markers -- never as a "cited claim"
label.
"""

import pytest

from app.extraction.citations import citation_markers


@pytest.mark.parametrize(
    ("text", "markers"),
    [
        (
            "Musial et al. [31] developed X, and Askari et al. [32] did Y.",
            ["[31]", "[32]"],
        ),
        ("Prior detectors [3, 4] and [5]-[7] fail.", ["[3, 4]", "[5]", "[7]"]),
        ("Ranges [12–14] are common.", ["[12–14]"]),
        (
            "This holds (Smith et al., 2020; Lee, 2019).",
            ["(Smith et al., 2020; Lee, 2019)"],
        ),
        (
            "As shown by Greshake et al. (2023), attacks transfer.",
            ["Greshake et al. (2023)"],
        ),
        ("Repeated [9] and again [9].", ["[9]"]),
    ],
)
def test_markers_are_found_verbatim(text, markers):
    assert citation_markers(text) == markers


@pytest.mark.parametrize(
    "text",
    [
        "Our detector fails on Tamil.",
        "Accuracy drops to 30.6% [FPR] in table 3.",  # not a numeric reference
        "Results (Table 2, 2024 split) are shown.",  # parenthetical, no author
        "We test on 5 languages [see appendix].",
        "",
    ],
)
def test_text_without_a_reference_marker_has_none(text):
    assert citation_markers(text) == []
