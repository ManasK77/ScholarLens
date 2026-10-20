"""What a trend slope can and cannot claim on a small corpus (D-07-08).

The live corpus has 1-4 papers per cluster per year, and was collected on
2026-10-12, so 2026 is a partial year. The stored series and slope are not
changed; these qualifiers travel with them so no reader takes "+1.5 per year"
from nine papers as a field-wide trend.
"""

from datetime import date

from app.trends.analysis import SMALL_SAMPLE_PAPERS_PER_YEAR, trend_qualifiers

TODAY = date(2026, 10, 13)


def test_a_tiny_series_with_the_current_year_is_qualified_as_such():
    assert trend_qualifiers({"2024": 1, "2025": 4, "2026": 4}, TODAY) == {
        "papers": 9,
        "years": 3,
        "max_papers_per_year": 4,
        "small_sample": True,
        "partial_years": ["2026"],
        "single_year": False,
    }


def test_one_year_defines_no_trend():
    qualifiers = trend_qualifiers({"2025": 1}, TODAY)
    assert qualifiers["single_year"] is True
    assert qualifiers["partial_years"] == []


def test_a_well_sampled_closed_series_is_not_flagged():
    counts = {"2022": SMALL_SAMPLE_PAPERS_PER_YEAR, "2023": 9}
    qualifiers = trend_qualifiers(counts, TODAY)
    assert qualifiers["small_sample"] is False
    assert qualifiers["partial_years"] == []


def test_an_empty_series_is_qualified_not_an_error():
    assert trend_qualifiers({}, TODAY) == {
        "papers": 0,
        "years": 0,
        "max_papers_per_year": 0,
        "small_sample": True,
        "partial_years": [],
        "single_year": True,
    }
