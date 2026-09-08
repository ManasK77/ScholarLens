"""Stage 7 aggregation and slope, with no database in the way (TREND-01/02).

Every expected value here is one a reader can verify by hand — that is the
point of a deterministic phase. Where a slope is asserted, the comment shows
the arithmetic rather than the number having been read off an implementation.
"""

import json
import math

import pytest

from app.trends.analysis import (
    DEFAULT_MAX_YEAR,
    DEFAULT_MIN_YEAR,
    SLOPE_DECIMALS,
    TrendSeries,
    analyze_cluster,
    trend_slope,
)


def series(mentions, *, min_year=DEFAULT_MIN_YEAR, max_year=DEFAULT_MAX_YEAR):
    return analyze_cluster(mentions, min_year=min_year, max_year=max_year)


class TestYearlyCounts:
    def test_counts_one_paper_per_year(self):
        result = series([(1, 2021), (2, 2021), (3, 2022)])

        assert result.yearly_counts == {"2021": 2, "2022": 1}

    def test_a_paper_mentioning_the_theme_twice_in_a_year_counts_once(self):
        # D-07-01: the unit is a paper, not a statement. Paper 1 contributed
        # three limitation sentences to this cluster; that is one 2023 mention.
        result = series([(1, 2023), (1, 2023), (1, 2023), (2, 2023)])

        assert result.yearly_counts == {"2023": 2}

    def test_the_same_paper_is_counted_once_in_each_year_it_appears(self):
        # A paper has exactly one publication year, so this only happens when
        # the corpus holds two records for it; each year still counts it once.
        result = series([(1, 2021), (1, 2022), (1, 2022)])

        assert result.yearly_counts == {"2021": 1, "2022": 1}

    def test_interior_years_with_no_mentions_are_filled_with_zero(self):
        # D-07-02: silence in 2021/2022 is evidence, not absence of data.
        result = series([(1, 2020), (2, 2023), (3, 2023)])

        assert result.yearly_counts == {"2020": 1, "2021": 0, "2022": 0, "2023": 2}

    def test_the_series_is_never_extrapolated_past_the_observed_range(self):
        result = series([(1, 2020), (2, 2021)])

        assert min(result.yearly_counts) == "2020"
        assert max(result.yearly_counts) == "2021"

    def test_years_are_ascending_and_keys_are_strings(self):
        result = series([(1, 2024), (2, 2020), (3, 2022)])

        assert list(result.yearly_counts) == ["2020", "2021", "2022", "2023", "2024"]
        assert all(isinstance(key, str) for key in result.yearly_counts)

    def test_counts_are_plain_ints_not_numpy_scalars(self):
        # The dict goes straight into a JSON column and a JSON response.
        result = series([(1, 2021), (2, 2022)])

        assert all(type(value) is int for value in result.yearly_counts.values())
        assert json.loads(json.dumps(result.yearly_counts)) == result.yearly_counts

    def test_input_order_does_not_change_the_result(self):
        mentions = [(3, 2022), (1, 2020), (2, 2021), (1, 2020)]

        assert (
            series(mentions).yearly_counts == series(reversed(mentions)).yearly_counts
        )


class TestUndatableMentions:
    def test_members_without_a_year_are_excluded_and_counted(self):
        # D-07-03: a paper GROBID could not date cannot be placed on the axis.
        result = series([(1, 2021), (2, None), (3, None)])

        assert result.yearly_counts == {"2021": 1}
        assert result.undated_mentions == 2

    def test_a_cluster_with_no_datable_member_yields_an_empty_series(self):
        result = series([(1, None), (2, None)])

        assert result.yearly_counts == {}
        assert result.slope == 0.0
        assert result.undated_mentions == 2

    def test_no_mentions_at_all_yields_an_empty_series(self):
        result = series([])

        assert result == TrendSeries(yearly_counts={}, slope=0.0, undated_mentions=0)

    def test_years_outside_the_sanity_window_are_dropped(self):
        # D-07-04: a `year = 12` from broken metadata would otherwise zero-fill
        # two thousand entries and flatten the slope to nothing.
        result = series([(1, 12), (2, 2024), (3, 99999)])

        assert result.yearly_counts == {"2024": 1}
        assert result.undated_mentions == 2

    def test_the_window_bounds_are_inclusive(self):
        result = series([(1, 2000), (2, 2002)], min_year=2000, max_year=2002)

        assert result.yearly_counts == {"2000": 1, "2001": 0, "2002": 1}

    def test_a_boolean_year_is_treated_as_undated(self):
        # bool is a subclass of int; True must not become the year 1.
        result = series([(1, True), (2, 2024)])

        assert result.yearly_counts == {"2024": 1}
        assert result.undated_mentions == 1

    def test_the_default_window_is_a_bounded_span(self):
        assert DEFAULT_MIN_YEAR < DEFAULT_MAX_YEAR
        assert DEFAULT_MAX_YEAR - DEFAULT_MIN_YEAR <= 500

    def test_an_inverted_window_is_rejected(self):
        with pytest.raises(ValueError, match="min_year"):
            series([(1, 2020)], min_year=2030, max_year=2020)


