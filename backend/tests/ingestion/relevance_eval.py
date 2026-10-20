"""Offline evaluation of the S1 relevance gate on the labelled regression set.

`data/s1_relevance_regression.json` holds the papers the 2026-10-10 S1 audit
labelled by hand, with the cross-encoder score the production model gives each
one. `ReplayScorer` hands those recorded scores back, so the evaluation runs
the real gate logic without loading the model.

Metrics (INSUFFICIENT_EVIDENCE items are excluded from all three and reported
on their own):

* precision_strict  -- admitted items labelled CORE / admitted items;
* precision_lenient -- admitted items labelled CORE or PARTIAL / admitted items;
* recall_core       -- admitted CORE items / CORE items;
* ranking_pairs     -- among admitted items, the share of (CORE, non-CORE)
  pairs the admission ranking puts CORE first. This is what decides which
  candidates a full source cap keeps.

Run ``python -m tests.ingestion.relevance_eval`` from ``backend/`` to print
the report.
"""

import json
from collections.abc import Callable
from pathlib import Path

from app.ingestion.relevance import (
    RelevanceDecision,
    RelevanceGate,
    analyze_topic,
    load_lexicon,
    rank_key,
)

DATASET_PATH = Path(__file__).with_name("data") / "s1_relevance_regression.json"
EXCLUDED = "INSUFFICIENT_EVIDENCE"


def load_dataset(path: Path = DATASET_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _collapse(text: str | None) -> str:
    return " ".join((text or "").split())


class ReplayScorer:
    """The recorded cross-encoder score for each dataset item's text."""

    def __init__(self, items: list[dict]) -> None:
        self._by_title = {
            _collapse(item["title"]): item["cross_encoder_score"] for item in items
        }

    def score(self, query: str, texts: list[str]) -> list[float]:
        scores = []
        for text in texts:
            matches = [
                score
                for title, score in self._by_title.items()
                if text == title or text.startswith(f"{title}. ")
            ]
            if len(matches) != 1:
                raise KeyError(f"No recorded score for: {text[:80]!r}")
            scores.append(matches[0])
        return scores


def build_gate(dataset: dict, floor: float = -1.0, strong: float = 3.5):
    return RelevanceGate(
        analyze_topic(dataset["topic"], load_lexicon()),
        ReplayScorer(dataset["items"]),
        semantic_floor=floor,
        strong_threshold=strong,
    )


def evaluate(
    gate,
    dataset: dict,
    ranking: Callable[[RelevanceDecision], tuple] = rank_key,
) -> dict:
    items = dataset["items"]
    decisions = gate.evaluate_many(
        [(item["title"], item["abstract"]) for item in items]
    )
    rows = [
        {
            "id": item["id"],
            "label": item["label"],
            "admitted": decision.admitted,
            "reason_code": decision.reason_code,
            "tier": decision.tier,
            "score": decision.score,
            "rank": ranking(decision) if decision.admitted else None,
        }
        for item, decision in zip(items, decisions, strict=True)
    ]
    judged = [row for row in rows if row["label"] != EXCLUDED]
    admitted = [row for row in judged if row["admitted"]]
    core = [row for row in judged if row["label"] == "CORE"]

    def share(part: int, whole: int) -> float | None:
        return round(part / whole, 3) if whole else None

    admitted_core = [row for row in admitted if row["label"] == "CORE"]
    admitted_other = [row for row in admitted if row["label"] != "CORE"]
    pairs = [(c, o) for c in admitted_core for o in admitted_other]
    return {
        "items": len(rows),
        "admitted": len(admitted),
        "precision_strict": share(len(admitted_core), len(admitted)),
        "precision_lenient": share(
            sum(row["label"] in ("CORE", "PARTIAL") for row in admitted),
            len(admitted),
        ),
        "recall_core": share(len(admitted_core), len(core)),
        "ranking_pairs": share(
            sum(c["rank"] > o["rank"] for c, o in pairs), len(pairs)
        ),
        "false_admissions": [
            (row["id"], row["label"], row["reason_code"]) for row in admitted_other
        ],
        "missed_core": [
            (row["id"], row["reason_code"]) for row in core if not row["admitted"]
        ],
        "insufficient_evidence": [
            (row["id"], row["admitted"], row["reason_code"], row["tier"])
            for row in rows
            if row["label"] == EXCLUDED
        ],
        "rows": rows,
    }


def _print(report: dict) -> None:
    for row in report["rows"]:
        print(
            f"{row['id']:<28} {row['label']:<22} "
            f"{'ADMIT' if row['admitted'] else 'reject':<7} {row['reason_code']}"
        )
    print()
    for key in (
        "items",
        "admitted",
        "precision_strict",
        "precision_lenient",
        "recall_core",
        "ranking_pairs",
        "false_admissions",
        "missed_core",
        "insufficient_evidence",
    ):
        print(f"{key}: {report[key]}")


if __name__ == "__main__":
    dataset = load_dataset()
    _print(evaluate(build_gate(dataset), dataset))
