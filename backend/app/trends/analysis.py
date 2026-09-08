"""Stage 7 aggregation and trend slope — Pandas + NumPy, nothing else.

This module is deliberately pure: mentions in, a `TrendSeries` out. It touches
no database, no vector store, no model and no clock, which is what makes the
project's "the same corpus produces the same number" claim testable rather than
aspirational. `service.py` owns the persistence side.

The two rules that shape everything here (see the phase plan for the full
reasoning):

* **A mention is a paper, not a sentence** (D-07-01). One paper that contributed
  four limitation statements to a cluster mentioned that limitation once.
* **Interior silence is evidence** (D-07-02). A year inside the observed range
  with no mentions is a real zero, and the series is never extrapolated outside
  that range.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Sanity window on publication years (D-07-04). Broken metadata reaches this
# phase as `year = 12` or `year = 99999`; with zero-filling, one such value
# turns a five-year series into a several-thousand-entry dict and flattens the
# slope toward nothing. Both bounds are constants rather than "this year",
# because a slope whose value moved with the wall clock would not be
# reproducible. `Settings.trend_min_year` / `trend_max_year` override them.
DEFAULT_MIN_YEAR = 1900
DEFAULT_MAX_YEAR = 2100

# Least squares on float64 leaves a bottom-bit tail that differs between BLAS
# builds. Six decimals is far below any weight Phase 8 could give the slope and
# well above the two decimals the dashboard renders (D-07-06).
SLOPE_DECIMALS = 6


@dataclass(frozen=True, slots=True)
class TrendSeries:
    """One cluster's mention history and the rate derived from it.

    `yearly_counts` maps a **year string** to a distinct-paper count, ascending,
    with interior gaps filled. String keys are not incidental: the dict is
    stored in a JSON column and served as-is, and `frontend/src/types.ts`
    declares it `Record<string, number>`.

    `slope` is signed mentions per year. `undated_mentions` counts the members
    that carried no usable year and so appear nowhere in the series.
    """

    yearly_counts: dict[str, int] = field(default_factory=dict)
    slope: float = 0.0
    undated_mentions: int = 0


def _datable_year(year: object, min_year: int, max_year: int) -> int | None:
    """The member's year if it can sit on a time axis, otherwise `None`.

    `bool` is excluded explicitly: it is a subclass of `int`, and a stray `True`
    must not become the year 1.
    """
    if isinstance(year, bool) or not isinstance(year, int):
        return None
    return year if min_year <= year <= max_year else None


def trend_slope(yearly_counts: Mapping[str, int]) -> float:
    """Ordinary least squares of count against year (D-07-05).

    `slope = sum((x - mean x)(y - mean y)) / sum((x - mean x)^2)`, in mentions
    per year. Written out rather than delegated to `np.polyfit`, which emits a
    `RankWarning` and returns unstable coefficients on exactly the degenerate
    inputs this phase meets routinely.

    Every degenerate case resolves to a defined `0.0`, never `NaN`: an empty
    series, a single year (the denominator is zero — one point defines no rate),
    and a flat series (the numerator is zero).
    """
    if len(yearly_counts) < 2:
        return 0.0

    years = np.array([int(year) for year in yearly_counts], dtype=np.float64)
    counts = np.array(list(yearly_counts.values()), dtype=np.float64)

    centered_years = years - years.mean()
    denominator = float(centered_years @ centered_years)
    if denominator == 0.0:
        return 0.0

    slope = float(centered_years @ (counts - counts.mean())) / denominator
    if not math.isfinite(slope):
        return 0.0
    slope = round(slope, SLOPE_DECIMALS)
    # `-0.0` renders as "+-0.00" in the dashboard, which prefixes "+" whenever
    # `slope >= 0` and then calls `.toFixed(2)`.
    return 0.0 if slope == 0.0 else slope


def build_yearly_counts(
    mentions: Iterable[tuple[int, object]], *, min_year: int, max_year: int
) -> tuple[dict[str, int], int]:
    """Distinct papers per year, zero-filled across the observed range.

    Returns the series and the number of mentions that carried no usable year.
    """
    if min_year > max_year:
        raise ValueError(f"min_year {min_year} is above max_year {max_year}")

    rows = []
    undated = 0
    for paper_id, year in mentions:
        datable = _datable_year(year, min_year, max_year)
        if datable is None:
            undated += 1
        else:
            rows.append((paper_id, datable))

    if not rows:
        return {}, undated

    frame = pd.DataFrame(rows, columns=["paper_id", "year"])
    # D-07-01: one paper mentioning the theme repeatedly in a year is one
    # mention. `groupby` then sorts by year, so the result is already ascending.
    counts = frame.drop_duplicates().groupby("year").size()
    # D-07-02: fill the gaps *inside* the observed range only — extending past
    # it would assert something about years the corpus never spoke about.
    filled = counts.reindex(
        range(int(counts.index.min()), int(counts.index.max()) + 1), fill_value=0
    )
    # `int(...)` on both sides: NumPy scalars are not JSON-serializable.
    return {str(int(year)): int(count) for year, count in filled.items()}, undated


def analyze_cluster(
    mentions: Iterable[tuple[int, object]],
    *,
    min_year: int = DEFAULT_MIN_YEAR,
    max_year: int = DEFAULT_MAX_YEAR,
) -> TrendSeries:
    """One cluster's `(paper_id, year)` mentions in, its trend out.

    A cluster with nothing datable still gets a `TrendSeries` — an empty series
    and a `0.0` slope — so that Phase 8 never has to tell "no trend" apart from
    "not computed" (D-07-07).
    """
    yearly_counts, undated = build_yearly_counts(
        mentions, min_year=min_year, max_year=max_year
    )
    return TrendSeries(
        yearly_counts=yearly_counts,
        slope=trend_slope(yearly_counts),
        undated_mentions=undated,
    )
