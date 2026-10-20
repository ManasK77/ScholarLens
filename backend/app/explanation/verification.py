"""Which parts of a Mode A report are verified, and which are LLM judgment (D-09-13).

The four claims in a report's `justification` are the model's judgments; the
service already discards citations to passages that were never retrieved.
That made "conflicting evidence" look established whenever the model cited
two real passages -- whether or not they disagree. Here a conflict claim is
checked with the project's own NLI model (both directions, as in Stage 6),
and every claim records its `basis`:

* `llm_cited`     -- the model's judgment; its citations are real passages.
* `not_supported` -- the model did not assert it, or cited nothing real.
* `nli_verified`  -- (conflict only) two cited passages from different papers
                     contradict each other in both NLI directions.
* `llm_unverified`-- (conflict only) the model asserted a conflict the NLI
                     check did not confirm; kept for audit, `supported` false.
* `not_claimed`   -- (conflict only) the model did not assert one.

No LLM call: verification only re-reads passages the report already cites.
"""

from collections.abc import Callable, Sequence
from itertools import combinations

from sqlalchemy.orm import Session

from app.explanation.storage import list_reports

CONFLICT = "conflicting_evidence"
METHOD = "nli_both_directions"

# `(premise, hypothesis)` pairs -> P(contradiction) for each.
ScorePairs = Callable[[list[tuple[str, str]]], Sequence[float]]
Verifier = Callable[[list[dict]], dict]


def conflict_verification(
    cited: list[dict], score_pairs: ScorePairs, threshold: float
) -> dict:
    """Whether any two cited passages from different papers contradict."""
    pairs = [
        (left, right)
        for left, right in combinations(cited, 2)
        if left.get("paper_id") != right.get("paper_id")
    ]
    if not pairs:
        return {
            "method": METHOD,
            "threshold": threshold,
            "pairs_checked": 0,
            "max_score": None,
            "verified": False,
        }
    inputs = []
    for left, right in pairs:
        inputs.append((left["text"], right["text"]))
        inputs.append((right["text"], left["text"]))
    scores = list(score_pairs(inputs))
    agreed = [min(scores[2 * i], scores[2 * i + 1]) for i in range(len(pairs))]
    best = max(agreed)
    return {
        "method": METHOD,
        "threshold": threshold,
        "pairs_checked": len(pairs),
        "max_score": round(best, 4),
        "verified": best >= threshold,
    }


def nli_conflict_verifier(detector=None) -> Verifier:
    """The production verifier. The NLI model loads only when first needed."""

    def verify(cited: list[dict]) -> dict:
        nonlocal detector
        if detector is None:
            from app.clustering.nli import get_detector

            detector = get_detector()
        return conflict_verification(cited, detector.score_pairs, detector.threshold)

    return verify


def annotate_justification(
    justification: dict, evidence: list[dict], verifier: Verifier
) -> dict:
    """The justification with every claim's `basis`, the conflict verified."""
    annotated = {}
    for name, claim in justification.items():
        claim = dict(claim)
        if name != CONFLICT:
            claim["basis"] = "llm_cited" if claim.get("supported") else "not_supported"
        annotated[name] = claim
    conflict = annotated.get(CONFLICT)
    if conflict is None:
        return annotated
    asserted = bool(conflict.get("supported"))
    conflict["llm_supported"] = asserted
    if not asserted:
        conflict["basis"] = "not_claimed"
        return annotated
    by_id = {item["id"]: item for item in evidence}
    cited = [by_id[i] for i in conflict.get("evidence_ids") or [] if i in by_id]
    verification = verifier(cited)
    conflict["verification"] = verification
    conflict["supported"] = bool(verification.get("verified"))
    conflict["basis"] = "nli_verified" if conflict["supported"] else "llm_unverified"
    return annotated


def verify_stored_reports(session: Session, verifier: Verifier) -> list[int]:
    """Annotate stored reports written before D-09-13. Idempotent; no LLM call."""
    updated = []
    for row in list_reports(session):
        justification = row.justification or {}
        if not justification or "llm_supported" in justification.get(CONFLICT, {}):
            continue
        row.justification = annotate_justification(
            justification, list(row.evidence or []), verifier
        )
        updated.append(row.cluster_id)
    session.flush()
    return updated
