"""Phase 13 Mode B evaluation: dataset validation, metrics, splits, reproduction."""

import copy
import csv
import json

import pytest
from evaluation import mode_b

PASSAGES = [
    ("pa1", 1, "limitation", "MIPIAD covers English and Bangla only."),
    ("pa2", 1, "paragraph", "Hybrid F1 is 0.9205."),
    ("pb1", 2, "paragraph", "PromptArmor does not fully eliminate inflation."),
    ("pc1", 3, "paragraph", "Unrelated passage about scaling."),
]


def _write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _catalogue(tmp_path):
    passages = tmp_path / "passages.csv"
    papers = tmp_path / "papers.csv"
    _write_csv(passages, ["passage_id", "paper_id", "source_type", "text"], PASSAGES)
    _write_csv(
        papers,
        ["paper_id", "title", "year", "has_full_text"],
        [
            (1, "MIPIAD", 2026, True),
            (2, "Relevance judges", 2026, True),
            (3, "Scaling", 2024, True),
        ],
    )
    return mode_b.load_catalogue(passages, papers)


QUESTIONS = [
    ("q1", "Which languages?"),
    ("q2", "Does PromptArmor work?"),
    ("q3", "Protein folding?"),
]
LABELS = [
    ("q1", "relevant", "factual", 1, "1", "pa1", ""),
    ("q2", "partially_relevant", "factual", 1, "2", "pb1", ""),
    ("q3", "unrelated", "off_topic", 1, "", "", ""),
]
CLAIMS = [
    (
        "c1",
        "supported",
        "verbatim",
        "pa1",
        "MIPIAD covers English and Bangla only.",
        "",
    ),
    (
        "c2",
        "unsupported",
        "language_swapped",
        "pa1",
        "MIPIAD covers English and Hindi only.",
        "",
    ),
]
LABEL_HEADER = [
    "question_id", "label", "question_type", "papers_needed",
    "relevant_paper_ids", "relevant_passage_ids", "rationale",
]  # fmt: skip


def _dataset(tmp_path, labels=LABELS, questions=QUESTIONS, claims=CLAIMS):
    directory = tmp_path / "dataset"
    directory.mkdir(exist_ok=True)
    _write_csv(directory / "questions.csv", ["question_id", "question"], questions)
    _write_csv(directory / "labels.csv", LABEL_HEADER, labels)
    _write_csv(
        directory / "claims.csv",
        ["claim_id", "label", "phenomenon", "cited_passage_ids", "claim", "note"],
        claims,
    )
    return directory


def _hits(*rows):
    return [
        {"id": i, "paper_id": p, "score": s, "source_type": "paragraph"}
        for i, p, s in rows
    ]


# --- dataset validation: fail loudly, never drop a row ----------------------------


def test_a_valid_dataset_loads_with_hashes(tmp_path):
    data = mode_b.load_dataset(_dataset(tmp_path), _catalogue(tmp_path))
    assert set(data["labels"]) == {"q1", "q2", "q3"}
    assert data["labels"]["q1"]["relevant_passage_ids"] == ["pa1"]
    assert set(data["hashes"]) == {"questions.csv", "labels.csv", "claims.csv"}
    assert all(len(h) == 64 for h in data["hashes"].values())


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ([*LABELS[:2], ("q3", "maybe", "off_topic", 1, "", "", "")], "unknown label"),
        (
            [*LABELS[:2], ("q3", "unrelated", "off_topic", 1, "", "pa1", "")],
            "must not list",
        ),
        (
            [("q1", "relevant", "factual", 1, "1", "zz9", ""), *LABELS[1:]],
            "unknown passage",
        ),
        ([("q1", "relevant", "factual", 1, "2", "pa1", ""), *LABELS[1:]], "paper 1"),
        (
            [("q1", "relevant", "factual", 1, "1", "", ""), *LABELS[1:]],
            "needs at least one",
        ),
        (LABELS[:2], "no label"),
        ([*LABELS, ("q1", "relevant", "factual", 1, "1", "pa1", "")], "duplicate"),
        (
            [("q1", "relevant", "made_up", 1, "1", "pa1", ""), *LABELS[1:]],
            "question_type",
        ),
    ],
)
def test_an_invalid_dataset_fails_with_every_problem_named(tmp_path, labels, message):
    with pytest.raises(mode_b.DatasetError, match=message):
        mode_b.load_dataset(_dataset(tmp_path, labels=labels), _catalogue(tmp_path))


