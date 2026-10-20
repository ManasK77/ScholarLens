"""Phase 13 evaluation of Mode B (Ask ScholarLens): retrieval gate, claim check, live runs.

Offline and read-only. Inputs are files: a labelled dataset
(`evaluation/datasets/<name>/`), the passage catalogue it was labelled
against, a saved retrieval run (`app.interactive.calibration --mode embed
--questions ...`, made inside the backend container, no LLM), and optionally
saved live `/query` results (`app.interactive.live_check`) with their review
records. Nothing here loads a model, calls an API or touches a store.

The production rules are imported, never copied: the sufficiency gate is
`app.interactive.workflow.assess_sufficiency`, the split is
`app.interactive.calibration.split_tune_test`, the claim check is
`app.interactive.claim_check.check_claims`.

    PYTHONPATH=. backend/.venv/bin/python -m evaluation.mode_b \
        --dataset evaluation/datasets/mode_b_v1 \
        --retrieval-run evaluation/output/mode_b_eval/<run>/retrieval-run.json \
        --output-dir evaluation/output/mode_b_eval/<run> --run-id <run> \
        [--live result.json ...] [--live-review review.csv]
    PYTHONPATH=. backend/.venv/bin/python -m evaluation.mode_b \
        --reproduce evaluation/output/mode_b_eval/<run>/manifest.json

What it reports is defined in `evaluation/README.md` ("Mode B metrics").
Every rate carries its numerator and denominator; a rate with no
denominator is `None`, never 0; calibration and held-out numbers are kept
apart and the threshold is chosen on the calibration split only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
DEFAULT_PASSAGES = (
    REPO / "evaluation/output/mode_b_calibration/independent/passages.csv"
)
DEFAULT_PAPERS = REPO / "evaluation/output/mode_b_calibration/independent/papers.csv"
SCHEMA = "scholarlens-mode-b-eval/1"

LABELS = ("relevant", "partially_relevant", "weak", "unrelated")
ANSWERABLE = frozenset({"relevant", "partially_relevant"})
QUESTION_TYPES = frozenset(
    {
        "factual", "numeric", "comparison", "limitations", "future_work",
        "synthesis", "causal_explanation", "absence_stated", "partial_single_paper",
        "overgeneralization_trap", "misleading_overlap", "not_in_index",
        "adjacent_topic", "off_topic",
    }
)  # fmt: skip
CLAIM_LABELS = ("supported", "unsupported")
# Production code whose behaviour the results depend on; hashed per run.
CODE = (
    "backend/app/interactive/workflow.py",
    "backend/app/interactive/claim_check.py",
    "backend/app/interactive/intent.py",
    "backend/app/interactive/calibration.py",
    "evaluation/mode_b.py",
)


class DatasetError(ValueError):
    """The inputs are inconsistent; the message lists every problem found."""


def _backend():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_csv(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _ids(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(";") if part.strip()]


# --- inputs -------------------------------------------------------------------------


def load_catalogue(passages: Path, papers: Path) -> dict:
    """The passage catalogue (one row per indexed passage) and paper titles."""
    rows = {
        r["passage_id"]: {
            "paper_id": int(r["paper_id"]),
            "source_type": r["source_type"],
            "text": r["text"],
        }
        for r in _read_csv(passages)
    }
    titles = {int(r["paper_id"]): r["title"] for r in _read_csv(papers)}
    return {
        "passages": rows,
        "titles": titles,
        "hashes": {
            "passages.csv": sha256_file(passages),
            "papers.csv": sha256_file(papers),
        },
    }


def _load_claims(path: Path, passages: dict) -> tuple[list[dict], list[str]]:
    """Labelled claims and every problem found in them (rows are never dropped)."""
    claims: list[dict] = []
    problems: list[str] = []
    seen_claims: set[str] = set()
    for row in _read_csv(path):
        cid = (row.get("claim_id") or "").strip()
        where = f"{path.name}: {cid}"
        if cid in seen_claims:
            problems.append(f"{where}: duplicate claim_id")
        seen_claims.add(cid)
        if row.get("label") not in CLAIM_LABELS:
            problems.append(f"{where}: unknown label {row.get('label')!r}")
        cited = _ids(row.get("cited_passage_ids"))
        if not cited:
            problems.append(f"{where}: cites no passage")
        problems.extend(
            f"{where}: unknown passage {p}" for p in cited if p not in passages
        )
        if not (row.get("claim") or "").strip():
            problems.append(f"{where}: empty claim")
        claims.append(
            {
                "claim_id": cid,
                "label": row.get("label"),
                "phenomenon": (row.get("phenomenon") or "").strip(),
                "cited_passage_ids": cited,
                "claim": (row.get("claim") or "").strip(),
            }
        )

    return claims, problems


def load_dataset(directory: Path, catalogue: dict) -> dict:
    """Questions, labels and claims, validated together. Never drops a row:
    every problem is collected and raised as one `DatasetError`."""
    directory = Path(directory)
    problems: list[str] = []
    passages = catalogue["passages"]

    questions: dict[str, str] = {}
    for row in _read_csv(directory / "questions.csv"):
        qid = (row.get("question_id") or "").strip()
        if qid in questions:
            problems.append(f"questions.csv: duplicate question_id {qid}")
        if not (row.get("question") or "").strip():
            problems.append(f"questions.csv: {qid} has no question text")
        questions[qid] = (row.get("question") or "").strip()

    labels: dict[str, dict] = {}
    for row in _read_csv(directory / "labels.csv"):
        qid = (row.get("question_id") or "").strip()
        where = f"labels.csv: {qid}"
        if qid in labels:
            problems.append(f"{where}: duplicate question_id")
            continue
        label = (row.get("label") or "").strip()
        if label not in LABELS:
            problems.append(f"{where}: unknown label {label!r}")
        qtype = (row.get("question_type") or "").strip()
        if qtype not in QUESTION_TYPES:
            problems.append(f"{where}: unknown question_type {qtype!r}")
        try:
            papers = [int(p) for p in _ids(row.get("relevant_paper_ids"))]
        except ValueError:
            problems.append(f"{where}: relevant_paper_ids must be integers")
            papers = []
        expected = _ids(row.get("relevant_passage_ids"))
        if label in ANSWERABLE and not expected:
            problems.append(
                f"{where}: a {label} question needs at least one expected passage"
            )
        if label in ("weak", "unrelated") and (expected or papers):
            problems.append(
                f"{where}: a {label} question must not list expected evidence"
            )
        for pid in expected:
            if pid not in passages:
                problems.append(f"{where}: unknown passage {pid}")
            elif passages[pid]["paper_id"] not in papers:
                problems.append(
                    f"{where}: passage {pid} is from paper "
                    f"{passages[pid]['paper_id']}, not in relevant_paper_ids"
                )
        try:
            needed = int(row.get("papers_needed") or 1)
        except ValueError:
            problems.append(f"{where}: papers_needed must be an integer")
            needed = 1
        labels[qid] = {
            "label": label,
            "question_type": qtype,
            "papers_needed": needed,
            "relevant_paper_ids": papers,
            "relevant_passage_ids": expected,
        }
    for qid in sorted(set(questions) - set(labels)):
        problems.append(f"labels.csv: question {qid} has no label")
    for qid in sorted(set(labels) - set(questions)):
        problems.append(f"labels.csv: label {qid} has no question in questions.csv")

    claims, claim_problems = _load_claims(directory / "claims.csv", passages)
    problems.extend(claim_problems)

    if problems:
        raise DatasetError("; ".join(problems))
    card = directory / "dataset.json"
    provenance = (
        json.loads(card.read_text(encoding="utf-8"))
        if card.exists()
        # Independence is never assumed: a dataset must declare it.
        else {"source": "unspecified", "independent": False}
    )
    provenance.setdefault("independent", False)
    return {
        "questions": questions,
        "labels": labels,
        "claims": claims,
        "provenance": provenance,
        "hashes": {
            name: sha256_file(directory / name)
            for name in ("questions.csv", "labels.csv", "claims.csv")
        },
    }


def load_run(path: Path, dataset: dict) -> dict:
    """A saved embed run, checked against the dataset it claims to score."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = []
    provenance = document.get("provenance") or {}
    if provenance.get("questions_sha256") != dataset["hashes"]["questions.csv"]:
        problems.append(
            "the retrieval run was made from a different questions.csv "
            "(questions_sha256 differs); re-run the embed step"
        )
    if provenance.get("query_matches_stored_vectors") is not True:
        problems.append(
            "the run's query encoder does not match the stored vectors' "
            "embed_version; its cosines are not comparable"
        )
    hits: dict[str, list[dict]] = {}
    for row in document.get("queries") or []:
        qid = row.get("question_id")
        if qid in dataset["questions"] and row.get("text") != dataset["questions"][qid]:
            problems.append(f"run question {qid} text differs from questions.csv")
        if not isinstance(row.get("hits"), list):
            problems.append(f"run question {qid} has no hit list")
            continue
        for hit in row["hits"]:
            if not {"id", "paper_id", "score"} <= set(hit):
                problems.append(f"run question {qid} has a malformed hit")
                break
        hits[qid] = row["hits"]
    if problems:
        raise DatasetError("; ".join(problems))
    return {"hits": hits, "document": document}


