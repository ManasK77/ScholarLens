"""The hand-labelled Stage 6 expectation, written before any clustering code.

README §15 Phase 6 and ROADMAP Phase 6 both require a concrete expected
outcome first: *a small hand-labelled set of limitation sentences that should
cluster together*. This module is that set, and it is the single source of
truth for both levels of the Phase 6 clustering tests:

* the fast unit tests, which drive `cluster_statements` with the deterministic
  synthetic vectors built here — they check the clustering *wiring* (no fixed
  cluster count, members grouped by theme, stable ordering);
* the `@pytest.mark.integration` test, which embeds `.text` with the real
  SPECTER2 encoder and asserts the *same* grouping — that is the check that the
  semantics actually hold.

Three themes, four statements each, every statement from a different paper
except where a shared paper is needed to exercise the same-paper rule. Two
statements are a deliberate cross-paper contradiction (`CONTRADICTION_PAIR`).

**Noise rejection (D-06-07) is deliberately *not* tested here.** Whether
HDBSCAN calls a given point an outlier is a density judgement, and density is
exactly what a twelve-point set does not have; asserting it on this set would
be asserting a small-sample artefact. `test_cluster.py` proves it separately on
geometry built for that question.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class LabelledStatement:
    point_id: str
    paper_id: int
    text: str
    year: int
    theme: str


LABELLED_STATEMENTS: tuple[LabelledStatement, ...] = (
    LabelledStatement(
        "p-ml-1",
        101,
        "Our evaluation is limited to English, and accuracy degrades sharply "
        "on low-resource languages.",
        2023,
        "multilingual",
    ),
    LabelledStatement(
        "p-ml-2",
        102,
        "The model inherits a strong English bias and transfers poorly to "
        "other languages.",
        2024,
        "multilingual",
    ),
    LabelledStatement(
        "p-ml-3",
        103,
        "Cross-lingual transfer does not recover accuracy for low-resource languages.",
        2024,
        "multilingual",
    ),
    LabelledStatement(
        "p-ml-4",
        104,
        "Cross-lingual transfer fully recovers accuracy for low-resource languages.",
        2025,
        "multilingual",
    ),
    LabelledStatement(
        "p-hl-1",
        201,
        "The system still hallucinates factual claims that are not supported "
        "by the retrieved context.",
        2023,
        "hallucination",
    ),
    LabelledStatement(
        "p-hl-2",
        202,
        "Generated summaries contain unfaithful statements that no source "
        "document entails.",
        2024,
        "hallucination",
    ),
    LabelledStatement(
        "p-hl-3",
        203,
        "Future work should reduce hallucination by grounding every generated "
        "claim in cited evidence.",
        2025,
        "hallucination",
    ),
    LabelledStatement(
        "p-hl-4",
        203,
        "Factual consistency of the generated text remains unreliable across domains.",
        2025,
        "hallucination",
    ),
    LabelledStatement(
        "p-cc-1",
        301,
        "Training the model requires substantial GPU resources that many "
        "research groups cannot afford.",
        2022,
        "compute",
    ),
    LabelledStatement(
        "p-cc-2",
        302,
        "Inference latency and memory footprint make deployment on commodity "
        "hardware impractical.",
        2024,
        "compute",
    ),
    LabelledStatement(
        "p-cc-3",
        303,
        "The computational cost of pretraining limits how often the model can "
        "be updated.",
        2024,
        "compute",
    ),
    LabelledStatement(
        "p-cc-4",
        304,
        "Future work should explore distillation to reduce the compute budget "
        "required at inference time.",
        2025,
        "compute",
    ),
)

THEMES: tuple[str, ...] = ("multilingual", "hallucination", "compute")

# The cross-paper disagreement Stage 6 must flag: paper 103 says cross-lingual
# transfer does not recover accuracy, paper 104 says it fully does.
CONTRADICTION_PAIR: tuple[str, str] = ("p-ml-3", "p-ml-4")


def statements_for_theme(theme: str) -> tuple[LabelledStatement, ...]:
    return tuple(item for item in LABELLED_STATEMENTS if item.theme == theme)


# --- Deterministic synthetic vectors (unit tests only) ----------------------
#
# One near-orthogonal direction per theme in an 8-d space, plus a small
# reproducible per-statement perturbation so members are close but not
# identical, plus the large shared component every statement carries.
#
# That shared component is not decoration. Real SPECTER2 `[CLS]` vectors are
# strongly anisotropic — measured on this very set, raw within-theme cosine is
# 0.931 against 0.871 across themes, i.e. barely separable. A synthetic fixture
# of clean orthogonal directions would let a clustering implementation pass
# without ever confronting that, so `_CONE_WEIGHT` reproduces the cone here and
# `center_vectors` is what removes it (D-06-08).
#
# Real semantics are the integration test's job.

_THEME_AXIS: dict[str, int] = {"multilingual": 0, "hallucination": 1, "compute": 2}
_VECTOR_DIMENSION = 8
_JITTER = 0.05
_CONE_AXIS = 7
_CONE_WEIGHT = 3.0


def synthetic_vector(statement: LabelledStatement, position: int) -> list[float]:
    """A unit vector: the shared cone, plus the statement's theme direction."""
    vector = [0.0] * _VECTOR_DIMENSION
    vector[_CONE_AXIS] = _CONE_WEIGHT
    axis = _THEME_AXIS[statement.theme]
    vector[axis] = 1.0
    vector[(axis + 1) % 3] = _JITTER * (position + 1)
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def synthetic_gap_points() -> list[dict]:
    """`LABELLED_STATEMENTS` in the exact shape `EvidenceIndex.gap_points()` returns."""
    points = []
    for position, statement in enumerate(LABELLED_STATEMENTS):
        points.append(
            {
                "id": statement.point_id,
                "vector": synthetic_vector(statement, position),
                "paper_id": statement.paper_id,
                "text": statement.text,
                "source_type": "future_work"
                if statement.text.startswith("Future work")
                else "limitation",
                "fact_id": position + 1,
                "year": statement.year,
                "venue": "ACL",
                "section_type": "limitations",
                "graph_node_id": f"paper-{statement.paper_id}",
            }
        )
    return points
