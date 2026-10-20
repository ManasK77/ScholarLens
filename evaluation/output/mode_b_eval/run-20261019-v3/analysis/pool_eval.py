"""Offline replay of the candidate-pool change on the frozen evaluation set.

Reads only saved files: the dataset, `cosine-top100.json` (the production
Qdrant cosine top-100 per question) and `cross-encoder-scores-top100.json` (the
production cross-encoder's logit for each of those passages). For each pool
depth, the twelve passages the model would be offered are the pool's twelve
best by cross-encoder score (depth 12 is today's behaviour: the cosine top 12).

    PYTHONPATH=. backend/.venv/Scripts/python.exe \
        evaluation/output/mode_b_eval/run-20261019-v3/analysis/pool_eval.py

Labels are agent-authored and the expected-passage lists are not exhaustive, so
every figure is a lower bound on evidence reached, not a measure of answer
quality. Splits use the production hash split (`calibration.split_tune_test`).
"""

import collections
import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO / "backend"))
from app.interactive.calibration import split_tune_test  # noqa: E402
from app.interactive.intent import question_scope  # noqa: E402

HERE = Path(__file__).resolve().parent
DEPTHS = (12, 24, 36, 48, 100)


def _csv(path):
    with open(path, encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


questions = {
    r["question_id"]: r["question"]
    for r in _csv(REPO / "evaluation/datasets/mode_b_v1/questions.csv")
}
labels = {
    r["question_id"]: r for r in _csv(REPO / "evaluation/datasets/mode_b_v1/labels.csv")
}
cosine = json.loads((HERE / "cosine-top100.json").read_text("utf-8"))
rerank = json.loads((HERE / "cross-encoder-scores-top100.json").read_text("utf-8"))
answerable = sorted(
    q for q, r in labels.items() if r["label"] in ("relevant", "partially_relevant")
)
calibration, held_out = split_tune_test(answerable)
scope = {q: question_scope(questions[q]) for q in answerable}


def needed(q):
    return {int(x) for x in labels[q]["relevant_paper_ids"].split(";") if x}


def expected(q):
    return {x for x in labels[q]["relevant_passage_ids"].split(";") if x}


def shown(q, depth):
    pool = cosine[q][:depth]
    if depth == 12:
        return pool[:12]
    return sorted(pool, key=lambda h: -rerank[q][h[0]])[:12]


def measure(ids, depth):
    papers_hit = papers_total = passages_hit = passages_total = reached = 0
    for q in ids:
        chosen = shown(q, depth)
        got_papers = {h[1] for h in chosen}
        got_passages = {h[0] for h in chosen}
        if len(needed(q)) >= 2:
            papers_total += len(needed(q))
            papers_hit += len(needed(q) & got_papers)
        if expected(q):
            passages_total += len(expected(q))
            passages_hit += len(expected(q) & got_passages)
            reached += bool(expected(q) & got_passages)
    return {
        "required_papers": [papers_hit, papers_total],
        "expected_passages": [passages_hit, passages_total],
        "questions_reaching_an_expected_passage": [reached, len(ids)],
    }


groups = {
    "all_answerable": answerable,
    "needs_two_or_more_papers": [q for q in answerable if len(needed(q)) >= 2],
}
result = {
    "note": "Agent-authored labels; expected-passage lists are not exhaustive.",
    "scope_counts_answerable": dict(collections.Counter(scope.values())),
    "by_pool_depth": {
        name: {
            part: {str(d): measure([q for q in ids if q in members], d) for d in DEPTHS}
            for part, members in (
                ("calibration", set(calibration)),
                ("held_out", set(held_out)),
                ("all", set(answerable)),
            )
        }
        for name, ids in groups.items()
    },
}
change = collections.Counter()
for q in answerable:
    if expected(q):
        before = len(expected(q) & {h[0] for h in shown(q, 12)})
        after = len(expected(q) & {h[0] for h in shown(q, 36)})
        change["more" if after > before else "fewer" if after < before else "same"] += 1
result["questions_changed_at_depth_36"] = dict(change)
(HERE / "pool-analysis.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
print(json.dumps(result["questions_changed_at_depth_36"]))