# --- statistics -----------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes in n trials; `None` when n = 0."""
    if n == 0:
        return None
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def _rate(k: int, n: int) -> dict:
    return {
        "k": k,
        "n": n,
        "rate": round(k / n, 4) if n else None,
        "ci95": wilson(k, n),
    }


def auroc(positive: list[float | None], negative: list[float | None]) -> float | None:
    """P(a positive outscores a negative), ties counted half. `None` scores
    (never accepted at any threshold) rank below every real score."""
    if not positive or not negative:
        return None

    def value(score):
        return -math.inf if score is None else score

    wins = 0.0
    for p in positive:
        for q in negative:
            a, b = value(p), value(q)
            wins += 1.0 if a > b else 0.5 if a == b else 0.0
    return round(wins / (len(positive) * len(negative)), 4)


def _percentiles(values: list[float]) -> dict:
    _backend()
    from app.interactive.calibration import percentiles

    return percentiles(values)


# --- the gate -----------------------------------------------------------------------


def accepted(
    hits: list[dict], threshold: float, min_chunks: int, min_papers: int
) -> bool:
    _backend()
    from app.interactive.workflow import assess_sufficiency

    return assess_sufficiency(list(hits), threshold, min_chunks, min_papers)[
        "sufficient"
    ]


def gate_score(hits: list[dict], min_chunks: int, min_papers: int) -> float | None:
    """The highest threshold at which Mode B's rule accepts these hits."""
    for score in sorted({float(h["score"]) for h in hits}, reverse=True):
        if accepted(hits, score, min_chunks, min_papers):
            return score
    return None