class TestTrendSlope:
    def test_a_steady_rise_has_a_positive_slope(self):
        # counts 1,2,3 over 2021..2023: perfectly linear, +1 paper per year.
        result = series(
            [(1, 2021), (2, 2022), (3, 2022), (4, 2023), (5, 2023), (6, 2023)]
        )

        assert result.yearly_counts == {"2021": 1, "2022": 2, "2023": 3}
        assert result.slope == 1.0

    def test_a_decline_has_a_negative_slope(self):
        result = series(
            [(1, 2021), (2, 2021), (3, 2021), (4, 2022), (5, 2022), (6, 2023)]
        )

        assert result.yearly_counts == {"2021": 3, "2022": 2, "2023": 1}
        assert result.slope == -1.0

    def test_a_single_year_has_no_slope(self):
        # One point defines no rate; the least-squares denominator is zero.
        result = series([(1, 2024), (2, 2024)])

        assert result.yearly_counts == {"2024": 2}
        assert result.slope == 0.0

    def test_a_flat_series_has_a_zero_slope(self):
        result = series([(1, 2021), (2, 2022), (3, 2023)])

        assert result.yearly_counts == {"2021": 1, "2022": 1, "2023": 1}
        assert result.slope == 0.0

    def test_zero_filled_gap_years_pull_the_slope_down(self):
        # 2020:1, 2021:0, 2022:0, 2023:2. xbar = 2021.5, ybar = 0.75.
        # numerator = (-1.5)(0.25) + (-0.5)(-0.75) + (0.5)(-0.75) + (1.5)(1.25)
        #           = -0.375 + 0.375 - 0.375 + 1.875 = 1.5
        # denominator = 2.25 + 0.25 + 0.25 + 2.25 = 5.0  ->  0.3
        result = series([(1, 2020), (2, 2023), (3, 2023)])

        assert result.slope == 0.3

    def test_a_dormant_theme_scores_below_a_continuous_one(self):
        dormant = series([(1, 2020), (2, 2024)])
        continuous = series([(1, 2023), (2, 2024), (3, 2024)])

        assert dormant.slope < continuous.slope

    def test_the_slope_is_a_signed_rate_not_a_normalized_score(self):
        # Phase 8 multiplies it by w2, so magnitude must scale with the data.
        small = series([(1, 2021), (2, 2022), (3, 2022)])
        large = series([(1, 2021)] + [(i, 2022) for i in range(2, 22)])

        assert large.slope > small.slope > 0

    def test_the_slope_is_rounded_to_a_fixed_precision(self):
        # D-07-06: 2020:1, 2021:0, 2022:2 -> numerator 1.0, denominator 2.0.
        result = series([(1, 2020), (2, 2022), (3, 2022)])

        assert result.slope == round(result.slope, SLOPE_DECIMALS)
        assert result.slope == 0.5

    def test_a_negative_zero_slope_is_normalized_to_positive_zero(self):
        # The dashboard renders `slope >= 0 ? '+' : ''` then `.toFixed(2)`;
        # -0.0 passes that test and prints "+-0.00".
        assert not math.copysign(1.0, trend_slope({"2021": 2, "2022": 2})) < 0

    def test_trend_slope_accepts_a_bare_mapping(self):
        assert trend_slope({"2020": 1, "2021": 2, "2022": 3}) == 1.0
        assert trend_slope({}) == 0.0
        assert trend_slope({"2020": 4}) == 0.0

    def test_the_slope_is_always_finite(self):
        for mapping in ({}, {"2020": 0}, {"2020": 0, "2021": 0}):
            assert math.isfinite(trend_slope(mapping))
