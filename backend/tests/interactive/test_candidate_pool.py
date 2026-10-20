"""A wider candidate pool for the reranker, and the paper-10 miss of live LV-Q2.

Question p035 ("Which papers report that LLM safety or instruction compliance
is weaker for non-English prompts?", live LV-Q2) was answered from papers 3 and
1; paper 10, which states the finding too, was never retrieved. Measured with
the production encoder and Qdrant (read-only, 2026-10-19):

* the labelled paper-10 passages rank 270 and 397 of 632 by SPECTER2 cosine
  (0.880 and 0.866 against 0.953 for the best), so no threshold, pool depth or
  rerank can reach them: the embedding does not relate "safety weaker for
  non-English" to a passage about English versions not jailbreaking;
* the best paper-10 passages that are reachable (ranks 29 and 30) are about
  the study's method, and the cross-encoder scores them far below the twelve it
  keeps (-10.1 and -11.1 against -4.2): correctly left out.

That miss is a recall limit of the embedding, not a defect of the gate. What is
fixable is a different, general one. The pipeline asked Qdrant for exactly the
twelve passages it would show, so the cross-encoder only reordered them. With a
pool three times as large, the same cross-encoder picks the twelve (the
retrieve-then-rerank design of README section 9). Replayed on the 69 answerable
evaluation questions (agent-authored labels): 30 -> 40 questions get a labelled
passage into the twelve (held-out half 18 -> 24 of 41), required papers 31 ->
32 of 42. Deeper pools (4x, 8x) kept raising passage recall but lowered the
held-out required-paper recall (24 -> 22 -> 20 of 32), so the factor stays 3.

The sufficiency gate and the graph expansion still see only the first
`retrieval_limit` hits, so the gate calibrated on twelve hits is unchanged.

`fixtures/mode_b_p035_pool.json` holds the production top-36 cosine pool for
p035 with each passage's measured cross-encoder score.
"""

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.interactive.workflow import run_query
from tests.interactive.test_workflow import (
    FakeGraphExpander,
    FakeRetriever,
    ScriptedLLM,
    _services,
)

CASE = json.loads(
    (
        Path(__file__).resolve().parent.parent / "fixtures" / "mode_b_p035_pool.json"
    ).read_text(encoding="utf-8")
)
POOL = CASE["pool"]
RECORDED = {p["id"]: p["rerank_score"] for p in POOL}
EXPECTED = set(CASE["expected_passage_ids"])
OK = {"grounded": True, "unsupported_claim_indexes": []}
REQUIRED_ENV = {
    "NEO4J_URI": "bolt://neo4j:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "test_neo4j_pw",
    "QDRANT_URL": "http://qdrant:6333",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_USER": "scholarlens",
    "POSTGRES_PASSWORD": "test_pg_pw",
    "POSTGRES_DB": "scholarlens",
    "PDF_STORAGE_PATH": "/data/pdfs",
    "MAX_UPLOAD_SIZE_MB": "50",
    "UNPAYWALL_EMAIL": "you@example.com",
    "GROBID_URL": "http://grobid:8070",
}


def _recorded_reranker(question, evidence):
    """The production cross-encoder's recorded scores, best first."""
    ranked = [
        {**item, "rerank_score": RECORDED.get(item["id"], -99.0)} for item in evidence
    ]
    return sorted(ranked, key=lambda item: item["rerank_score"], reverse=True)


def _hit(point_id, paper_id, score, text="Existing systems fail outside English."):
    return {
        "id": point_id,
        "paper_id": paper_id,
        "text": text,
        "score": score,
        "source_type": "paragraph",
        "title": f"Paper {paper_id}",
    }


def _offered(llm):
    """Evidence ids the answer prompt offered the model."""
    prompt = llm.prompts["answer"][0]
    block = prompt.split("UNTRUSTED_RETRIEVED_EVIDENCE_BEGIN\n", 1)[1]
    block = block.split("\nUNTRUSTED_RETRIEVED_EVIDENCE_END", 1)[0]
    return [json.loads(line)["id"] for line in block.splitlines()]


def _run(retriever, llm, *, pool_factor, reranker=_recorded_reranker, limit=12):
    return run_query(
        CASE["question"],
        _services(retriever, llm, graph=FakeGraphExpander(), reranker=reranker),
        3,
        limit,
        2,
        2,
        0.2,
        1,
        retrieval_pool_factor=pool_factor,
    )


