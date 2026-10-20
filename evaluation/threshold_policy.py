"""Compare Mode B's retrieval acceptance policies on the frozen evaluation set.

Offline and read-only: it reads saved files only (the labelled dataset, a saved
retrieval run, saved cross-encoder top-12 scores). It loads no model and makes
no call. Three policies are compared on the same questions:

* the production gate, `RETRIEVAL_MIN_SCORE` (a Qdrant **cosine** similarity,
  `score >= min`), decided with `workflow.assess_question`;
* a cosine threshold re-chosen on the calibration split;
* the candidate reranker floor: the **cross-encoder logit** (unbounded, not a
  cosine) of a question's best reranked passage. The production workflow has
  no such gate; it exists only here, as a candidate.

A floor is "locked" only if `lock_verdict` says so. Its criteria are fixed in
code before the numbers are read: independent labels, no answerable question
rejected and no unrelated question accepted when the floor is fitted on one
half and tested on the other (both directions), and the two halves' floors
closer together than the margin to the lowest answerable score.

    PYTHONPATH=. backend/.venv/Scripts/python.exe -m evaluation.threshold_policy \
        --dataset evaluation/datasets/mode_b_v1 \
        --retrieval-run evaluation/output/mode_b_eval/run-20261018-v1/retrieval-run.json \
        --rerank-scores evaluation/output/mode_b_eval/run-20261018-v2/rerank-scores.json \
        --output evaluation/output/mode_b_eval/run-20261019-v3/threshold-policy.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation import mode_b

ANSWERABLE = mode_b.ANSWERABLE
LABELS = mode_b.LABELS


def tally(ids, labels, decisions, group: str | None = None) -> dict:
    """Accepted/total per label, or per `group` value then label."""
    out: dict = {}
    for q in ids:
        row = out
        if group:
            row = out.setdefault(labels[q][group], {})
        cell = row.setdefault(labels[q]["label"], {"accepted": 0, "n": 0})
        cell["n"] += 1
        cell["accepted"] += bool(decisions[q])
    return out


def answerable_split(ids, labels, decisions) -> dict:
    """Relevant + partially relevant questions against weak + unrelated ones."""
    out = {
        "answerable": {"accepted": 0, "n": 0},
        "unanswerable": {"accepted": 0, "n": 0},
    }
    for q in ids:
        cell = out["answerable" if labels[q]["label"] in ANSWERABLE else "unanswerable"]
        cell["n"] += 1
        cell["accepted"] += bool(decisions[q])
    return out


def rerank_top1(scores: dict[str, list[float]]) -> dict[str, float]:
    return {q: max(values) for q, values in scores.items()}


def fit_floor(ids, labels, top1) -> float | None:
    """Midpoint between the best unrelated and the worst relevant top-1 logit.

    `None` when the classes overlap: no floor separates them on these ids.
    """
    unrelated = [top1[q] for q in ids if labels[q]["label"] == "unrelated"]
    relevant = [top1[q] for q in ids if labels[q]["label"] == "relevant"]
    if not unrelated or not relevant or max(unrelated) >= min(relevant):
        return None
    return (max(unrelated) + min(relevant)) / 2


def cross_fit(labels, top1, folds) -> list[dict]:
    """Fit the floor on each fold, test it on the other, in both directions."""
    runs = []
    for fit_index, fit_ids in enumerate(folds):
        test_ids = folds[1 - fit_index]
        floor = fit_floor(fit_ids, labels, top1)
        decisions = {q: floor is not None and top1[q] >= floor for q in test_ids}
        runs.append(
            {
                "fit_fold": fit_index,
                "floor": floor,
                "test": tally(test_ids, labels, decisions),
                "answerable_rejected": sorted(
                    q
                    for q in test_ids
                    if labels[q]["label"] in ANSWERABLE and not decisions[q]
                ),
            }
        )
    return runs


def lock_verdict(
    *,
    labels_independent: bool,
    cross_fit_answerable_rejected: list[list[str]],
    cross_fit_unrelated_accepted: list[int],
    floors: list[float | None],
    lowest_answerable: float,
) -> dict:
    reasons = []
    if not labels_independent:
        reasons.append(
            "labels are agent-authored, not independent "
            "(evaluation/output/mode_b_calibration/independent/PROTOCOL.md)"
        )
    for rejected in cross_fit_answerable_rejected:
        if rejected:
            reasons.append(f"answerable questions rejected: {', '.join(rejected)}")
    if any(cross_fit_unrelated_accepted):
        reasons.append("an unrelated question was accepted on a held-out half")
    if None in floors:
        reasons.append("a half has no separating floor")
    else:
        spread = max(floors) - min(floors)
        margin = lowest_answerable - max(floors)
        if spread > margin:
            reasons.append(
                f"the two halves' floors differ by {spread:.2f}, more than the "
                f"{margin:.2f} margin to the lowest answerable score"
            )
    return {"decision": "defer" if reasons else "lock", "reasons": reasons}


# --- the files ----------------------------------------------------------------------


def _enriched(hits, catalogue):
    passages, titles = catalogue["passages"], catalogue["titles"]
    return [
        {
            **hit,
            "text": passages.get(hit["id"], {}).get("text", ""),
            "title": titles.get(hit["paper_id"]),
        }
        for hit in hits
    ]


def cosine_decisions(dataset, run, catalogue, settings, threshold, ids) -> dict:
    """The production gate (scope-aware paper minimum) at a cosine threshold."""
    mode_b._backend()
    from app.core.config import Settings
    from app.interactive.workflow import assess_question

    specific = settings.get(
        "retrieval_min_papers_paper_specific",
        Settings.model_fields["retrieval_min_papers_paper_specific"].default,
    )
    return {
        q: assess_question(
            dataset["questions"][q],
            _enriched(run["hits"][q], catalogue),
            threshold,
            settings["retrieval_min_chunks"],
            settings["retrieval_min_papers"],
            specific,
        )["sufficient"]
        for q in ids
    }


def _block(ids, labels, decisions) -> dict:
    return {
        "by_label": tally(ids, labels, decisions),
        "by_question_type": tally(ids, labels, decisions, group="question_type"),
        **answerable_split(ids, labels, decisions),
    }


def evaluate(dataset_dir, passages, papers, retrieval_run, rerank_scores) -> dict:
    catalogue = mode_b.load_catalogue(passages, papers)
    dataset = mode_b.load_dataset(dataset_dir, catalogue)
    run = mode_b.load_run(retrieval_run, dataset)
    settings = run["document"]["current_settings"]
    labels = dataset["labels"]
    scored = sorted(q for q in labels if q in run["hits"])
    calibration, held_out = mode_b.split(scored)
    top1 = rerank_top1(json.loads(Path(rerank_scores).read_text("utf-8"))["scores"])
    chosen = mode_b.choose_threshold(
        calibration, labels, run["hits"],
        settings["retrieval_min_chunks"], settings["retrieval_min_papers"],
    )["threshold"]  # fmt: skip
    floor = fit_floor(calibration, labels, top1)

    policies = {
        "cosine_production": {
            "applies_to": "Qdrant cosine similarity of each retrieved passage",
            "threshold": settings["retrieval_min_score"],
            "decisions": cosine_decisions(
                dataset, run, catalogue, settings, settings["retrieval_min_score"], scored
            ),
        },
        "cosine_calibration_choice": {
            "applies_to": "Qdrant cosine similarity of each retrieved passage",
            "threshold": chosen,
            "decisions": cosine_decisions(dataset, run, catalogue, settings, chosen, scored),
        },
        "rerank_floor_candidate": {
            "applies_to": "cross-encoder logit of the question's best reranked passage "
            "(unbounded; never compared with a cosine)",
            "threshold": floor,
            "decisions": {q: top1[q] >= floor for q in scored},
        },
    }  # fmt: skip
    report = {
        name: {
            "applies_to": p["applies_to"],
            "threshold": p["threshold"],
            "held_out": _block(held_out, labels, p["decisions"]),
            "calibration": _block(calibration, labels, p["decisions"]),
            "all": _block(scored, labels, p["decisions"]),
        }
        for name, p in policies.items()
    }
    folds = (calibration, held_out)
    runs = cross_fit(labels, top1, folds)
    answerable = sorted(top1[q] for q in scored if labels[q]["label"] in ANSWERABLE)
    unrelated = sorted(top1[q] for q in scored if labels[q]["label"] == "unrelated")
    verdict = lock_verdict(
        labels_independent=False,
        cross_fit_answerable_rejected=[r["answerable_rejected"] for r in runs],
        cross_fit_unrelated_accepted=[
            r["test"].get("unrelated", {"accepted": 0})["accepted"] for r in runs
        ],
        floors=[r["floor"] for r in runs],
        lowest_answerable=answerable[0],
    )
    return {
        "note": (
            "Provisional, agent-authored labels (mode_b_v1). The same agent wrote the "
            "gate rules; nothing here is independent validation."
        ),
        "split": {"calibration": len(calibration), "held_out": len(held_out)},
        "policies": report,
        "rerank_cross_fit": runs,
        "rerank_separation_all_130": {
            "highest_unrelated": unrelated[-1],
            "lowest_answerable": answerable[0],
            "gap": answerable[0] - unrelated[-1],
        },
        "rerank_floor_verdict": verdict,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mode B acceptance-policy comparison")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--passages", type=Path, default=mode_b.DEFAULT_PASSAGES)
    parser.add_argument("--papers", type=Path, default=mode_b.DEFAULT_PAPERS)
    parser.add_argument("--retrieval-run", type=Path, required=True)
    parser.add_argument("--rerank-scores", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = evaluate(
        args.dataset, args.passages, args.papers, args.retrieval_run, args.rerank_scores
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=1, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(result["rerank_floor_verdict"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