def split(question_ids: list[str]) -> tuple[list[str], list[str]]:
    """(calibration, held_out): the production hash split, by id alone."""
    _backend()
    from app.interactive.calibration import split_tune_test

    return split_tune_test(question_ids)


def _binary(tp: int, fp: int, tn: int, fn: int) -> dict:
    def ratio(a, b):
        return round(a / b, 4) if b else None

    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    specificity = ratio(tn, tn + fp)
    f1 = (
        round(2 * precision * recall / (precision + recall), 4)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    balanced = (
        round((recall + specificity) / 2, 4)
        if recall is not None and specificity is not None
        else None
    )
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": precision, "recall": recall, "specificity": specificity,
        "f1": f1, "balanced_accuracy": balanced,
    }  # fmt: skip


def gate_metrics(
    question_ids: list[str],
    labels: dict,
    run: dict,
    threshold: float,
    min_chunks: int,
    min_papers: int,
) -> dict:
    """Round-1 gate decisions against labels at one threshold.

    Positive = "the gate accepts". `answerable_vs_not` treats relevant and
    partially relevant as should-accept; `relevant_vs_unrelated` scores only
    the two unambiguous classes.
    """
    ids = [q for q in question_ids if q in labels and q in run]
    decision = {q: accepted(run[q], threshold, min_chunks, min_papers) for q in ids}
    by_label = {
        label: [q for q in ids if labels[q]["label"] == label] for label in LABELS
    }
    confusion = {
        label: {
            "accept": sum(decision[q] for q in qs),
            "reject": sum(not decision[q] for q in qs),
        }
        for label, qs in by_label.items()
    }

    def count(labels_, outcome):
        return sum(decision[q] == outcome for label in labels_ for q in by_label[label])

    answerable = _binary(
        count(ANSWERABLE, True),
        count(("weak", "unrelated"), True),
        count(("weak", "unrelated"), False),
        count(ANSWERABLE, False),
    )
    strict = _binary(
        count(("relevant",), True),
        count(("unrelated",), True),
        count(("unrelated",), False),
        count(("relevant",), False),
    )
    return {
        "threshold": threshold,
        "min_chunks": min_chunks,
        "min_papers": min_papers,
        "n": len(ids),
        "confusion": confusion,
        "answerable_vs_not": answerable,
        "relevant_vs_unrelated": strict,
        "false_accept": {
            label: _rate(confusion[label]["accept"], len(by_label[label]))
            for label in ("weak", "unrelated")
        }
        | {
            "weak_or_unrelated": _rate(
                count(("weak", "unrelated"), True),
                len(by_label["weak"]) + len(by_label["unrelated"]),
            )
        },
        "false_reject": {
            label: _rate(confusion[label]["reject"], len(by_label[label]))
            for label in ("relevant", "partially_relevant")
        },
        # Each rejection is a reformulation call and, after 3 rounds, a refusal.
        "rejected": _rate(sum(not decision[q] for q in ids), len(ids)),
    }


def candidate_thresholds(scores: list[float | None]) -> list[float]:
    """Midpoints between consecutive observed gate scores, plus their ends."""
    values = sorted({round(s, 6) for s in scores if s is not None})
    if not values:
        return []
    middles = [round((a + b) / 2, 6) for a, b in itertools.pairwise(values)]
    return sorted({round(values[0] - 1e-6, 6), *middles, round(values[-1] + 1e-6, 6)})


def choose_threshold(
    calibration_ids: list[str],
    labels: dict,
    run: dict,
    min_chunks: int,
    min_papers: int,
) -> dict:
    """Best balanced accuracy (answerable vs not) on the calibration split.

    Candidates come from the calibration split's own observed scores. Ties go
    to the lower threshold, which refuses fewer answerable questions. Returns
    `{"threshold": None, ...}` when the split lacks either class.
    """
    ids = [q for q in calibration_ids if q in labels and q in run]
    positives = [q for q in ids if labels[q]["label"] in ANSWERABLE]
    negatives = [q for q in ids if labels[q]["label"] not in ANSWERABLE]
    if not positives or not negatives:
        return {
            "threshold": None,
            "reason": "calibration split lacks a class",
            "n": len(ids),
        }
    scores = [gate_score(run[q], min_chunks, min_papers) for q in ids]
    best = None
    for threshold in candidate_thresholds(scores):
        metrics = gate_metrics(ids, labels, run, threshold, min_chunks, min_papers)
        value = metrics["answerable_vs_not"]["balanced_accuracy"]
        if best is None or value > best[0]:
            best = (value, threshold)
    if best is None:
        # No calibration question is accepted at any threshold (e.g. every
        # hit from one paper under the two-paper rule): nothing to choose.
        return {
            "threshold": None,
            "reason": "no candidate threshold: the gate accepts no calibration question",
            "n": len(ids),
        }
    return {
        "threshold": best[1],
        "objective": "balanced accuracy, answerable (relevant+partial) vs weak+unrelated",
        "calibration_balanced_accuracy": best[0],
        "n": len(ids),
    }


