"""Phase 6 data contract (Stage 6).

The input to this phase is whatever `EvidenceIndex.gap_points()` returns: one
dict per indexed `limitation` / `future_work` statement, carrying its stored
SPECTER2 vector alongside the Phase 5 payload. That payload originates in PDF
text, so the boundary between "a dict out of Qdrant" and "a statement Stage 6
reasons about" is validated **here**, once, rather than at each call site.

Nothing in this module imports numpy, scikit-learn or transformers: it is the
piece the storage layer and the API shape depend on, and it must stay cheap to
import.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

# The Phase 5 `source_type` values Stage 6 clusters. Kept in step with
# `app.embeddings.schema.GAP_SOURCE_TYPES`, but asserted independently: if
# `gap_points()`' filter ever broke, clustering whole paragraphs would swamp
# every real limitation theme.
GAP_SOURCE_TYPES = frozenset({"limitation", "future_work"})


def _clean_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text, got {value!r}")
    collapsed = " ".join(value.split())
    if not collapsed:
        raise ValueError(f"{label} must be non-empty text")
    return collapsed


@dataclass(frozen=True, slots=True)
class GapStatement:
    """One limitation/future-work statement with the vector it was indexed as.

    Field order is positional and is what `from_point` produces:
    `point_id, paper_id, text, year, source_type, vector`.
    """

    point_id: str
    paper_id: int
    text: str
    year: int | None
    source_type: str
    vector: tuple[float, ...]

    @classmethod
    def from_point(cls, point: dict) -> "GapStatement":
        point_id = _clean_text(point.get("id"), "point_id")
        text = _clean_text(point.get("text"), "text")

        paper_id = point.get("paper_id")
        if not isinstance(paper_id, int) or isinstance(paper_id, bool):
            raise TypeError(f"paper_id must be an int, got {paper_id!r}")

        source_type = point.get("source_type")
        if source_type not in GAP_SOURCE_TYPES:
            raise ValueError(
                f"unknown source_type {source_type!r}; Stage 6 clusters "
                f"{sorted(GAP_SOURCE_TYPES)} only"
            )

        raw_vector = point.get("vector")
        if not isinstance(raw_vector, Sequence) or isinstance(raw_vector, str | bytes):
            raise TypeError(f"vector must be a sequence of floats, got {raw_vector!r}")
        vector = tuple(float(value) for value in raw_vector)
        if not vector:
            raise ValueError(f"point {point_id!r} has an empty vector")
        if not all(math.isfinite(value) for value in vector):
            raise ValueError(f"point {point_id!r} has a non-finite vector")

        # A year that is not an int is metadata noise, not a reason to drop a
        # statement — Phase 7 already handles undated members.
        year = point.get("year")
        if isinstance(year, bool) or not isinstance(year, int):
            year = None

        return cls(point_id, paper_id, text, year, source_type, vector)


@dataclass(frozen=True, slots=True)
class ContradictionPair:
    """A cross-paper disagreement found inside one cluster (CLUSTER-02).

    Positional: `left_point_id, left_paper_id, right_point_id, right_paper_id,
    score`.
    """

    left_point_id: str
    left_paper_id: int
    right_point_id: str
    right_paper_id: int
    score: float

    def __post_init__(self) -> None:
        if self.left_point_id == self.right_point_id:
            raise ValueError("a contradiction needs two distinct statements")
        if self.left_paper_id == self.right_paper_id:
            # D-06-04: a paper disagreeing with itself is an extraction
            # artefact, not the between-papers signal CLUSTER-02 describes.
            raise ValueError("a contradiction must be between different papers")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [0, 1], got {self.score}")


@dataclass(frozen=True, slots=True)
class StatementCluster:
    """A discovered theme: its members, its label, and any disagreement in it."""

    label: str | None
    members: tuple[GapStatement, ...]
    contradictions: tuple[ContradictionPair, ...] = ()

    def __post_init__(self) -> None:
        # Callers build these from lists; freezing them here keeps the cluster
        # hashable and stops a later stage mutating a stored result in place.
        object.__setattr__(self, "members", tuple(self.members))
        object.__setattr__(self, "contradictions", tuple(self.contradictions))
        if not self.members:
            raise ValueError("a cluster needs at least one member")

    @property
    def member_count(self) -> int:
        """Distinct statements: a sentence stored under two labels counts once."""
        return len({(member.paper_id, member.text) for member in self.members})

    @property
    def statements(self) -> tuple[GapStatement, ...]:
        """One row per distinct statement, the smallest point id representing it."""
        seen: dict[tuple[int, str], GapStatement] = {}
        for member in sorted(self.members, key=lambda item: item.point_id):
            seen.setdefault((member.paper_id, member.text), member)
        return tuple(seen.values())

    @property
    def paper_ids(self) -> tuple[int, ...]:
        """Distinct contributing papers, in first-seen order."""
        return tuple(dict.fromkeys(member.paper_id for member in self.members))

    @property
    def contradiction_flag(self) -> bool:
        return bool(self.contradictions)

    @property
    def contradiction_score(self) -> float:
        """The strongest disagreement in the cluster; 0.0 when there is none.

        Phase 8 multiplies this by `w5`, so an unflagged cluster must score a
        clean zero rather than something merely small.
        """
        if not self.contradictions:
            return 0.0
        return max(pair.score for pair in self.contradictions)
