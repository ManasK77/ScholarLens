from types import SimpleNamespace

from app.interactive.workflow import InteractiveServices, default_services, run_query


class FakeRetriever:
    def __init__(self):
        self.queries = []

    def retrieve(self, query, limit):
        self.queries.append(query)
        if len(self.queries) == 1:
            return []
        return [
            {
                "id": "evidence-1",
                "text": "Existing systems fail on low-resource languages.",
                "paper_id": 8,
                "score": 0.9,
            }
        ]


class FakeLLM:
    provider = "fake"
    model = "fake-model"

    def generate_json(self, prompt, schema):
        if "Reformulate" in prompt:
            return {"query": "multilingual low-resource limitations"}
        if "Verify whether" in prompt:
            return {"grounded": True, "unsupported_claim_indexes": []}
        return {
            "answer": "Low-resource languages remain under-supported [1].",
            "claims": [
                {
                    "text": "Low-resource languages remain under-supported.",
                    "evidence_ids": ["evidence-1"],
                }
            ],
        }


def test_graph_retries_weak_retrieval_and_returns_grounded_claims():
    retriever = FakeRetriever()
    services = InteractiveServices(
        retriever=retriever, graph_search=lambda _: [], llm=FakeLLM()
    )

    result = run_query("What gaps exist?", services, max_rounds=3, retrieval_limit=5)

    assert result["retry_count"] == 1
    assert result["citations"][0]["paper_id"] == 8
    assert result["grounded"] is True


class EmptyRetriever:
    def retrieve(self, query, limit):
        return []

    def has_evidence(self):
        return False


class RecordingLLM(FakeLLM):
    def __init__(self):
        self.prompts = []

    def generate_json(self, prompt, schema):
        self.prompts.append(prompt)
        return super().generate_json(prompt, schema)


def test_empty_retrieval_returns_explicit_insufficient_result_without_generation():
    llm = RecordingLLM()
    services = InteractiveServices(
        retriever=EmptyRetriever(), graph_search=lambda _: [], llm=llm
    )

    result = run_query("What gaps exist?", services, max_rounds=3)

    assert result["answer"] == (
        "No indexed evidence was found for this question. "
        "Collect papers and run the evidence pipeline before querying."
    )
    assert result["citations"] == []
    assert result["claims"] == []
    assert result["grounded"] is False
    assert result["retry_count"] == 0
    assert result["trace"][-1] == "insufficient"
    assert llm.prompts == []


def test_default_services_does_not_load_reranker_until_evidence_needs_ranking(
    monkeypatch,
):
    index = EmptyRetriever()
    llm = RecordingLLM()
    monkeypatch.setattr("app.interactive.workflow.EvidenceIndex", lambda _: index)
    monkeypatch.setattr("app.interactive.workflow.get_llm_client", lambda _: llm)

    def fail_if_loaded():
        raise AssertionError(
            "reranker should stay unloaded during service construction"
        )

    monkeypatch.setattr("app.interactive.workflow._default_reranker", fail_if_loaded)

    services = default_services(SimpleNamespace(retrieval_top_n=12))

    assert services.retriever is index
