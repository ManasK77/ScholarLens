from app.core.reconcile import compare_projections


def test_reconciliation_detects_missing_orphaned_and_stale_payloads():
    expected = {
        "a": {"paper_id": 1, "fact_id": 3, "graph_node_id": "n1", "text": "current"}
    }
    actual = {
        "a": {"paper_id": 1, "fact_id": 30, "graph_node_id": "old", "text": "old"},
        "extra": {"paper_id": 99},
    }
    report = compare_projections(expected, actual, {1, 2}, {1: "n1", 99: "n99"})
    assert report["extra_points"] == ["extra"]
    assert report["stale_points"] == ["a"]
    assert report["missing_graph_papers"] == [2]
    assert report["orphan_graph_papers"] == [99]
    assert report["consistent"] is False
    assert "current" not in str(report)


def test_reconciliation_is_read_only_and_clean_when_projections_match():
    points = {"a": {"paper_id": 1, "fact_id": None}}
    assert compare_projections(points, points, {1}, {1: "n1"})["consistent"]
    assert compare_projections(points, {}, {1}, {1: "n1"})["missing_points"] == ["a"]