def _claim_for(item):
    return {
        "answer": "x",
        "claims": [{"text": item["text"][:80], "evidence_ids": [item["id"]]}],
    }


def test_the_retriever_is_asked_for_the_pool_not_just_the_shown_passages():
    retriever = FakeRetriever([POOL])
    llm = ScriptedLLM(answer=[_claim_for(POOL[0])], verify=[OK])

    _run(retriever, llm, pool_factor=3)

    assert retriever.queries[0][1] == 36


def test_the_default_asks_for_exactly_the_shown_passages():
    retriever = FakeRetriever([POOL[:12]])
    llm = ScriptedLLM(answer=[_claim_for(POOL[0])], verify=[OK])

    _run(retriever, llm, pool_factor=1)

    assert retriever.queries[0][1] == 12


def test_the_reranker_chooses_the_twelve_from_the_whole_pool():
    llm = ScriptedLLM(answer=[_claim_for(POOL[0])], verify=[OK])

    _run(FakeRetriever([POOL]), llm, pool_factor=3)

    offered = _offered(llm)
    assert len(offered) == 12
    cosine_top12 = {p["id"] for p in POOL[:12]}
    pool_only = [i for i in offered if i not in cosine_top12]
    # Passages below the cosine top twelve entered because the reranker ranks
    # them above passages that were in it.
    assert pool_only, "the reranker never saw anything beyond the first twelve"
    by_id = {p["id"]: p for p in POOL}
    assert all(by_id[i]["cosine_rank"] > 12 for i in pool_only)
    chosen = sorted((by_id[i]["rerank_score"] for i in offered), reverse=True)
    left_out = max(p["rerank_score"] for p in POOL if p["id"] not in set(offered))
    assert min(chosen) >= left_out


def test_the_expected_evidence_of_the_p035_question_is_still_offered():
    llm = ScriptedLLM(answer=[_claim_for(POOL[0])], verify=[OK])

    _run(FakeRetriever([POOL]), llm, pool_factor=3)

    offered = set(_offered(llm))
    in_pool = EXPECTED & {p["id"] for p in POOL}
    assert len(in_pool) == 3  # papers 1 and 3: the labelled ones paper 10 lacks
    assert in_pool <= offered


def test_the_gate_still_judges_only_the_first_twelve_hits():
    # Twelve hits from one paper then a second paper's passage: the gate needs
    # two papers among the first twelve, and the pool must not supply them.
    one_paper = [_hit(f"a{i}", 1, 0.95 - i * 0.001) for i in range(12)]
    other = [_hit(f"b{i}", 2, 0.80) for i in range(24)]
    llm = ScriptedLLM(
        answer=[], verify=[], reformulate=[{"query": "a"}, {"query": "b"}]
    )

    result = _run(FakeRetriever([one_paper + other]), llm, pool_factor=3, reranker=None)

    assert result["outcome"] == "insufficient_evidence"
    assessed = [t for t in result["trace"] if t.startswith("assess:")]
    assert all("papers=1" in t for t in assessed)


def test_without_a_reranker_the_pool_changes_nothing():
    llm = ScriptedLLM(answer=[_claim_for(POOL[0])], verify=[OK])

    _run(FakeRetriever([POOL]), llm, pool_factor=3, reranker=None)

    assert _offered(llm) == [p["id"] for p in POOL[:12]]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Open: paper 10's evidence for p035 (live LV-Q2) is not retrievable. Its "
        "labelled passages rank 270 and 397 of 632 by cosine, and the reachable "
        "paper-10 passages (ranks 29, 30) are method text the cross-encoder "
        "rightly rejects. Fixing it needs a different retriever or query, not a "
        "threshold, pool or rerank change."
    ),
)
def test_paper_10_evidence_reaches_the_model_for_the_p035_question():
    llm = ScriptedLLM(answer=[_claim_for(POOL[0])], verify=[OK])

    _run(FakeRetriever([POOL]), llm, pool_factor=3)

    by_id = {p["id"]: p for p in POOL}
    assert any(by_id[i]["paper_id"] == 10 for i in _offered(llm))


def test_the_setting_defaults_to_three_and_is_bounded(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    assert Settings(_env_file=None).retrieval_pool_factor == 3
    monkeypatch.setenv("RETRIEVAL_POOL_FACTOR", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
