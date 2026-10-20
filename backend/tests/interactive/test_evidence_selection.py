"""Generation sees only evidence that passed the sufficiency gate (D-B-03).

`assess` judged sufficiency on the qualifying vector passages, but `rerank`
then handed the model every retrieved row, sub-threshold passages included,
plus graph rows for papers whose only passages failed the gate. The model
could cite evidence the gate had never accepted. At the production threshold
0.2 nothing on this corpus is below threshold, so the defect was latent. It
appears as soon as the threshold means anything.
"""

from app.interactive.workflow import (
    assess_sufficiency,
    qualifying_evidence,
    run_query,
)
from tests.interactive.test_workflow import (
    FakeGraphExpander,
    FakeRetriever,
    ScriptedLLM,
    _answer,
    _point,
    _services,
)

OK = {"grounded": True, "unsupported_claim_indexes": []}


def _graph_row(paper_id: int, entity: str, seed: int | None = None) -> dict:
    # The shape `expand_paper_neighborhood` returns: `paper_id` is the seed or
    # a one-hop neighbour sharing an entity with it; `seed_paper_id` the seed.
    return {
        "paper_id": paper_id,
        "seed_paper_id": paper_id if seed is None else seed,
        "title": f"Paper {paper_id}",
        "entity": entity,
        "entity_type": "Dataset",
        "relation": "MENTIONS",
    }


def _query(retriever, llm, graph=None, threshold=0.9):
    return run_query(
        "Which systems fail outside English?",
        _services(retriever, llm, graph=graph),
        3,
        12,
        2,
        2,
        threshold,
        1,
    )


def test_a_sub_threshold_passage_never_reaches_the_answer_prompt():
    rows = [
        _point("strong-1", 1, score=0.95),
        _point("strong-2", 2, score=0.94),
        _point("weak-3", 3, text="Unrelated cooking advice.", score=0.40),
    ]
    llm = ScriptedLLM(answer=[_answer("strong-1", "strong-2")], verify=[OK])
    result = _query(FakeRetriever([rows]), llm)
    prompt = llm.prompts["answer"][0]
    assert "weak-3" not in prompt and "cooking" not in prompt
    assert {c["id"] for c in result["citations"]} == {"strong-1", "strong-2"}


def test_a_citation_of_a_sub_threshold_passage_is_an_invalid_citation():
    rows = [
        _point("strong-1", 1, score=0.95),
        _point("strong-2", 2, score=0.94),
        _point("weak-3", 3, score=0.40),
    ]
    cites_weak = _answer("weak-3")
    llm = ScriptedLLM(answer=[cites_weak, cites_weak])
    result = _query(FakeRetriever([rows]), llm)
    assert result["outcome"] == "grounding_failed"
    assert "invalid_citations=1" in result["trace"][4]
    assert "verify" not in llm.prompts


def test_graph_rows_are_kept_only_for_papers_that_passed_the_gate():
    rows = [
        _point("strong-1", 1, score=0.95),
        _point("strong-2", 2, score=0.94),
        _point("weak-3", 3, score=0.40),
    ]
    graph = FakeGraphExpander([_graph_row(1, "XLPID"), _graph_row(3, "HarmBench")])
    llm = ScriptedLLM(answer=[_answer("strong-1", "strong-2")], verify=[OK])
    _query(FakeRetriever([rows]), llm, graph)
    prompt = llm.prompts["answer"][0]
    assert "XLPID" in prompt and "HarmBench" not in prompt


def test_a_one_hop_neighbour_is_kept_when_its_seed_passed_the_gate():
    rows = [
        _point("strong-1", 1, score=0.95),
        _point("strong-2", 2, score=0.94),
        _point("weak-3", 3, score=0.40),
    ]
    graph = FakeGraphExpander(
        [
            _graph_row(10, "Neighbour via 1", seed=1),
            _graph_row(11, "Neighbour via 3", seed=3),
        ]
    )
    llm = ScriptedLLM(answer=[_answer("strong-1", "strong-2")], verify=[OK])
    _query(FakeRetriever([rows]), llm, graph)
    prompt = llm.prompts["answer"][0]
    assert "Neighbour via 1" in prompt and "Neighbour via 3" not in prompt


