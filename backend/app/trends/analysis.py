from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class TrendResult:
    yearly_counts: dict[int, int]
    slope: float


def compute_trend(years: list[int]) -> TrendResult:
    years = [int(year) for year in years if year is not None]
    if not years:
        return TrendResult({}, 0.0)
    first, last = min(years), max(years)
    counts = {year: years.count(year) for year in range(first, last + 1)}
    if len(counts) == 1:
        slope = 0.0
    else:
        slope = float(
            np.polyfit(
                np.asarray(list(counts), dtype=float),
                np.asarray(list(counts.values()), dtype=float),
                1,
            )[0]
        )
    return TrendResult(counts, slope)