def test_an_unknown_claim_passage_fails(tmp_path):
    claims = [("c1", "supported", "verbatim", "nope", "x y z", "")]
    with pytest.raises(mode_b.DatasetError, match="unknown passage"):
        mode_b.load_dataset(_dataset(tmp_path, claims=claims), _catalogue(tmp_path))


# --- metric formulas ----------------------------------------------------------------


def test_wilson_interval_matches_the_textbook_value():
    low, high = mode_b.wilson(8, 10)
    assert (round(low, 4), round(high, 4)) == (0.4902, 0.9433)
    assert mode_b.wilson(0, 0) is None


def test_auroc_counts_ties_as_half_and_handles_missing_scores():
    assert mode_b.auroc([0.9, 0.8], [0.7, 0.6]) == 1.0
    assert mode_b.auroc([0.5], [0.5]) == 0.5
    # A question that never passes the gate has no score: below every real one.
    assert mode_b.auroc([0.6, None], [0.5]) == 0.5
    assert mode_b.auroc([], [0.5]) is None


def test_gate_score_is_the_highest_threshold_mode_b_would_accept():
    hits = _hits(("a", 1, 0.95), ("b", 1, 0.94), ("c", 2, 0.91), ("d", 3, 0.80))
    assert mode_b.gate_score(hits, 2, 2) == pytest.approx(0.91)
    assert mode_b.gate_score(hits, 2, 1) == pytest.approx(0.94)
    assert mode_b.gate_score(_hits(("a", 1, 0.9), ("b", 1, 0.8)), 2, 2) is None