def ranking_metrics(question_ids: list[str], labels: dict, run: dict) -> dict:
    """Expected-evidence recall over the retrieved top-k (lower bounds: the
    expected lists are not exhaustive)."""
    passage, paper, rr, top1 = [], [], [], []
    for qid in question_ids:
        label = labels.get(qid)
        if not label or qid not in run or not label["relevant_passage_ids"]:
            continue
        hits = [h for h in run[qid] if h.get("source_type") != "graph"]
        wanted = set(label["relevant_passage_ids"])
        got = [str(h["id"]) for h in hits]
        passage.append(len(wanted & set(got)) / len(wanted))
        rank = next((i for i, h in enumerate(got, 1) if h in wanted), None)
        rr.append(1 / rank if rank else 0.0)
        papers = set(label["relevant_paper_ids"])
        paper.append(len(papers & {h["paper_id"] for h in hits}) / len(papers))
        top1.append(bool(hits) and hits[0]["paper_id"] in papers)

    def mean(values):
        return {
            "n": len(values),
            "mean": round(sum(values) / len(values), 4) if values else None,
        }

    return {
        "passage_recall_at_k": mean(passage),
        "paper_recall_at_k": mean(paper),
        "mrr": mean(rr),
        "top1_paper_expected": _rate(sum(top1), len(top1)),
    }


def _by_group(
    ids: list[str], labels: dict, run: dict, key: str, threshold, chunks, papers
):
    groups: dict[str, list[str]] = {}
    for q in ids:
        if q in labels and q in run:
            groups.setdefault(str(labels[q][key]), []).append(q)
    return {
        group: {
            "n": len(qs),
            "labels": sorted({labels[q]["label"] for q in qs}),
            "accepted": _rate(
                sum(accepted(run[q], threshold, chunks, papers) for q in qs), len(qs)
            ),
        }
        for group, qs in sorted(groups.items())
    }


def evaluate_retrieval(dataset: dict, run: dict, settings: dict) -> dict:
    labels, hits = dataset["labels"], run["hits"]
    chunks, papers = settings["retrieval_min_chunks"], settings["retrieval_min_papers"]
    current = settings["retrieval_min_score"]
    labelled = sorted(labels)
    scored = [q for q in labelled if q in hits]
    calibration, held_out = split(scored)
    chosen = choose_threshold(calibration, labels, hits, chunks, papers)

    def block(ids, threshold, min_papers=papers):
        return gate_metrics(ids, labels, hits, threshold, chunks, min_papers)

    scores = {q: gate_score(hits[q], chunks, papers) for q in scored}
    by_label = {
        label: [scores[q] for q in scored if labels[q]["label"] == label]
        for label in LABELS
    }
    answerable = [s for label in ANSWERABLE for s in by_label[label]]
    rest = by_label["weak"] + by_label["unrelated"]
    result = {
        "coverage": {
            "labelled": len(labelled),
            "scored": len(scored),
            "missing_from_run": sorted(set(labelled) - set(hits)),
            "unlabelled_in_run": sorted(set(hits) - set(labelled)),
            "split": {"calibration": len(calibration), "held_out": len(held_out)},
            "label_counts": {
                part: {
                    label: sum(labels[q]["label"] == label for q in ids)
                    for label in LABELS
                }
                for part, ids in (("calibration", calibration), ("held_out", held_out))
            },
        },
        "gate_score": {
            "definition": (
                "highest threshold at which Mode B's sufficiency rule "
                f"({chunks} chunks, {papers} papers) accepts the question's round-1 hits"
            ),
            "by_label": {
                label: _percentiles([s for s in v if s is not None])
                for label, v in by_label.items()
            },
            "never_accepted": {
                label: sum(s is None for s in v) for label, v in by_label.items()
            },
            "auroc": {
                "relevant_vs_unrelated": auroc(
                    by_label["relevant"], by_label["unrelated"]
                ),
                "answerable_vs_weak_or_unrelated": auroc(answerable, rest),
                "partial_vs_weak": auroc(
                    by_label["partially_relevant"], by_label["weak"]
                ),
                "relevant_vs_weak": auroc(by_label["relevant"], by_label["weak"]),
            },
            "overlap": {
                "max_unrelated": max(
                    (s for s in by_label["unrelated"] if s is not None), default=None
                ),
                "min_relevant": min(
                    (s for s in by_label["relevant"] if s is not None), default=None
                ),
                "max_weak": max(
                    (s for s in by_label["weak"] if s is not None), default=None
                ),
                "min_partial": min(
                    (s for s in by_label["partially_relevant"] if s is not None),
                    default=None,
                ),
            },
        },
        "chosen_on_calibration": chosen,
        "current_threshold": {
            "threshold": current,
            "all": block(scored, current),
            "calibration": block(calibration, current),
            "held_out": block(held_out, current),
        },
        "ranking": {
            "all": ranking_metrics(scored, labels, hits),
            "held_out": ranking_metrics(held_out, labels, hits),
        },
    }
    if chosen["threshold"] is not None:
        t = chosen["threshold"]
        result["chosen_threshold"] = {
            "threshold": t,
            "calibration": block(calibration, t),
            # Never used to choose t: reported once, as the held-out estimate.
            "held_out": block(held_out, t),
            "held_out_by_question_type": _by_group(
                held_out, labels, hits, "question_type", t, chunks, papers
            ),
            "all_by_question_type": _by_group(
                scored, labels, hits, "question_type", t, chunks, papers
            ),
            "distinct_paper_rule": {
                f"min_papers={p}": {
                    "held_out": block(held_out, t, p),
                    "all": block(scored, t, p),
                }
                for p in (1, 2)
            },
        }
        result["current_threshold"]["by_question_type"] = _by_group(
            scored, labels, hits, "question_type", current, chunks, papers
        )
        result["current_threshold"]["distinct_paper_rule"] = {
            f"min_papers={p}": block(scored, current, p) for p in (1, 2)
        }
    by_paper: dict[int, list[str]] = {}
    for q in scored:
        if labels[q]["label"] == "relevant" and labels[q]["papers_needed"] == 1:
            for p in labels[q]["relevant_paper_ids"]:
                by_paper.setdefault(p, []).append(q)
    result["single_paper_relevant_by_paper"] = {
        str(p): {
            "n": len(qs),
            "expected_paper_in_top_k": sum(
                p in {h["paper_id"] for h in hits[q]} for q in qs
            ),
            "gate_scores": [scores[q] for q in qs],
        }
        for p, qs in sorted(by_paper.items())
    }
    return result


