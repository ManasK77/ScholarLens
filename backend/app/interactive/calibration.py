"""Read-only calibration of Mode B's retrieval sufficiency threshold.

`RETRIEVAL_MIN_SCORE` is compared against the vector store's score, which is a
Qdrant cosine similarity (`Distance.COSINE`, `EvidenceIndex.retrieve` passes
`point.score` through untouched): higher is more similar, range [-1, 1].
SPECTER2 vectors are strongly anisotropic, so raw cosines sit far above 0.

Two modes, both read-only (Qdrant `scroll` / `query_points` only; nothing is
written to Qdrant, Neo4j or PostgreSQL, and no setting is changed):

* `stored` (default) -- no model is loaded. Every stored vector is used as a
  proxy query ("a question phrased like a corpus statement"). This measures
  the score scale and which thresholds would reject *in-corpus* queries, i.e.
  a false-reject bound. It cannot say anything about unrelated questions,
  because no stored vector is unrelated to the corpus.
* `embed` -- encodes the labelled synthetic queries in `QUERIES` with the
  production encoder (SPECTER2 base + proximity adapter, loaded locally from
  the model cache) and retrieves them exactly as Mode B does. Refuses to run
  without `--allow-model-load`. No LLM is called in either mode.

Run inside the backend container (the image need not be rebuilt):

    docker exec -i nlp-backend-1 python - [--mode embed --allow-model-load] \
        < backend/app/interactive/calibration.py > calibration.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from statistics import median

# Thresholds swept in the report. 0.2 is the current production default.
SWEEP = (0.2, 0.5, 0.6, 0.7, 0.75, 0.8, 0.82, 0.84, 0.86, 0.88, 0.9, 0.92, 0.95)

# SYNTHETIC queries, written for this calibration from the corpus's own topic
# (20 papers on multilingual prompt injection / LLM security) and the gap
# clusters. Category labels are the author's expectation, not expert-validated
# ground truth.
QUERIES: tuple[dict, ...] = (
    {
        "category": "relevant",
        "text": "Which languages have multilingual prompt injection defenses been evaluated on?",
    },
    {
        "category": "relevant",
        "text": "How well do prompt injection detectors generalize to low-resource languages such as Bangla?",
    },
    {
        "category": "relevant",
        "text": "What limitations do papers report about indirect prompt injection attacks on LLM agents?",
    },
    {
        "category": "relevant",
        "text": "Do adversarial attacks bypass prompt injection defenses in large language models?",
    },
    {"category": "partial", "text": "How are jailbreak attacks on chatbots evaluated?"},
    {
        "category": "partial",
        "text": "What are the limitations of machine translation with large language models?",
    },
    {
        "category": "partial",
        "text": "How is retrieval-augmented generation secured in enterprise chatbots?",
    },
    {
        "category": "weak",
        "text": "What datasets are used to benchmark text classification?",
    },
    {"category": "weak", "text": "How do transformer models handle long documents?"},
    {
        "category": "weak",
        "text": "What are common evaluation metrics for natural language generation?",
    },
    {
        "category": "unrelated",
        "text": "How do diffusion models predict protein structure?",
    },
    {
        "category": "unrelated",
        "text": "What methods forecast crop yield from satellite imagery?",
    },
    {
        "category": "unrelated",
        "text": "How does lithium battery degradation depend on temperature?",
    },
    {
        "category": "unrelated",
        "text": "Which reinforcement learning methods control quadruped robots?",
    },
)


def percentiles(values: Sequence[float]) -> dict:
    """min / p10 / p25 / median / p75 / p90 / max of `values`, rounded."""
    data = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not data:
        return {"n": 0}

    def at(q: float) -> float:
        return round(data[min(len(data) - 1, int(q * (len(data) - 1) + 0.5))], 4)

    return {
        "n": len(data),
        "min": round(data[0], 4),
        "p10": at(0.10),
        "p25": at(0.25),
        "median": round(median(data), 4),
        "p75": at(0.75),
        "p90": at(0.90),
        "max": round(data[-1], 4),
    }


def sufficiency_sweep(
    hits_per_query: Sequence[Sequence[dict]],
    thresholds: Sequence[float],
    min_chunks: int,
    min_papers: int,
) -> dict[str, float]:
    """Fraction of queries Mode B's rule would call sufficient, per threshold."""
    from app.interactive.workflow import assess_sufficiency

    if not hits_per_query:
        return {f"{t:g}": 0.0 for t in thresholds}
    return {
        f"{t:g}": round(
            sum(
                assess_sufficiency(list(hits), t, min_chunks, min_papers)["sufficient"]
                for hits in hits_per_query
            )
            / len(hits_per_query),
            4,
        )
        for t in thresholds
    }