def test_gate_metrics_report_counts_rates_and_denominators():
    labels = {
        "r1": {"label": "relevant"}, "r2": {"label": "relevant"},
        "p1": {"label": "partially_relevant"},
        "w1": {"label": "weak"}, "u1": {"label": "unrelated"},
    }  # fmt: skip
    strong = _hits(("a", 1, 0.95), ("b", 2, 0.94))
    weak = _hits(("a", 1, 0.85), ("b", 2, 0.84))
    run = {"r1": strong, "r2": weak, "p1": strong, "w1": strong, "u1": weak}
    m = mode_b.gate_metrics(list(labels), labels, run, 0.9, 2, 2)
    assert m["confusion"]["relevant"] == {"accept": 1, "reject": 1}
    assert m["answerable_vs_not"]["tp"] == 2 and m["answerable_vs_not"]["fn"] == 1
    assert m["answerable_vs_not"]["fp"] == 1 and m["answerable_vs_not"]["tn"] == 1
    assert m["answerable_vs_not"]["precision"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["false_accept"]["weak"] == {
        "k": 1,
        "n": 1,
        "rate": 1.0,
        "ci95": mode_b.wilson(1, 1),
    }
    assert (
        m["false_reject"]["relevant"]["k"] == 1
        and m["false_reject"]["relevant"]["n"] == 2
    )
    assert (
        m["relevant_vs_unrelated"]["tp"] == 1 and m["relevant_vs_unrelated"]["tn"] == 1
    )


def test_an_undefined_rate_is_none_never_zero():
    labels = {"r1": {"label": "relevant"}}
    m = mode_b.gate_metrics(
        ["r1"], labels, {"r1": _hits(("a", 1, 0.95), ("b", 2, 0.94))}, 0.9, 2, 2
    )
    assert m["false_accept"]["unrelated"] == {
        "k": 0,
        "n": 0,
        "rate": None,
        "ci95": None,
    }
    assert m["answerable_vs_not"]["specificity"] is None


def test_ranking_metrics_use_the_expected_passages():
    labels = {
        "q1": {
            "label": "relevant",
            "relevant_passage_ids": ["pa2", "pa1"],
            "relevant_paper_ids": [1],
        }
    }
    run = {"q1": _hits(("x", 3, 0.9), ("pa1", 1, 0.8))}
    m = mode_b.ranking_metrics(["q1"], labels, run)
    assert m["passage_recall_at_k"]["mean"] == 0.5
    assert m["mrr"]["mean"] == 0.5
    assert m["paper_recall_at_k"]["mean"] == 1.0
    assert m["top1_paper_expected"]["k"] == 0


# --- threshold choice: calibration split only ---------------------------------------


def _scored_labels(n=40):
    labels, run = {}, {}
    for i in range(n):
        qid = f"q{i:03d}"
        relevant = i % 2 == 0
        labels[qid] = {"label": "relevant" if relevant else "unrelated"}
        top = 0.95 if relevant else 0.87
        run[qid] = _hits(("a", 1, top), ("b", 2, top - 0.001))
    return labels, run


def test_the_threshold_is_chosen_on_the_calibration_split_only():
    labels, run = _scored_labels()
    calibration, held_out = mode_b.split(list(labels))
    chosen = mode_b.choose_threshold(calibration, labels, run, 2, 2)
    # Flip every held-out label: the chosen threshold must not move.
    flipped = copy.deepcopy(labels)
    for qid in held_out:
        flipped[qid]["label"] = (
            "unrelated" if flipped[qid]["label"] == "relevant" else "relevant"
        )
    assert mode_b.choose_threshold(calibration, flipped, run, 2, 2) == chosen
    assert 0.869 < chosen["threshold"] <= 0.949


def test_the_split_is_fixed_by_question_id_alone():
    ids = [f"p{i:03d}" for i in range(1, 50)]
    assert mode_b.split(ids) == mode_b.split(list(reversed(ids)))
    calibration, held_out = mode_b.split(ids)
    assert (
        not set(calibration) & set(held_out) and len(calibration) + len(held_out) == 49
    )


# --- the whole run: missing data, determinism, reproduction --------------------------


def _run_doc(dataset_dir, hits_by_id, drop=()):
    import hashlib

    questions = dict(QUESTIONS)
    return {
        "mode": "embed",
        "queries": [
            {"question_id": q, "text": questions[q], "hits": hits_by_id[q]}
            for q in questions
            if q not in drop
        ],
        "current_settings": {
            "retrieval_min_score": 0.2,
            "retrieval_min_chunks": 2,
            "retrieval_min_papers": 2,
            "retrieval_top_n": 12,
        },
        "provenance": {
            "questions_sha256": hashlib.sha256(
                (dataset_dir / "questions.csv").read_bytes()
            ).hexdigest(),
            "query_matches_stored_vectors": True,
        },
    }


HITS = {
    "q1": _hits(("pa1", 1, 0.95), ("pb1", 2, 0.94)),
    "q2": _hits(("pb1", 2, 0.93), ("pa2", 1, 0.92)),
    "q3": _hits(("pc1", 3, 0.86), ("pa2", 1, 0.85)),
}


def _evaluate(tmp_path, run_doc, **kw):
    dataset = _dataset(tmp_path)
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(run_doc), encoding="utf-8")
    return mode_b.evaluate(
        dataset_dir=dataset,
        passages=tmp_path / "passages.csv",
        papers=tmp_path / "papers.csv",
        retrieval_run=run_path,
        output_dir=tmp_path / "out",
        run_id="test-run",
        **kw,
    )


def test_a_labelled_question_missing_from_the_run_is_reported_not_scored_as_a_miss(
    tmp_path,
):
    _catalogue(tmp_path)
    doc = _run_doc(_dataset(tmp_path), HITS, drop=("q2",))
    result = _evaluate(tmp_path, doc)
    assert result["retrieval"]["coverage"]["missing_from_run"] == ["q2"]
    counted = result["retrieval"]["current_threshold"]["all"]["confusion"]
    assert (
        "partially_relevant" not in counted
        or sum(counted["partially_relevant"].values()) == 0
    )
    assert any("q2" in w for w in result["warnings"])


def test_a_stale_run_for_other_questions_is_refused(tmp_path):
    _catalogue(tmp_path)
    doc = _run_doc(_dataset(tmp_path), HITS)
    doc["provenance"]["questions_sha256"] = "0" * 64
    with pytest.raises(mode_b.DatasetError, match="questions.csv"):
        _evaluate(tmp_path, doc)


def test_query_vectors_from_another_encoder_are_refused(tmp_path):
    _catalogue(tmp_path)
    doc = _run_doc(_dataset(tmp_path), HITS)
    doc["provenance"]["query_matches_stored_vectors"] = False
    with pytest.raises(mode_b.DatasetError, match="encoder"):
        _evaluate(tmp_path, doc)


