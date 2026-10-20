"""Mode B weak retrieval, retry limits and refusals, end to end through the
compiled LangGraph workflow.

Deterministic: retrieval, graph expansion and the LLM are scripted fakes, so
these establish the workflow's control flow, not live model behaviour. Limits
come from the production `Settings` defaults, not from literals.
"""

import pytest

from app.core.config import Settings
from app.interactive.workflow import run_query
from tests.interactive.test_workflow import (
    FakeGraphExpander,
    FakeRetriever,
    ScriptedLLM,
    _answer,
    _point,
    _services,
)

DEFAULTS = {name: Settings.model_fields[name].default for name in Settings.model_fields}
THRESHOLD = DEFAULTS["retrieval_min_score"]
MAX_ROUNDS = DEFAULTS["retrieval_round_cap"]
MAX_REGENERATIONS = DEFAULTS["grounding_regeneration_cap"]
GROUNDED = {"grounded": True, "unsupported_claim_indexes": []}
REJECTED = {"grounded": False, "unsupported_claim_indexes": [0]}


def _query(services, **overrides):
    """`run_query` exactly as `POST /query` calls it, production defaults."""
    options = [
        DEFAULTS["retrieval_round_cap"],
        DEFAULTS["retrieval_top_n"],
        DEFAULTS["retrieval_min_chunks"],
        DEFAULTS["retrieval_min_papers"],
        DEFAULTS["retrieval_min_score"],
        DEFAULTS["grounding_regeneration_cap"],
    ]
    names = [
        "max_rounds",
        "retrieval_limit",
        "min_relevant_chunks",
        "min_distinct_papers",
        "min_relevance_score",
        "max_regenerations",
    ]
    kwargs = dict(zip(names, options, strict=True))
    kwargs.update(overrides)
    return run_query("Which languages were defences evaluated on?", services, **kwargs)


def test_the_production_limits_are_the_ones_these_tests_assume():
    assert (THRESHOLD, MAX_ROUNDS, MAX_REGENERATIONS) == (0.2, 3, 1)
    assert DEFAULTS["retrieval_min_chunks"] == 2
    assert DEFAULTS["retrieval_min_papers"] == 2


GRAPH_ROWS = [
    {"paper_id": 8, "entity": "prompt injection", "entity_type": "task"},
    {"paper_id": 9, "entity": "Bangla", "entity_type": "domain"},
    {"paper_id": 10, "entity": "jailbreak", "entity_type": "task"},
]

# name -> (round-1 vector hits, graph rows). Each is insufficient at 0.2 / 2 / 2.
WEAK = {
    "zero_passages": ([], []),
    "one_qualifying_passage": ([_point("w1", 8)], []),
    "qualifying_passages_from_one_paper": (
        [_point("w1", 8), _point("w2", 8), _point("w3", 8)],
        [],
    ),
    "scores_below_threshold": (
        [_point("w1", 8, score=THRESHOLD - 1e-6), _point("w2", 9, score=0.05)],
        [],
    ),
    "graph_rows_beside_sub_threshold_hits": (
        [_point("w1", 8, score=0.1), _point("w2", 9, score=0.1)],
        GRAPH_ROWS,
    ),
    "graph_rows_beside_one_qualifying_passage": ([_point("w1", 8)], GRAPH_ROWS),
}
STRONG = [_point("s1", 8), _point("s2", 9)]


@pytest.mark.parametrize("case", sorted(WEAK))
def test_weak_retrieval_reformulates_and_retries(case):
    hits, graph_rows = WEAK[case]
    retriever = FakeRetriever([hits, STRONG])
    llm = ScriptedLLM(
        reformulate=[{"query": "multilingual prompt injection defence languages"}],
        answer=[_answer("s1", "s2")],
        verify=[GROUNDED],
    )

    result = _query(_services(retriever, llm, graph=FakeGraphExpander(graph_rows)))

    assert result["trace"][1].startswith("assess:sufficient=False")
    assert len(llm.prompts["reformulate"]) == 1
    assert [q for q, _ in retriever.queries][
        1
    ] == "multilingual prompt injection defence languages"
    assert result["retrieval_rounds"] == 2 and result["retry_count"] == 1
    assert result["grounded"] is True
    assert result["outcome"] == "grounded_answer"


@pytest.mark.parametrize("case", sorted(WEAK))
def test_persistent_weak_retrieval_ends_in_the_safe_insufficient_result(case):
    hits, graph_rows = WEAK[case]
    retriever = FakeRetriever([hits])
    llm = ScriptedLLM(reformulate=[{"query": "retry one"}, {"query": "retry two"}])

    result = _query(_services(retriever, llm, graph=FakeGraphExpander(graph_rows)))

    assert result["retrieval_rounds"] == MAX_ROUNDS
    assert len(llm.prompts["reformulate"]) == MAX_ROUNDS - 1
    # Weak retrieval never reaches generation or grounding: a separate path.
    assert "answer" not in llm.prompts and "verify" not in llm.prompts
    assert result["answer"].startswith("Insufficient relevant evidence")
    assert result["claims"] == [] and result["grounded"] is False
    assert result["outcome"] == "insufficient_evidence"
    assert result["trace"][-1] == "insufficient"