def separating_range(by_category: dict[str, list[float]]) -> dict:
    """The thresholds that accept every relevant query and reject every unrelated one.

    Each value is a query's *k*-th best score (k = the chunks Mode B needs).
    Returns `{"low": ..., "high": ...}` -- a threshold `t` with
    `low < t <= high` separates the two groups -- or `{"separable": False}`.
    """
    relevant = by_category.get("relevant") or []
    unrelated = by_category.get("unrelated") or []
    if not relevant or not unrelated:
        return {"separable": False, "reason": "needs relevant and unrelated queries"}
    low, high = max(unrelated), min(relevant)
    if low >= high:
        return {"separable": False, "low": round(low, 4), "high": round(high, 4)}
    return {"separable": True, "low": round(low, 4), "high": round(high, 4)}


def leave_one_out(by_category: dict[str, list[float]]) -> dict:
    """Hold each relevant/unrelated query out, pick a threshold from the rest.

    The threshold is the midpoint of the remaining queries' separating range.
    A held-out query is classified correctly when it lands on its own side.
    With a handful of queries this measures how fragile a threshold is; it is
    not an estimate of production accuracy.
    """
    labelled = [("relevant", s) for s in by_category.get("relevant") or []] + [
        ("unrelated", s) for s in by_category.get("unrelated") or []
    ]
    results = []
    for position, (category, score) in enumerate(labelled):
        rest: dict[str, list[float]] = {"relevant": [], "unrelated": []}
        for other, (c, s) in enumerate(labelled):
            if other != position:
                rest[c].append(s)
        split = separating_range(rest)
        if not split.get("separable"):
            results.append(
                {
                    "category": category,
                    "score": score,
                    "threshold": None,
                    "correct": False,
                }
            )
            continue
        threshold = (split["low"] + split["high"]) / 2
        accepted = score >= threshold
        results.append(
            {
                "category": category,
                "score": round(score, 4),
                "threshold": round(threshold, 4),
                "correct": accepted == (category == "relevant"),
            }
        )
    return {
        "held_out": len(results),
        "correct": sum(r["correct"] for r in results),
        "thresholds": percentiles(
            [r["threshold"] for r in results if r["threshold"] is not None]
        ),
        "results": results,
    }


def analyze_saved(report: dict, thresholds: Sequence[float] = SWEEP) -> dict:
    """Re-analyse a saved `--mode embed` report. No model, no store access."""
    by_category: dict[str, list[float]] = {}
    for row in report["queries"]:
        if row.get("kth_best") is not None:
            by_category.setdefault(row["category"], []).append(row["kth_best"])
    acceptance = {
        category: {f"{t:g}": sum(score >= t for score in scores) for t in thresholds}
        for category, scores in by_category.items()
    }
    return {
        "mode": "from_file",
        "queries": {c: len(v) for c, v in by_category.items()},
        "kth_best_by_category": {c: percentiles(v) for c, v in by_category.items()},
        "accepted_by_threshold": acceptance,
        "relevant_vs_unrelated": separating_range(by_category),
        "leave_one_out": leave_one_out(by_category),
        "label_status": report.get("label_status"),
    }


# --- independent-label evaluation (PROTOCOL.md) ---------------------------------

LABELS = ("relevant", "partially_relevant", "weak", "unrelated")


