"""Which retrieved passages one Mode A report is allowed to rest on (D-09-04).

Stage 9 used to hand the LLM whatever Qdrant ranked highest. For the 20-paper
corpus's top gap that was seven passages from one paper and one from a paper
outside the cluster entirely, while five of the six papers that actually
mentioned the theme went unquoted. The report that came back read as the
field's position on prompt injection; it was one study's experiment.

Nothing here retrieves or re-ranks — similarity is still Qdrant's judgement and
the pipeline's. This module only decides which of the ranked passages survive
into a single report, under two rules:

* **a per-paper cap**, so no one study can be the whole evidence base; and
* **a preference for the cluster's own papers**, because those are the papers
  the pipeline found the theme in. It is a preference and not a filter: a
  passage from outside the cluster is still real retrieved evidence, and a thin
  cluster needs it.

Both are applied breadth-first — every paper's best passage before any paper's
second — so a tight `limit` spends itself on distinct papers rather than on one
paper's top-N. Selection is a pure function of the ranked list, so the same
retrieval always yields the same report evidence.
"""

from collections.abc import Iterable


def _paper_id(item: dict) -> int | None:
    value = item.get("paper_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def select_evidence(
    evidence: list[dict],
    cluster_paper_ids: Iterable[int],
    limit: int,
    per_paper_cap: int,
) -> list[dict]:
    """The passages one report may cite, in retrieval order.

    `evidence` is the projected, ranked list — best first. `cluster_paper_ids`
    are the papers Stage 6 grouped into this cluster.

    Returned in retrieval order rather than selection order, matching what
    `service._grounded_evidence` already promises: the model's or this
    function's ordering carries no information the pipeline produced, while
    Qdrant's is by similarity.

    A passage whose `paper_id` is unusable is dropped. It cannot be capped,
    preferred or counted, and an uncountable passage in a report whose whole
    point is "how many papers say this" is worse than one fewer passage.
    """
    if limit <= 0 or per_paper_cap <= 0:
        raise ValueError(
            f"limit and per_paper_cap must be positive, got {limit} and "
            f"{per_paper_cap}"
        )

    members = set(cluster_paper_ids)

    # Retrieval rank by object identity, so the result can be put back in
    # retrieval order at the end. `evidence.index(item)` would be O(n^2) and
    # would compare dicts by value, resolving two byte-identical passages to
    # the same position.
    rank = {id(item): position for position, item in enumerate(evidence)}

    # `paper_id -> its passages, best first`. Python dicts keep insertion
    # order, so papers are already ordered by their best passage's rank and no
    # sort key has to reconstruct it.
    by_paper: dict[int, list[dict]] = {}
    for item in evidence:
        paper_id = _paper_id(item)
        if paper_id is None:
            continue
        by_paper.setdefault(paper_id, []).append(item)

    # Cluster papers first, each group still in best-passage order. `sorted` is
    # stable, so this is a partition and not a re-ranking.
    papers = sorted(by_paper, key=lambda paper_id: paper_id not in members)

    selected: list[dict] = []
    for position in range(per_paper_cap):
        for paper_id in papers:
            passages = by_paper[paper_id]
            if position < len(passages):
                selected.append(passages[position])
                if len(selected) == limit:
                    return sorted(selected, key=lambda item: rank[id(item)])
    return sorted(selected, key=lambda item: rank[id(item)])


def evidence_summary(evidence: list[dict]) -> dict:
    """How much evidence, and from how many papers.

    The two are reported separately because a report that cites eight passages
    from one paper and a report that cites eight papers are not equally
    supported, and only the second number says which one this is. Stage 9 puts
    it in the prompt so the model can weigh its own confidence against it, and
    `GET /reports` serves it so the dashboard never has to infer it.
    """
    counts: dict[int, int] = {}
    for item in evidence:
        paper_id = _paper_id(item)
        if paper_id is not None:
            counts[paper_id] = counts.get(paper_id, 0) + 1
    return {
        "passage_count": sum(counts.values()),
        "unique_paper_count": len(counts),
        "max_passages_from_one_paper": max(counts.values(), default=0),
    }