def test_a_neighbour_reached_from_two_seeds_survives_if_either_passed():
    rows = [
        _point("strong-1", 1, score=0.95),
        _point("strong-2", 2, score=0.94),
        _point("weak-3", 3, score=0.40),
    ]
    shared = "Shared entity"
    graph = FakeGraphExpander(
        [_graph_row(10, shared, seed=3), _graph_row(10, shared, seed=2)]
    )
    llm = ScriptedLLM(answer=[_answer("strong-1", "strong-2")], verify=[OK])
    _query(FakeRetriever([rows]), llm, graph)
    assert shared in llm.prompts["answer"][0]


def test_one_sentence_stored_twice_is_offered_once():
    text = "Existing systems fail outside English."
    rows = [
        _point("lim-1", 1, text=text, score=0.95),
        _point("fw-1", 1, text=text, score=0.95),  # same sentence, second type
        _point("strong-2", 2, score=0.94),
    ]
    llm = ScriptedLLM(answer=[_answer("lim-1", "strong-2")], verify=[OK])
    _query(FakeRetriever([rows]), llm)
    prompt = llm.prompts["answer"][0]
    assert prompt.count(text) == 2  # lim-1 and strong-2, never fw-1
    assert "fw-1" not in prompt


def test_at_the_production_threshold_every_real_passage_is_still_offered():
    rows = [
        _point(f"p{i}", i, text=f"Systems fail outside English {i}.", score=0.75)
        for i in range(1, 6)
    ]
    llm = ScriptedLLM(answer=[_answer("p1", "p2")], verify=[OK])
    _query(FakeRetriever([rows]), llm, threshold=0.2)
    prompt = llm.prompts["answer"][0]
    assert all(f"p{i}" in prompt for i in range(1, 6))


def test_a_claim_repeated_in_the_draft_is_shown_once():
    rows = [_point("strong-1", 1, score=0.95), _point("strong-2", 2, score=0.94)]
    claim = {
        "text": "Existing systems fail outside English.",
        "evidence_ids": ["strong-1"],
    }
    repeat = {
        "text": "existing systems  fail outside English.",
        "evidence_ids": ["strong-2"],
    }
    llm = ScriptedLLM(answer=[{"answer": "x", "claims": [claim, repeat]}], verify=[OK])
    result = _query(FakeRetriever([rows]), llm)
    assert result["outcome"] == "grounded_answer"
    assert [c["text"] for c in result["claims"]] == [claim["text"]]
    assert result["answer"] == claim["text"]
    # The verifier saw the de-duplicated claim list, and only its citations remain.
    assert {c["id"] for c in result["citations"]} == {"strong-1"}


def test_the_gate_and_the_selection_share_one_qualification_rule():
    evidence = [
        {
            "id": "a",
            "paper_id": 1,
            "text": "x",
            "score": 0.95,
            "source_type": "limitation",
        },
        {
            "id": "b",
            "paper_id": 2,
            "text": "y",
            "score": 0.10,
            "source_type": "paragraph",
        },
        {
            "id": "c",
            "paper_id": 3,
            "text": "z",
            "score": float("nan"),
            "source_type": "paragraph",
        },
        {
            "id": "d",
            "paper_id": 4,
            "text": "w",
            "score": 1.7,
            "source_type": "paragraph",
        },
        {"id": "g", "paper_id": 1, "text": "e", "score": 0.5, "source_type": "graph"},
    ]
    selected = qualifying_evidence(evidence, 0.5)
    assert [item["id"] for item in selected] == ["a", "g"]
    verdict = assess_sufficiency(evidence, 0.5, 1, 1)
    assert verdict["relevant"] == len(
        [i for i in selected if i["source_type"] != "graph"]
    )