def cohen_kappa(first: Sequence[str], second: Sequence[str]) -> float:
    """Agreement between two labellers beyond what chance would give."""
    if len(first) != len(second) or not first:
        raise ValueError("kappa needs two equally long, non-empty label lists")
    n = len(first)
    observed = sum(a == b for a, b in zip(first, second, strict=True)) / n
    expected = sum(
        (first.count(label) / n) * (second.count(label) / n)
        for label in set(first) | set(second)
    )
    return 1.0 if expected == 1.0 else round((observed - expected) / (1 - expected), 4)


def split_tune_test(question_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """Deterministic split by a hash of the id: same ids, same halves, any order."""
    import hashlib

    tune, test = [], []
    for qid in sorted(set(question_ids)):
        (tune if hashlib.sha256(qid.encode()).digest()[0] % 2 == 0 else test).append(
            qid
        )
    return tune, test


def _accepted(hits, threshold, min_chunks, min_papers) -> bool:
    from app.interactive.workflow import assess_sufficiency

    return assess_sufficiency(list(hits), threshold, min_chunks, min_papers)[
        "sufficient"
    ]


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def label_metrics(
    labels: dict[str, dict],
    run: dict[str, list[dict]],
    threshold: float,
    min_chunks: int,
    min_papers: int,
    question_ids: Sequence[str] | None = None,
) -> dict:
    """Round-1 retrieval quality against independent labels at one threshold.

    `labels[qid]` = {label, relevant_paper_ids, relevant_passage_ids};
    `run[qid]` = the top-k hits Mode B retrieved for that question. Partial and
    weak questions are reported, not scored as errors: whether they should be
    answered is a product decision the labels alone do not settle.
    """
    ids = [q for q in (question_ids or labels) if q in labels and q in run]
    accepted = {q: _accepted(run[q], threshold, min_chunks, min_papers) for q in ids}
    by_label: dict[str, list[str]] = {}
    for q in ids:
        by_label.setdefault(labels[q]["label"], []).append(q)
    paper_recall, passage_recall = [], []
    for q in ids:
        hits = run[q]
        papers = {h["paper_id"] for h in hits if h.get("source_type") != "graph"}
        if labels[q]["relevant_paper_ids"]:
            wanted = labels[q]["relevant_paper_ids"]
            paper_recall.append(len(wanted & papers) / len(wanted))
        if labels[q]["relevant_passage_ids"]:
            wanted = labels[q]["relevant_passage_ids"]
            passage_recall.append(len(wanted & {h["id"] for h in hits}) / len(wanted))
    return {
        "threshold": threshold,
        "counts": {
            label: len(by_label[label]) for label in LABELS if label in by_label
        },
        "false_reject_rate": _rate(
            [not accepted[q] for q in by_label.get("relevant", [])]
        ),
        "false_accept_rate": _rate(
            [accepted[q] for q in by_label.get("unrelated", [])]
        ),
        "accepted_fraction_by_label": {
            label: _rate([accepted[q] for q in by_label[label]])
            for label in ("partially_relevant", "weak")
            if label in by_label
        },
        "retry_frequency": _rate([not accepted[q] for q in ids]),
        "paper_recall_at_k": round(sum(paper_recall) / len(paper_recall), 4)
        if paper_recall
        else None,
        "passage_recall_at_k": round(sum(passage_recall) / len(passage_recall), 4)
        if passage_recall
        else None,
        "mean_distinct_papers_in_top_k": round(
            sum(len({h["paper_id"] for h in run[q]}) for q in ids) / len(ids), 4
        )
        if ids
        else None,
    }


def choose_threshold(
    labels, run, tune_ids, candidates, min_chunks, min_papers
) -> float | None:
    """The candidate with the best (1 - FAR) + (1 - FRR) on the tune half.

    Ties go to the lowest threshold. `None` when the tune half lacks either
    relevant or unrelated questions -- nothing can be chosen from it.
    """
    best, best_score = None, -1.0
    for threshold in sorted(candidates):
        m = label_metrics(labels, run, threshold, min_chunks, min_papers, tune_ids)
        if m["false_accept_rate"] is None or m["false_reject_rate"] is None:
            return None
        score = (1 - m["false_accept_rate"]) + (1 - m["false_reject_rate"])
        if score > best_score:
            best, best_score = threshold, score
    return best


def evaluate_labels(
    labels, run, candidates, min_chunks, min_papers, *, current, min_per_label=25
) -> dict:
    """Choose on the tune half, report on the test half only."""
    ids = [q for q in labels if q in run]
    tune, test = split_tune_test(ids)
    chosen = choose_threshold(labels, run, tune, candidates, min_chunks, min_papers)
    totals = {label: sum(labels[q]["label"] == label for q in ids) for label in LABELS}
    return {
        "chosen_on": "tune",
        "reported_on": "test",
        "split": {"tune": len(tune), "test": len(test)},
        "label_totals": totals,
        "sample_sufficient": min(totals["relevant"], totals["unrelated"])
        >= min_per_label,
        "chosen_threshold": chosen,
        "test_at_chosen": label_metrics(
            labels, run, chosen, min_chunks, min_papers, test
        )
        if chosen is not None
        else None,
        "current_threshold": label_metrics(
            labels, run, current, min_chunks, min_papers, test
        ),
    }


def load_adjudicated(path: str) -> dict[str, dict]:
    """`adjudicated-labels.csv` -> labels. Unknown labels fail; blank rows skip."""
    import csv

    def ids(value: str) -> list[str]:
        return [part.strip() for part in (value or "").split(";") if part.strip()]

    labels = {}
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            label = (row.get("final_label") or "").strip()
            if not label:
                continue
            if label not in LABELS:
                raise ValueError(f"{row.get('question_id')}: unknown label {label!r}")
            labels[row["question_id"].strip()] = {
                "label": label,
                "relevant_paper_ids": {
                    int(p) for p in ids(row.get("relevant_paper_ids"))
                },
                "relevant_passage_ids": set(ids(row.get("relevant_passage_ids"))),
            }
    return labels


def _kth(hits: Sequence[dict], k: int) -> float | None:
    scores = sorted(
        (h["score"] for h in hits if h.get("source_type") != "graph"), reverse=True
    )
    return scores[k - 1] if len(scores) >= k else None


def _hit(point) -> dict:
    payload = point.payload or {}
    return {
        "id": str(point.id),
        "paper_id": payload.get("paper_id"),
        "source_type": payload.get("source_type"),
        "score": float(point.score),
    }


def run_stored(client, settings) -> dict:
    """Stored vectors as proxy queries. Read-only; no model."""
    import numpy as np

    points, offset = [], None
    while True:
        page, offset = client.scroll(
            settings.qdrant_collection,
            limit=256,
            offset=offset,
            with_vectors=True,
            with_payload=["paper_id", "source_type"],
        )
        points.extend(page)
        if offset is None:
            break
    points.sort(key=lambda p: str(p.id))
    matrix = np.asarray([p.vector for p in points], dtype=np.float64)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    cosine = matrix @ matrix.T
    off_diagonal = cosine[~np.eye(len(points), dtype=bool)]

    top_n = settings.retrieval_top_n
    hits_per_query, kth = [], []
    for point in points:
        result = client.query_points(
            settings.qdrant_collection,
            query=point.vector,
            limit=top_n + 1,
            with_payload=["paper_id", "source_type"],
        )
        hits = [_hit(h) for h in result.points if str(h.id) != str(point.id)][:top_n]
        hits_per_query.append(hits)
        value = _kth(hits, settings.retrieval_min_chunks)
        if value is not None:
            kth.append(value)

    return {
        "mode": "stored",
        "proxy": "each stored vector used as a query, itself excluded",
        "points": len(points),
        "all_pairs_cosine": percentiles(off_diagonal.tolist()),
        "nearest_neighbour_cosine": percentiles(
            [h[0]["score"] for h in hits_per_query if h]
        ),
        f"top{top_n}_cosine": percentiles(
            [h[-1]["score"] for h in hits_per_query if h]
        ),
        f"kth_best_cosine_k{settings.retrieval_min_chunks}": percentiles(kth),
        "sufficient_fraction_by_threshold": sufficiency_sweep(
            hits_per_query,
            SWEEP,
            settings.retrieval_min_chunks,
            settings.retrieval_min_papers,
        ),
    }


def load_questions(path: str) -> list[dict]:
    """`independent-questions.csv` -> [{question_id, text}]. No labels: those
    live in a separate file the run never sees."""
    import csv

    with open(path, encoding="utf-8", newline="") as handle:
        rows = [
            {"question_id": r["question_id"].strip(), "text": r["question"].strip()}
            for r in csv.DictReader(handle)
            if (r.get("question") or "").strip()
        ]
    if len({r["question_id"] for r in rows}) != len(rows):
        raise ValueError("question_id values must be unique")
    return rows


def run_embed(client, settings, questions: list[dict] | None = None) -> dict:
    """Encode questions and retrieve exactly as Mode B's first round does.

    Default: the 14 synthetic `QUERIES` (ids q001-q014, matching the template).
    With `questions`: an independent set, embedded without any label.
    """
    from app.embeddings.encoder import get_encoder

    independent = questions is not None
    queries = questions or [
        {"question_id": f"q{i:03d}", **q} for i, q in enumerate(QUERIES, 1)
    ]
    encoder = get_encoder()  # loads SPECTER2 + adapter from the local cache
    top_n = settings.retrieval_top_n
    vectors = encoder.encode([q["text"] for q in queries], len(queries))
    rows, by_category = [], {}
    for query, vector in zip(queries, vectors, strict=True):
        result = client.query_points(
            settings.qdrant_collection,
            query=vector,
            limit=top_n,
            with_payload=["paper_id", "source_type"],
        )
        hits = [_hit(h) for h in result.points]
        kth = _kth(hits, settings.retrieval_min_chunks)
        if kth is not None and query.get("category"):
            by_category.setdefault(query["category"], []).append(kth)
        rows.append(
            {
                **query,
                "synthetic": not independent,
                # Every hit, so independent labels can be scored offline later.
                "hits": hits,
                "top1": round(hits[0]["score"], 4) if hits else None,
                f"top{top_n}": round(hits[-1]["score"], 4) if hits else None,
                "kth_best": round(kth, 4) if kth is not None else None,
                "papers_in_top": len({h["paper_id"] for h in hits}),
                # Source papers of the best hits, so a threshold can be checked
                # per paper and not only on aggregate scores.
                "top_hits": [
                    {k: h[k] for k in ("paper_id", "source_type", "score")}
                    for h in hits[:5]
                ],
                "sufficient_at": sufficiency_sweep(
                    [hits],
                    SWEEP,
                    settings.retrieval_min_chunks,
                    settings.retrieval_min_papers,
                ),
            }
        )
    return {
        "mode": "embed",
        "queries": rows,
        "kth_best_by_category": {c: percentiles(v) for c, v in by_category.items()},
        "relevant_vs_unrelated": separating_range(by_category),
        # The tool cannot know who wrote a question file; independence is a
        # property of the dataset (its card), never asserted here.
        "label_status": "external question file; labels held separately, "
        "authorship and independence recorded by the dataset, not this run"
        if independent
        else "synthetic queries, author-assigned categories; not expert-validated",
    }


def _resolved(repo: str, revision: str | None) -> str | None:
    from app.core.model_revision import resolved_revision

    return resolved_revision(repo, revision)


def _query_embed_version(settings) -> str:
    from app.embeddings.indexer import EvidenceIndex

    return EvidenceIndex(settings).embedding_version()


def run_provenance(client, settings, questions_path: str | None) -> dict:
    """What produced an embed run, so a later reader can trust or reproduce it.

    Encoder repos and the cache commits they resolve to (local cache only, no
    network), a hash of the question file, and whether the query encoder's
    `embed_version` equals the one every stored vector was written with. When
    it does not, query and passage cosines are not comparable.
    """
    import hashlib
    from datetime import UTC, datetime

    from app.core.model_revision import split_revision

    stored, offset = set(), None
    while True:
        page, offset = client.scroll(
            settings.qdrant_collection,
            limit=256,
            offset=offset,
            with_payload=["embed_version"],
            with_vectors=False,
        )
        stored |= {(p.payload or {}).get("embed_version") for p in page}
        if offset is None:
            break
    adapter, adapter_revision = split_revision(settings.embedding_adapter)
    base_revision = getattr(settings, "embedding_model_revision", None)
    query_version = _query_embed_version(settings)
    questions_sha = None
    if questions_path:
        with open(questions_path, "rb") as handle:
            questions_sha = hashlib.sha256(handle.read()).hexdigest()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "embedding_model": settings.embedding_model,
        "embedding_model_revision": _resolved(settings.embedding_model, base_revision)
        or base_revision,
        "embedding_adapter": adapter,
        "embedding_adapter_revision": _resolved(adapter, adapter_revision)
        or adapter_revision,
        "query_embed_version": query_version,
        "stored_embed_versions": sorted(v for v in stored if v is not None),
        "query_matches_stored_vectors": stored == {query_version},
        "questions_sha256": questions_sha,
        "llm_calls": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("stored", "embed"), default="stored")
    parser.add_argument(
        "--from-file",
        help="re-analyse a saved --mode embed JSON report (no model, no store)",
    )
    parser.add_argument(
        "--questions",
        help="--mode embed: an independent question CSV instead of the 14 synthetic",
    )
    parser.add_argument(
        "--evaluate-labels",
        help="adjudicated label CSV; with --run, score offline (no model, no store)",
    )
    parser.add_argument("--run", help="a saved --mode embed JSON report")
    parser.add_argument(
        "--allow-model-load",
        action="store_true",
        help="required for --mode embed: loads SPECTER2 locally (no LLM call)",
    )
    args = parser.parse_args(argv)
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as handle:
            json.dump(analyze_saved(json.load(handle)), sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    if args.evaluate_labels:
        if not args.run:
            parser.error("--evaluate-labels needs --run")
        with open(args.run, encoding="utf-8") as handle:
            saved = json.load(handle)
        run = {row["question_id"]: row["hits"] for row in saved["queries"]}
        settings_used = saved.get("current_settings", {})
        report = evaluate_labels(
            load_adjudicated(args.evaluate_labels),
            run,
            SWEEP,
            settings_used.get("retrieval_min_chunks", 2),
            settings_used.get("retrieval_min_papers", 2),
            current=settings_used.get("retrieval_min_score", 0.2),
        )
        json.dump(report, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    if args.mode == "embed" and not args.allow_model_load:
        parser.error("--mode embed loads SPECTER2; pass --allow-model-load to confirm")

    from qdrant_client import QdrantClient

    from app.core.config import get_settings

    settings = get_settings()
    client = QdrantClient(url=settings.qdrant_url, timeout=60)
    report = (
        run_stored(client, settings)
        if args.mode == "stored"
        else run_embed(
            client, settings, load_questions(args.questions) if args.questions else None
        )
    )
    report["score"] = {
        "definition": "Qdrant cosine similarity (Distance.COSINE), passed through untransformed",
        "direction": "higher is more similar; relevant when score >= RETRIEVAL_MIN_SCORE",
        "graph_rows": "fixed 0.5, not a similarity; excluded from sufficiency",
    }
    report["current_settings"] = {
        "retrieval_min_score": settings.retrieval_min_score,
        "retrieval_min_chunks": settings.retrieval_min_chunks,
        "retrieval_min_papers": settings.retrieval_min_papers,
        "retrieval_top_n": settings.retrieval_top_n,
    }
    if args.mode == "embed":
        report["provenance"] = run_provenance(client, settings, args.questions)
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
