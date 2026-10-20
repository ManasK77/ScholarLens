"""Offline calibration harness for Mode B's sufficiency threshold."""

import pytest

from app.interactive import calibration


def _hits(*scores, papers=None):
    papers = papers or range(1, len(scores) + 1)
    return [
        {"id": str(i), "paper_id": p, "score": s, "source_type": "paragraph"}
        for i, (s, p) in enumerate(zip(scores, papers, strict=True))
    ]


def test_percentiles_are_ordered_and_ignore_non_finite_values():
    summary = calibration.percentiles([0.9, 0.1, float("nan"), 0.5, 0.7, 0.3])
    assert summary["n"] == 5
    assert summary["min"] == 0.1 and summary["max"] == 0.9
    assert (
        summary["min"]
        <= summary["p25"]
        <= summary["median"]
        <= summary["p75"]
        <= summary["max"]
    )
    assert calibration.percentiles([]) == {"n": 0}


def test_sweep_applies_mode_bs_own_rule_per_threshold():
    queries = [_hits(0.9, 0.85), _hits(0.6, 0.4), _hits(0.95, 0.95, papers=[1, 1])]
    sweep = calibration.sufficiency_sweep(queries, (0.2, 0.5, 0.8), 2, 2)
    # 0.2: q1, q2 pass (q3 is one paper) ; 0.5: q1 only ; 0.8: q1 only.
    assert sweep == {
        "0.2": round(2 / 3, 4),
        "0.5": round(1 / 3, 4),
        "0.8": round(1 / 3, 4),
    }


def test_sweep_never_counts_graph_rows():
    graph = [
        {"id": "g", "paper_id": p, "score": 0.5, "source_type": "graph"} for p in (1, 2)
    ]
    assert calibration.sufficiency_sweep([graph], (0.2,), 2, 2) == {"0.2": 0.0}


def test_separating_range_reports_a_gap_only_when_one_exists():
    assert calibration.separating_range(
        {"relevant": [0.9, 0.88], "unrelated": [0.8, 0.82]}
    ) == {
        "separable": True,
        "low": 0.82,
        "high": 0.88,
    }
    overlap = calibration.separating_range({"relevant": [0.85], "unrelated": [0.9]})
    assert overlap["separable"] is False
    assert calibration.separating_range({"relevant": [0.9]})["separable"] is False


def test_kth_best_ignores_graph_rows():
    hits = _hits(0.9, 0.7) + [
        {"id": "g", "paper_id": 3, "score": 0.95, "source_type": "graph"}
    ]
    assert calibration._kth(hits, 2) == 0.7
    assert calibration._kth(hits, 3) is None


def test_every_synthetic_query_is_labelled_with_a_known_category():
    categories = {q["category"] for q in calibration.QUERIES}
    assert categories == {"relevant", "partial", "weak", "unrelated"}
    assert all(q["text"].strip() for q in calibration.QUERIES)


def test_embed_mode_refuses_to_load_a_model_without_explicit_permission(monkeypatch):
    def forbidden():
        raise AssertionError("model load attempted")

    monkeypatch.setattr("app.embeddings.encoder.get_encoder", forbidden)
    with pytest.raises(SystemExit) as exc:
        calibration.main(["--mode", "embed"])
    assert exc.value.code == 2


def test_the_production_threshold_is_untouched():
    from app.core.config import Settings

    assert Settings.model_fields["retrieval_min_score"].default == 0.2


def test_leave_one_out_reports_fragility_not_accuracy():
    loo = calibration.leave_one_out(
        {"relevant": [0.95, 0.96, 0.945], "unrelated": [0.87, 0.88, 0.889]}
    )
    assert loo["held_out"] == 6 and loo["correct"] == 6
    # Remove the closest unrelated query and the threshold moves.
    assert loo["thresholds"]["min"] < loo["thresholds"]["max"]


def test_leave_one_out_counts_an_overlapping_hold_out_as_wrong():
    loo = calibration.leave_one_out(
        {"relevant": [0.95, 0.86], "unrelated": [0.87, 0.80]}
    )
    assert loo["correct"] < loo["held_out"]