# --- claim check ------------------------------------------------------------------------


def evaluate_claims(dataset: dict, catalogue: dict) -> dict:
    """Deterministic claim-check vetoes against the labelled claims."""
    _backend()
    from app.interactive.claim_check import check_claims

    passages, titles = catalogue["passages"], catalogue["titles"]
    evidence = {
        pid: {
            "id": pid,
            "paper_id": row["paper_id"],
            "source_type": row["source_type"],
            "text": row["text"],
            "title": titles.get(row["paper_id"]),
        }
        for pid, row in passages.items()
    }
    claims = dataset["claims"]
    vetoes = check_claims(
        [{"text": c["claim"], "evidence_ids": c["cited_passage_ids"]} for c in claims],
        evidence,
    )
    rows = [
        {**{k: c[k] for k in ("claim_id", "label", "phenomenon")}, "reasons": reasons}
        for c, reasons in zip(claims, vetoes, strict=True)
    ]
    by_phenomenon: dict[str, dict] = {}
    for row in rows:
        entry = by_phenomenon.setdefault(
            row["phenomenon"], {"label": row["label"], "n": 0, "vetoed": 0}
        )
        entry["n"] += 1
        entry["vetoed"] += bool(row["reasons"])
    reasons: dict[str, int] = {}
    for row in rows:
        for reason in row["reasons"]:
            reasons[reason] = reasons.get(reason, 0) + 1

    def part(label):
        chosen = [r for r in rows if r["label"] == label]
        return {"vetoed": _rate(sum(bool(r["reasons"]) for r in chosen), len(chosen))}

    return {
        "note": (
            "The deterministic checks can only reject. 'vetoed' on unsupported "
            "claims is their catch rate; the rest reach the LLM grounding check. "
            "'vetoed' on supported claims is a false rejection (over-refusal). "
            "No result here establishes entailment."
        ),
        "supported": part("supported"),
        "unsupported": part("unsupported"),
        "by_phenomenon": dict(sorted(by_phenomenon.items())),
        "reasons": dict(sorted(reasons.items())),
        "false_rejections": [
            r for r in rows if r["label"] == "supported" and r["reasons"]
        ],
        "passed_to_llm_check": [
            r for r in rows if r["label"] == "unsupported" and not r["reasons"]
        ],
    }


def load_claims_supplement(directory: Path, catalogue: dict) -> dict:
    """A further labelled claim set (`claims.csv` + `dataset.json`), validated
    like the main one and scored on its own, never pooled with it."""
    directory = Path(directory)
    claims, problems = _load_claims(directory / "claims.csv", catalogue["passages"])
    if problems:
        raise DatasetError("; ".join(problems))
    card = directory / "dataset.json"
    provenance = (
        json.loads(card.read_text(encoding="utf-8"))
        if card.exists()
        else {"source": "unspecified", "independent": False}
    )
    provenance.setdefault("independent", False)
    return {
        "claims": claims,
        "provenance": provenance,
        "hashes": {"claims.csv": sha256_file(directory / "claims.csv")},
    }


