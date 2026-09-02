from app.trends.analysis import compute_trend


def test_compute_trend_fills_missing_years_and_returns_positive_slope():
    trend = compute_trend([2020, 2022, 2022, 2023])

    assert trend.yearly_counts == {2020: 1, 2021: 0, 2022: 2, 2023: 1}
    assert trend.slope > 0