def test_a_saved_report_is_reanalysed_without_any_model_or_store(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("model or store touched")

    monkeypatch.setattr("app.embeddings.encoder.get_encoder", forbidden)
    saved = {
        "queries": [
            {"category": "relevant", "kth_best": 0.95},
            {"category": "relevant", "kth_best": 0.946},
            {"category": "unrelated", "kth_best": 0.88},
            {"category": "weak", "kth_best": 0.92},
        ],
        "label_status": "synthetic",
    }
    report = calibration.analyze_saved(saved, (0.9,))
    assert report["accepted_by_threshold"]["unrelated"] == {"0.9": 0}
    assert report["accepted_by_threshold"]["weak"] == {"0.9": 1}
    assert report["relevant_vs_unrelated"]["separable"] is True


# --- independent-label evaluation (offline; no model, no store) -----------------


def _run_row(qid, scores, papers, ids=None):
    ids = ids or [f"{qid}-p{i}" for i in range(len(scores))]
    return {
        "question_id": qid,
        "hits": [
            {"id": i, "paper_id": p, "score": s, "source_type": "paragraph"}
            for i, s, p in zip(ids, scores, papers, strict=True)
        ],
    }


def test_cohen_kappa_measures_agreement_beyond_chance():
    a = ["relevant", "unrelated", "relevant", "weak", "unrelated", "relevant"]
    assert calibration.cohen_kappa(a, a) == 1.0
    b = ["relevant", "unrelated", "weak", "weak", "unrelated", "unrelated"]
    assert 0 < calibration.cohen_kappa(a, b) < 1
    with pytest.raises(ValueError):
        calibration.cohen_kappa(a, b[:3])


def test_the_tune_test_split_is_deterministic_and_disjoint():
    ids = [f"q{i:03d}" for i in range(100)]
    tune, test = calibration.split_tune_test(ids)
    assert (tune, test) == calibration.split_tune_test(list(reversed(ids)))
    assert not set(tune) & set(test) and set(tune) | set(test) == set(ids)
    assert 30 <= len(tune) <= 70


def test_label_metrics_count_false_accepts_rejects_retries_and_recall():
    labels = {
        "r1": {
            "label": "relevant",
            "relevant_paper_ids": {1, 2},
            "relevant_passage_ids": {"r1-p0"},
        },
        "r2": {
            "label": "relevant",
            "relevant_paper_ids": {3},
            "relevant_passage_ids": set(),
        },
        "u1": {
            "label": "unrelated",
            "relevant_paper_ids": set(),
            "relevant_passage_ids": set(),
        },
        "u2": {
            "label": "unrelated",
            "relevant_paper_ids": set(),
            "relevant_passage_ids": set(),
        },
    }
    run = {
        "r1": _run_row("r1", [0.95, 0.94], [1, 2])["hits"],
        "r2": _run_row("r2", [0.91, 0.85], [4, 5])[
            "hits"
        ],  # misses paper 3; 2nd below 0.9
        "u1": _run_row("u1", [0.92, 0.91], [6, 7])["hits"],  # false accept at 0.9
        "u2": _run_row("u2", [0.80, 0.79], [6, 7])["hits"],
    }
    m = calibration.label_metrics(
        labels, run, threshold=0.9, min_chunks=2, min_papers=2
    )
    assert m["counts"] == {"relevant": 2, "unrelated": 2}
    assert m["false_reject_rate"] == 0.5  # r2 rejected
    assert m["false_accept_rate"] == 0.5  # u1 accepted
    assert m["retry_frequency"] == 0.5  # r2 and u2 would reformulate
    assert m["paper_recall_at_k"] == 0.5  # r1 2/2, r2 0/1 -> mean 0.5
    assert m["passage_recall_at_k"] == 1.0  # only r1 has passage labels
    assert m["mean_distinct_papers_in_top_k"] == 2.0


def test_partial_and_weak_questions_are_reported_not_scored_as_errors():
    labels = {
        "p1": {
            "label": "partially_relevant",
            "relevant_paper_ids": set(),
            "relevant_passage_ids": set(),
        },
        "w1": {
            "label": "weak",
            "relevant_paper_ids": set(),
            "relevant_passage_ids": set(),
        },
    }
    run = {
        "p1": _run_row("p1", [0.95, 0.94], [1, 2])["hits"],
        "w1": _run_row("w1", [0.93, 0.92], [1, 2])["hits"],
    }
    m = calibration.label_metrics(labels, run, 0.9, 2, 2)
    assert m["false_accept_rate"] is None and m["false_reject_rate"] is None
    assert m["accepted_fraction_by_label"] == {"partially_relevant": 1.0, "weak": 1.0}


def test_the_threshold_is_chosen_on_tune_questions_only():
    labels = {
        f"r{i}": {
            "label": "relevant",
            "relevant_paper_ids": set(),
            "relevant_passage_ids": set(),
        }
        for i in range(4)
    }
    labels.update(
        {
            f"u{i}": {
                "label": "unrelated",
                "relevant_paper_ids": set(),
                "relevant_passage_ids": set(),
            }
            for i in range(4)
        }
    )
    run = {
        q: _run_row(q, [0.95, 0.95], [1, 2])["hits"]
        for q in labels
        if q.startswith("r")
    }
    run.update(
        {
            q: _run_row(q, [0.86, 0.86], [1, 2])["hits"]
            for q in labels
            if q.startswith("u")
        }
    )
    chosen = calibration.choose_threshold(
        labels, run, ["r0", "r1", "u0", "u1"], (0.8, 0.9, 0.99), 2, 2
    )
    assert chosen == 0.9
    report = calibration.evaluate_labels(
        labels, run, (0.8, 0.9, 0.99), 2, 2, current=0.2, min_per_label=25
    )
    assert report["chosen_on"] == "tune" and report["reported_on"] == "test"
    assert report["sample_sufficient"] is False  # 4 per label < 25
    assert report["current_threshold"]["threshold"] == 0.2


def test_the_cli_scores_saved_runs_against_labels_offline(
    tmp_path, monkeypatch, capsys
):
    import json

    def forbidden(*_a, **_k):
        raise AssertionError("model or store touched")

    monkeypatch.setattr("app.embeddings.encoder.get_encoder", forbidden)
    monkeypatch.setattr("qdrant_client.QdrantClient", forbidden)
    labels = tmp_path / "adjudicated.csv"
    labels.write_text(
        "question_id,final_label,relevant_paper_ids,relevant_passage_ids,adjudicator_id,agreement\n"
        "a,relevant,1;2,,x,agree\n"
        "b,unrelated,,,x,agree\n"
        "c,,,,,\n"
    )
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "queries": [
                    _run_row("a", [0.95, 0.94], [1, 2]),
                    _run_row("b", [0.86, 0.85], [3, 4]),
                ],
                "current_settings": {
                    "retrieval_min_score": 0.2,
                    "retrieval_min_chunks": 2,
                    "retrieval_min_papers": 2,
                },
            }
        )
    )
    assert calibration.main(["--evaluate-labels", str(labels), "--run", str(run)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (
        report["label_totals"]["relevant"] == 1 and report["sample_sufficient"] is False
    )


def test_unknown_labels_are_rejected(tmp_path):
    labels = tmp_path / "adjudicated.csv"
    labels.write_text(
        "question_id,final_label,relevant_paper_ids,relevant_passage_ids\nq,maybe,,\n"
    )
    with pytest.raises(ValueError, match="unknown label"):
        calibration.load_adjudicated(str(labels))


def test_independent_questions_need_unique_ids(tmp_path):
    path = tmp_path / "q.csv"
    path.write_text("question_id,question,author_id\nq1,What?,a\nq1,Why?,b\n")
    with pytest.raises(ValueError, match="unique"):
        calibration.load_questions(str(path))


# --- run provenance (Phase 13: every run records what produced it) ----------------


class _ScrollClient:
    def __init__(self, versions):
        self.pages = [
            [type("P", (), {"payload": {"embed_version": v}})() for v in versions]
        ]

    def scroll(self, collection, limit, offset, with_payload, with_vectors):
        return (self.pages.pop(0) if self.pages else []), None


def _settings():
    from types import SimpleNamespace

    return SimpleNamespace(
        qdrant_collection="c",
        embedding_model="allenai/specter2_base",
        embedding_adapter="allenai/specter2",
    )


def test_run_provenance_records_encoder_revisions_and_input_hash(tmp_path, monkeypatch):
    questions = tmp_path / "questions.csv"
    questions.write_text("question_id,question\nq1,What?\n", encoding="utf-8")
    monkeypatch.setattr(calibration, "_resolved", lambda repo, rev: f"rev-of-{repo}")
    monkeypatch.setattr(calibration, "_query_embed_version", lambda s: "v1")
    settings = _settings()
    record = calibration.run_provenance(
        _ScrollClient(["v1", "v1"]), settings, str(questions)
    )
    assert record["embedding_model_revision"] == f"rev-of-{settings.embedding_model}"
    assert record["embedding_adapter_revision"] == "rev-of-allenai/specter2"
    assert len(record["questions_sha256"]) == 64
    assert record["stored_embed_versions"] == ["v1"]
    assert record["query_matches_stored_vectors"] is True
    assert record["generated_at"].endswith("+00:00")


def test_run_provenance_flags_query_vectors_that_do_not_match_the_index(monkeypatch):
    monkeypatch.setattr(calibration, "_resolved", lambda repo, rev: None)
    monkeypatch.setattr(calibration, "_query_embed_version", lambda s: "v2")
    record = calibration.run_provenance(_ScrollClient(["v1"]), _settings(), None)
    assert record["query_matches_stored_vectors"] is False
    assert record["questions_sha256"] is None


def test_an_external_question_file_is_not_labelled_independent(monkeypatch):
    # The tool cannot know who wrote a question file; it must not claim it.
    from types import SimpleNamespace

    from app.embeddings import encoder

    class Encoder:
        def encode(self, texts, batch):
            return [[1.0, 0.0] for _ in texts]

    class Client:
        def query_points(self, collection, query, limit, with_payload):
            point = SimpleNamespace(
                id="a", payload={"paper_id": 1, "source_type": "paragraph"}, score=0.9
            )
            return SimpleNamespace(points=[point])

    monkeypatch.setattr(encoder, "get_encoder", lambda: Encoder())
    settings = SimpleNamespace(
        qdrant_collection="c",
        retrieval_top_n=1,
        retrieval_min_chunks=1,
        retrieval_min_papers=1,
    )
    report = calibration.run_embed(
        Client(), settings, [{"question_id": "x1", "text": "What?"}]
    )
    assert "independent" not in report["label_status"].split(";")[0]
    assert report["queries"][0]["synthetic"] is False