def evaluate_scope_policy(
    dataset: dict, run: dict, catalogue: dict, settings: dict, thresholds
) -> dict:
    """The scope-aware sufficiency rule against the two-paper rule, per question.

    Both decisions use the production functions on the same round-1 hits:
    `workflow.assess_sufficiency` with the configured paper minimum (the old
    rule for every question) and `workflow.assess_question` with the
    paper-specific minimum for a single-paper question (the new rule). That
    minimum comes from `settings` when the run recorded it, else from the
    central settings default. Hits get their passage text and paper title from
    the catalogue, as the live workflow gives them.
    """
    _backend()
    from app.core.config import Settings
    from app.interactive.workflow import assess_question, assess_sufficiency

    labels, questions = dataset["labels"], dataset["questions"]
    passages, titles = catalogue["passages"], catalogue["titles"]
    chunks, papers = settings["retrieval_min_chunks"], settings["retrieval_min_papers"]
    specific = settings.get(
        "retrieval_min_papers_paper_specific",
        Settings.model_fields["retrieval_min_papers_paper_specific"].default,
    )
    scored = sorted(q for q in labels if q in run["hits"])
    calibration, held_out = split(scored)

    def enriched(q):
        return [
            {
                **hit,
                "text": passages.get(hit["id"], {}).get("text", ""),
                "title": titles.get(hit["paper_id"]),
            }
            for hit in run["hits"][q]
        ]

    def tally(ids, decisions):
        return {
            label: {
                "accepted": sum(
                    decisions[q] for q in ids if labels[q]["label"] == label
                ),
                "n": sum(labels[q]["label"] == label for q in ids),
            }
            for label in LABELS
        }

    thresholds = [t for t in dict.fromkeys(thresholds) if t is not None]
    report, by_needed = {}, {}
    for t in thresholds:
        old = {
            q: assess_sufficiency(enriched(q), t, chunks, papers)["sufficient"]
            for q in scored
        }
        verdicts = {
            q: assess_question(questions[q], enriched(q), t, chunks, papers, specific)
            for q in scored
        }
        new = {q: v["sufficient"] for q, v in verdicts.items()}
        # The scope reads the titles of the passages that qualify at `t`.
        scopes = {q: v["scope"] for q, v in verdicts.items()}
        if t == thresholds[0]:
            for q in scored:
                row = by_needed.setdefault(str(labels[q]["papers_needed"]), {})
                row[scopes[q]] = row.get(scopes[q], 0) + 1
        report[str(t)] = {
            part: {"two_paper_rule": tally(ids, old), "scope_policy": tally(ids, new)}
            for part, ids in (
                ("all", scored),
                ("calibration", calibration),
                ("held_out", held_out),
            )
        } | {
            "changed": [
                {
                    "question_id": q,
                    "label": labels[q]["label"],
                    "scope": scopes[q],
                    "two_paper_rule": old[q],
                    "scope_policy": new[q],
                }
                for q in scored
                if old[q] != new[q]
            ]
        }
    return {
        "note": (
            "Labels are provisional and agent-authored (not independent). The scope "
            "rule was written after the Phase 13 run had exposed p031/p032, so the "
            "'all' figures are development figures; nothing here was tuned on "
            "held-out labels."
        ),
        "min_papers_paper_specific": specific,
        "scope_by_papers_needed": {
            k: dict(sorted(v.items())) for k, v in sorted(by_needed.items())
        },
        "thresholds": report,
    }


def evaluate_relevance_rule(dataset: dict, catalogue: dict) -> dict:
    """How often the answer-relevance rule (`intent.addresses_question`) would
    refuse a correct answer, with each answerable question's expected passages
    (and their papers' titles) standing in for that answer's evidence.

    A proxy: a real answer cites what retrieval found, not the labelled
    passages. For weak and unrelated questions it only counts how many name a
    subject at all -- the rule can act on no others.
    """
    _backend()
    from app.interactive.intent import addresses_question, question_subjects

    labels, questions = dataset["labels"], dataset["questions"]
    passages, titles = catalogue["passages"], catalogue["titles"]
    answerable, refused, unanswerable, with_subjects = 0, [], 0, 0
    for q in sorted(labels):
        if not question_subjects(questions[q]):
            if labels[q]["label"] not in ANSWERABLE:
                unanswerable += 1
            continue
        if labels[q]["label"] in ANSWERABLE:
            answerable += 1
            ids = labels[q]["relevant_passage_ids"]
            texts = [passages[p]["text"] for p in ids] + [
                titles.get(passages[p]["paper_id"], "") for p in ids
            ]
            if not addresses_question(questions[q], texts):
                refused.append(q)
        else:
            unanswerable += 1
            with_subjects += 1
    return {
        "note": (
            "Expected passages stand in for a correct answer's evidence; labels are "
            "provisional and agent-authored. The rule was added after live LV-Q3."
        ),
        "answerable_with_subjects": {"n": answerable, "would_refuse": refused},
        "unanswerable_with_subjects": {"n": with_subjects, "of": unanswerable},
    }


def evaluate_completeness(paths: list[Path], aspects_path: Path) -> dict:
    """Per live answer: which labelled aspects a *returned* claim cites evidence for.

    An aspect is covered when some returned claim cites one of the aspect's
    expected passages. Structured and auditable, deliberately strict: a claim
    that addresses an aspect from another passage counts as uncovered, so this
    is a lower bound on coverage, not a quality score.
    """
    aspects = json.loads(Path(aspects_path).read_text(encoding="utf-8"))
    result = {}
    for path in paths:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        test_id = doc.get("test_id") or Path(path).stem
        if test_id not in aspects:
            continue
        response = doc.get("response") or {}
        cited = {
            i for c in response.get("claims") or [] for i in c.get("evidence_ids") or []
        }
        covered = [
            a["id"] for a in aspects[test_id] if cited & set(a["expected_passage_ids"])
        ]
        result[test_id] = {
            "outcome": doc.get("outcome"),
            "aspects": len(aspects[test_id]),
            "covered": covered,
            "uncovered": [a["id"] for a in aspects[test_id] if a["id"] not in covered],
            "removed_claim_count": response.get("removed_claim_count", 0),
        }
    return result


# --- live results --------------------------------------------------------------------