def test_identical_inputs_give_an_identical_results_digest(tmp_path):
    _catalogue(tmp_path)
    doc = _run_doc(_dataset(tmp_path), HITS)
    first = _evaluate(tmp_path, doc)
    second = _evaluate(tmp_path, doc)
    assert first["manifest"]["results_sha256"] == second["manifest"]["results_sha256"]
    written = json.loads(
        (tmp_path / "out" / "manifest.json").read_text(encoding="utf-8")
    )
    assert written["label_provenance"]["independent"] is False
    assert written["mode"] == {
        "offline": True,
        "mocked": False,
        "live_calls": 0,
        "llm_judge": False,
    }


def test_reproduce_detects_a_changed_label_file(tmp_path):
    _catalogue(tmp_path)
    doc = _run_doc(_dataset(tmp_path), HITS)
    _evaluate(tmp_path, doc)
    manifest = tmp_path / "out" / "manifest.json"
    assert mode_b.reproduce(manifest)["reproduced"] is True
    labels = tmp_path / "dataset" / "labels.csv"
    labels.write_text(
        labels.read_text(encoding="utf-8").replace("partially_relevant", "weak"),
        encoding="utf-8",
    )
    outcome = mode_b.reproduce(manifest)
    assert outcome["reproduced"] is False
    assert "labels.csv" in " ".join(outcome["input_mismatches"])


def test_claim_check_metrics_count_vetoes_by_label(tmp_path):
    _catalogue(tmp_path)
    result = _evaluate(tmp_path, _run_doc(_dataset(tmp_path), HITS))
    claims = result["claims"]
    assert (
        claims["supported"]["vetoed"]["k"] == 0
        and claims["supported"]["vetoed"]["n"] == 1
    )
    assert claims["unsupported"]["vetoed"]["k"] == 1
    assert claims["by_phenomenon"]["language_swapped"]["vetoed"] == 1
    assert claims["note"].startswith("The deterministic checks can only reject")


# --- scope-aware sufficiency, supplementary claims, aspect completeness -------------


def _scope_inputs(tmp_path):
    """q1 asks about one study and every hit is from paper 1; q2 asks across
    papers with every hit from paper 1; q3 is off-topic with hits from 2 papers."""
    questions = [
        ("q1", "Which languages does the MIPIAD study cover?"),
        ("q2", "Which papers report English-only evaluation?"),
        ("q3", "Protein folding?"),
    ]
    labels = [
        ("q1", "relevant", "factual", 1, "1", "pa1", ""),
        ("q2", "partially_relevant", "synthesis", 2, "1", "pa1", ""),
        ("q3", "unrelated", "off_topic", 1, "", "", ""),
    ]
    _catalogue(tmp_path)
    dataset = mode_b.load_dataset(
        _dataset(tmp_path, labels=labels, questions=questions), _catalogue(tmp_path)
    )
    hits = {
        "q1": _hits(("pa1", 1, 0.95), ("pa2", 1, 0.94)),
        "q2": _hits(("pa1", 1, 0.93), ("pa2", 1, 0.92)),
        "q3": _hits(("pc1", 3, 0.86), ("pa2", 1, 0.85)),
    }
    return dataset, {"hits": hits}, _catalogue(tmp_path)


def test_the_scope_policy_is_compared_with_the_two_paper_rule(tmp_path):
    dataset, run, catalogue = _scope_inputs(tmp_path)
    settings = {"retrieval_min_score": 0.2, "retrieval_min_chunks": 2,
                "retrieval_min_papers": 2}  # fmt: skip
    policy = mode_b.evaluate_scope_policy(dataset, run, catalogue, settings, [0.2])
    at = policy["thresholds"]["0.2"]
    # Only the single-paper question changes: rejected before, accepted now.
    assert at["changed"] == [
        {"question_id": "q1", "label": "relevant", "scope": "single_paper",
         "two_paper_rule": False, "scope_policy": True}
    ]  # fmt: skip
    assert at["all"]["two_paper_rule"]["relevant"] == {"accepted": 0, "n": 1}
    assert at["all"]["scope_policy"]["relevant"] == {"accepted": 1, "n": 1}
    assert at["all"]["scope_policy"]["partially_relevant"] == {"accepted": 0, "n": 1}
    assert policy["scope_by_papers_needed"] == {
        "1": {"single_paper": 1, "unspecified": 1},
        "2": {"synthesis": 1},
    }
    assert "provisional" in policy["note"]