def test_two_qualifying_passages_from_two_papers_take_the_normal_path():
    retriever = FakeRetriever([STRONG])
    llm = ScriptedLLM(answer=[_answer("s1", "s2")], verify=[GROUNDED])

    result = _query(_services(retriever, llm, graph=FakeGraphExpander(GRAPH_ROWS)))

    assert result["trace"][1].startswith("assess:sufficient=True;relevant=2;papers=2;")
    assert "reformulate" not in llm.prompts
    assert result["retrieval_rounds"] == 1 and result["regeneration_count"] == 0
    assert result["grounded"] is True and result["outcome"] == "grounded_answer"
    # A grounded answer keeps exactly the evidence its claims cite.
    assert [c["id"] for c in result["citations"]] == ["s1", "s2"]


def test_graph_rows_alone_never_make_retrieval_sufficient():
    # Every row the graph can return, at any threshold down to zero.
    retriever = FakeRetriever(
        [[_point("w1", 8, score=-0.5), _point("w2", 9, score=-0.5)]]
    )
    llm = ScriptedLLM(reformulate=[{"query": "retry one"}, {"query": "retry two"}])

    result = _query(
        _services(retriever, llm, graph=FakeGraphExpander(GRAPH_ROWS * 10)),
        min_relevance_score=0.0,
    )

    assert all(
        "relevant=0;papers=0" in t for t in result["trace"] if t.startswith("assess:")
    )
    assert result["outcome"] == "insufficient_evidence"


def test_an_empty_index_is_told_apart_from_weak_retrieval():
    llm = ScriptedLLM()
    result = _query(_services(FakeRetriever([[]], has_evidence=False), llm))
    assert result["outcome"] == "no_indexed_evidence"
    assert result["retrieval_rounds"] == 1 and llm.prompts == {}


def test_configured_limits_cannot_be_raised_past_the_hard_caps():
    retriever = FakeRetriever([[_point("w1", 8)]])
    llm = ScriptedLLM(reformulate=[{"query": f"retry {i}"} for i in range(10)])
    result = _query(_services(retriever, llm), max_rounds=99)
    assert result["retrieval_rounds"] == 3 and len(retriever.queries) == 3

    llm = ScriptedLLM(answer=[_answer("s1", "s2")] * 5, verify=[REJECTED] * 5)
    result = _query(_services(FakeRetriever([STRONG]), llm), max_regenerations=99)
    assert result["regeneration_count"] == 1
    assert len(llm.prompts["answer"]) == 2 and len(llm.prompts["verify"]) == 2


@pytest.mark.parametrize(
    ("drafts", "verdicts"),
    [
        # Semantic: real citations, the verifier rejects both drafts.
        (
            [_answer("s1", "s2", text="Draft one"), _answer("s1", text="Draft two")],
            [REJECTED, REJECTED],
        ),
        # Structural: invented citations, never sent to the verifier.
        (
            [
                _answer("made-up", text="Draft one"),
                _answer("also-made-up", text="Draft two"),
            ],
            [],
        ),
    ],
    ids=["verifier_rejects", "invented_citations"],
)
def test_grounding_failure_after_regeneration_is_a_safe_refusal(drafts, verdicts):
    llm = ScriptedLLM(answer=drafts, verify=verdicts)

    result = _query(_services(FakeRetriever([STRONG]), llm))

    # Retrieval was sufficient: this is the grounding path, not weak retrieval.
    assert result["trace"][1].startswith("assess:sufficient=True")
    assert "reformulate" not in llm.prompts
    assert result["regeneration_count"] == MAX_REGENERATIONS
    assert result["grounded"] is False and result["claims"] == []
    assert "Draft" not in result["answer"]
    assert result["answer"].startswith("A grounded answer could not be produced")
    assert result["outcome"] == "grounding_failed"
    # No answer exists, so nothing is cited in support of one.
    assert result["citations"] == []


def test_a_draft_that_passes_on_regeneration_keeps_its_citations():
    llm = ScriptedLLM(
        answer=[_answer("s1", "s2", text="Draft one"), _answer("s2", text="Draft two")],
        verify=[REJECTED, GROUNDED],
    )
    result = _query(_services(FakeRetriever([STRONG]), llm))
    assert result["grounded"] is True and result["regeneration_count"] == 1
    # The accepted draft's checked claims are the answer; its free prose
    # ("Draft two") was never checked against a passage, so it is not shown.
    assert result["answer"] == "Existing systems fail outside English."
    assert [c["id"] for c in result["citations"]] == ["s2"]