def summarize_live(paths: list[Path], review_path: Path | None) -> dict:
    """Saved live `/query` results and their human or agent review rows."""
    runs = []
    for path in paths:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        response = doc.get("response") or {}
        workflow = doc.get("workflow") or {}
        expected = doc.get("expected_outcome")
        runs.append(
            {
                "file": Path(path).name,
                "test_id": doc.get("test_id"),
                "question": doc.get("question"),
                "verdict": doc.get("verdict"),
                "outcome": doc.get("outcome"),
                "expected_outcome": expected,
                "outcome_as_expected": None
                if expected is None
                else doc.get("outcome") == expected,
                "http_status": doc.get(
                    "http_status", 200 if doc.get("sent") and "checks" in doc else None
                ),
                "elapsed_seconds": doc.get("elapsed_seconds"),
                "retrieval_rounds": workflow.get("retrieval_rounds"),
                "reformulations": workflow.get("reformulations"),
                "generations": workflow.get("generations"),
                "regenerations": workflow.get("regenerations"),
                "grounding_reasons": workflow.get("grounding_reasons"),
                "llm_calls": doc.get("llm_calls"),
                "claims": len(response.get("claims") or []),
                "citations": len(response.get("citations") or []),
                "cited_papers": sorted(
                    {c.get("paper_id") for c in response.get("citations") or []}
                ),
                "expected_paper_overlap": doc.get("expected_paper_overlap"),
                "failed_checks": sorted(
                    k for k, v in (doc.get("checks") or {}).items() if not v.get("ok")
                ),
            }
        )
    reviews = _read_csv(review_path) if review_path else []
    by_type: dict[str, list[dict]] = {}
    for row in reviews:
        by_type.setdefault(row.get("reviewer_type", "unknown"), []).append(row)

    def review_block(rows):
        return {
            "claims_reviewed": len(rows),
            "independent_reviews": sum(r.get("independent") == "yes" for r in rows),
            "supported": _rate(
                sum(r.get("support") == "supported" for r in rows), len(rows)
            ),
            "partially_supported": _rate(
                sum(r.get("support") == "partial" for r in rows), len(rows)
            ),
            "unsupported": _rate(
                sum(r.get("support") == "unsupported" for r in rows), len(rows)
            ),
            "citation_correct": _rate(
                sum(r.get("citation_correct") == "yes" for r in rows), len(rows)
            ),
            "citation_complete": _rate(
                sum(r.get("citation_complete") == "yes" for r in rows), len(rows)
            ),
        }

    outcomes = [
        r["outcome_as_expected"] for r in runs if r["outcome_as_expected"] is not None
    ]
    return {
        "runs": runs,
        "outcome_as_expected": _rate(sum(outcomes), len(outcomes)),
        "loop_bound_violations": sum(
            (r["retrieval_rounds"] or 0) > 3 or (r["regenerations"] or 0) > 1
            for r in runs
        ),
        "reviews_by_reviewer_type": {
            k: review_block(v) for k, v in sorted(by_type.items())
        },
        "note": (
            "Live runs are few and chosen by the developer; their counts describe "
            "these runs only. Agent reviews are not independent human review."
        ),
    }


