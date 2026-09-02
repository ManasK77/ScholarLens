from app.interactive.workflow import InteractiveServices, run_query


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