def test_supplementary_claims_are_scored_separately_with_their_provenance(tmp_path):
    _catalogue(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    _write_csv(
        extra / "claims.csv",
        ["claim_id", "label", "phenomenon", "cited_passage_ids", "claim", "note"],
        [("s1", "unsupported", "absence_inferred", "pa1",
          "No study has evaluated MIPIAD on Hindi.", "")],
    )  # fmt: skip
    (extra / "dataset.json").write_text(
        json.dumps({"source": "agent-authored", "independent": False}), encoding="utf-8"
    )
    result = _evaluate(
        tmp_path, _run_doc(_dataset(tmp_path), HITS), claims_supplement=extra
    )
    supplement = result["claims_supplement"]
    assert supplement["unsupported"]["vetoed"] == {"k": 1, "n": 1, "rate": 1.0,
        "ci95": supplement["unsupported"]["vetoed"]["ci95"]}  # fmt: skip
    assert supplement["provenance"]["independent"] is False
    assert result["claims"]["unsupported"]["vetoed"]["n"] == 1  # main set untouched
    assert "claims_supplement/claims.csv" in result["manifest"]["inputs"]["sha256"]


def test_aspect_completeness_counts_aspects_a_returned_claim_cites(tmp_path):
    live = tmp_path / "LV-X.json"
    live.write_text(
        json.dumps(
            {
                "test_id": "LV-X",
                "outcome": "grounded_answer",
                "response": {
                    "claims": [{"text": "A.", "evidence_ids": ["pa1"]}],
                    "removed_claim_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    aspects = tmp_path / "aspects.json"
    aspects.write_text(
        json.dumps(
            {
                "LV-X": [
                    {"id": "languages", "expected_passage_ids": ["pa1"]},
                    {"id": "limitations", "expected_passage_ids": ["pb1"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    result = mode_b.evaluate_completeness([live], aspects)
    assert result["LV-X"] == {
        "outcome": "grounded_answer",
        "aspects": 2,
        "covered": ["languages"],
        "uncovered": ["limitations"],
        "removed_claim_count": 1,
    }


def test_the_answer_relevance_rule_is_measured_on_the_expected_passages(tmp_path):
    questions = [
        ("q1", "Which languages does MIPIAD cover?"),
        ("q2", "How does MIPIAD perform on Hindi?"),
        ("q3", "Protein folding?"),
    ]
    labels = [
        ("q1", "relevant", "factual", 1, "1", "pa1", ""),
        ("q2", "partially_relevant", "factual", 1, "2", "pb1", ""),
        ("q3", "unrelated", "off_topic", 1, "", "", ""),
    ]
    catalogue = _catalogue(tmp_path)
    dataset = mode_b.load_dataset(
        _dataset(tmp_path, labels=labels, questions=questions), catalogue
    )
    rule = mode_b.evaluate_relevance_rule(dataset, catalogue)
    # q1's expected passage names MIPIAD; q2's names neither MIPIAD nor Hindi.
    assert rule["answerable_with_subjects"] == {"n": 2, "would_refuse": ["q2"]}
    assert rule["unanswerable_with_subjects"] == {"n": 0, "of": 1}


def test_the_scope_policy_uses_the_configured_paper_specific_minimum(tmp_path):
    dataset, run, catalogue = _scope_inputs(tmp_path)
    settings = {"retrieval_min_score": 0.2, "retrieval_min_chunks": 2,
                "retrieval_min_papers": 2, "retrieval_min_papers_paper_specific": 2}  # fmt: skip
    policy = mode_b.evaluate_scope_policy(dataset, run, catalogue, settings, [0.2])
    assert policy["thresholds"]["0.2"]["changed"] == []
    assert policy["min_papers_paper_specific"] == 2


def test_no_threshold_is_chosen_when_no_calibration_question_is_ever_accepted():
    labels = {
        "a": {"label": "relevant"},
        "b": {"label": "unrelated"},
    }
    # Every hit from one paper: the two-paper rule never accepts either.
    run = {q: _hits(("x1", 1, 0.9), ("x2", 1, 0.8)) for q in labels}
    chosen = mode_b.choose_threshold(["a", "b"], labels, run, 2, 2)
    assert chosen["threshold"] is None and "no candidate" in chosen["reason"]