# --- run, write, reproduce ----------------------------------------------------------


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _digest(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def compute(
    dataset_dir,
    passages,
    papers,
    retrieval_run,
    live=(),
    live_review=None,
    claims_supplement=None,
    aspects=None,
) -> dict:
    """Every result, deterministically, from the given files."""
    catalogue = load_catalogue(passages, papers)
    dataset = load_dataset(dataset_dir, catalogue)
    run = load_run(retrieval_run, dataset)
    settings = run["document"]["current_settings"]
    retrieval = evaluate_retrieval(dataset, run, settings)
    supplement = (
        load_claims_supplement(claims_supplement, catalogue)
        if claims_supplement
        else None
    )
    warnings = [
        f"labelled question {q} is missing from the retrieval run and was not scored"
        for q in retrieval["coverage"]["missing_from_run"]
    ] + [
        f"run question {q} has no label and was not scored"
        for q in retrieval["coverage"]["unlabelled_in_run"]
    ]
    results = {
        "schema": SCHEMA,
        "retrieval": retrieval,
        "claims": evaluate_claims(dataset, catalogue),
        "claims_supplement": {
            **evaluate_claims(supplement, catalogue),
            "provenance": supplement["provenance"],
        }
        if supplement
        else None,
        "scope_policy": evaluate_scope_policy(
            dataset,
            run,
            catalogue,
            settings,
            [
                settings["retrieval_min_score"],
                retrieval["chosen_on_calibration"]["threshold"],
            ],
        ),
        "relevance_rule": evaluate_relevance_rule(dataset, catalogue),
        "live": summarize_live([Path(p) for p in live], live_review) if live else None,
        "completeness": evaluate_completeness([Path(p) for p in live], aspects)
        if live and aspects
        else None,
        "settings": settings,
        "warnings": warnings,
    }
    inputs = {
        "dataset_dir": str(Path(dataset_dir)),
        "passages": str(Path(passages)),
        "papers": str(Path(papers)),
        "retrieval_run": str(Path(retrieval_run)),
        "live": [str(Path(p)) for p in live],
        "live_review": str(Path(live_review)) if live_review else None,
        "claims_supplement": str(Path(claims_supplement))
        if claims_supplement
        else None,
        "aspects": str(Path(aspects)) if aspects else None,
        "sha256": {
            **{f"dataset/{k}": v for k, v in dataset["hashes"].items()},
            **{f"catalogue/{k}": v for k, v in catalogue["hashes"].items()},
            "retrieval_run": sha256_file(retrieval_run),
            **{f"live/{Path(p).name}": sha256_file(p) for p in live},
            **({"live_review": sha256_file(live_review)} if live_review else {}),
            **(
                {f"claims_supplement/{k}": v for k, v in supplement["hashes"].items()}
                if supplement
                else {}
            ),
            **({"aspects": sha256_file(aspects)} if aspects else {}),
        },
    }
    return {"results": results, "inputs": inputs, "dataset": dataset, "run": run}


def evaluate(
    dataset_dir,
    passages,
    papers,
    retrieval_run,
    output_dir,
    run_id,
    live=(),
    live_review=None,
    claims_supplement=None,
    aspects=None,
) -> dict:
    computed = compute(
        dataset_dir, passages, papers, retrieval_run, live, live_review,
        claims_supplement, aspects,
    )  # fmt: skip
    results, inputs = computed["results"], computed["inputs"]
    manifest = {
        "schema": SCHEMA,
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "git": {
            "head": _git("rev-parse", "HEAD"),
            "dirty_paths": len((_git("status", "--porcelain") or "").splitlines()),
        },
        "code_sha256": {p: sha256_file(REPO / p) for p in CODE if (REPO / p).exists()},
        "python": platform.python_version(),
        "inputs": inputs,
        "retrieval_run_provenance": computed["run"]["document"].get("provenance"),
        "label_provenance": computed["dataset"]["provenance"],
        "settings": results["settings"],
        "mode": {"offline": True, "mocked": False, "live_calls": 0, "llm_judge": False},
        "results_sha256": _digest(results),
        "warnings": results["warnings"],
    }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(
        json.dumps(results, indent=1, sort_keys=True), encoding="utf-8"
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8"
    )
    return {**results, "manifest": manifest}


def reproduce(manifest_path: Path) -> dict:
    """Recompute a recorded run from its recorded inputs and compare."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    inputs = manifest["inputs"]
    current = {}
    for key, path in (
        *(
            (f"dataset/{n}", Path(inputs["dataset_dir"]) / n)
            for n in ("questions.csv", "labels.csv", "claims.csv")
        ),
        ("catalogue/passages.csv", Path(inputs["passages"])),
        ("catalogue/papers.csv", Path(inputs["papers"])),
        ("retrieval_run", Path(inputs["retrieval_run"])),
        *(
            (
                (
                    "claims_supplement/claims.csv",
                    Path(inputs["claims_supplement"]) / "claims.csv",
                ),
            )
            if inputs.get("claims_supplement")
            else ()
        ),
        *((("aspects", Path(inputs["aspects"])),) if inputs.get("aspects") else ()),
    ):
        current[key] = sha256_file(path) if path.exists() else None
    mismatched = [
        f"{key}: recorded {inputs['sha256'].get(key)} now {value}"
        for key, value in current.items()
        if inputs["sha256"].get(key) != value
    ]
    try:
        recomputed = compute(
            inputs["dataset_dir"], inputs["passages"], inputs["papers"],
            inputs["retrieval_run"], inputs["live"], inputs["live_review"],
            inputs.get("claims_supplement"), inputs.get("aspects"),
        )  # fmt: skip
        digest = _digest(recomputed["results"])
        error = None
    except (DatasetError, OSError, KeyError) as exc:
        digest, error = None, str(exc)
    code_now = {
        p: sha256_file(REPO / p) for p in manifest["code_sha256"] if (REPO / p).exists()
    }
    return {
        "reproduced": not mismatched and digest == manifest["results_sha256"],
        "input_mismatches": mismatched,
        "results_sha256": {
            "recorded": manifest["results_sha256"],
            "recomputed": digest,
        },
        "code_changed": sorted(
            p for p, h in manifest["code_sha256"].items() if code_now.get(p) != h
        ),
        "error": error,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 13 Mode B evaluation (offline)")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--passages", type=Path, default=DEFAULT_PASSAGES)
    parser.add_argument("--papers", type=Path, default=DEFAULT_PAPERS)
    parser.add_argument("--retrieval-run", type=Path)
    parser.add_argument("--live", type=Path, nargs="*", default=[])
    parser.add_argument("--live-review", type=Path)
    parser.add_argument("--claims-supplement", type=Path)
    parser.add_argument("--aspects", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--reproduce", type=Path)
    args = parser.parse_args(argv)
    if args.reproduce:
        outcome = reproduce(args.reproduce)
        print(json.dumps(outcome, indent=1))
        return 0 if outcome["reproduced"] else 1
    if not args.dataset:
        parser.error("--dataset is required")
    if args.validate_only:
        data = load_dataset(args.dataset, load_catalogue(args.passages, args.papers))
        counts = {
            label: sum(v["label"] == label for v in data["labels"].values())
            for label in LABELS
        }
        print(json.dumps({"valid": True, "questions": len(data["labels"]), "labels": counts,
                          "claims": len(data["claims"]), "sha256": data["hashes"]}, indent=1))  # fmt: skip
        return 0
    if not (args.retrieval_run and args.output_dir and args.run_id):
        parser.error("--retrieval-run, --output-dir and --run-id are required")
    result = evaluate(
        args.dataset, args.passages, args.papers, args.retrieval_run,
        args.output_dir, args.run_id, args.live, args.live_review,
        args.claims_supplement, args.aspects,
    )  # fmt: skip
    print(json.dumps({"run_id": args.run_id, "results_sha256": result["manifest"]["results_sha256"],
                      "warnings": result["warnings"]}, indent=1))  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
