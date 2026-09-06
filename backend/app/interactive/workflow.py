"""Mode B GraphRAG implemented as a bounded LangGraph state machine."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.embeddings.indexer import EvidenceIndex
from app.explanation.llm import StructuredLLM, get_llm_client
from app.ner_re.graph import graph_session, query_graph


class Retriever(Protocol):
    def retrieve(self, query: str, limit: int) -> list[dict]: ...


class QueryDraft(BaseModel):
    query: str


class GroundedClaim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(min_length=1)


class AnswerDraft(BaseModel):
    answer: str
    claims: list[GroundedClaim]


class GroundingVerdict(BaseModel):
    grounded: bool
    unsupported_claim_indexes: list[int] = Field(default_factory=list)


class QueryState(TypedDict, total=False):
    question: str
    query: str
    evidence: list[dict]
    retry_count: int
    max_rounds: int
    retrieval_limit: int
    sufficient: bool
    corpus_empty: bool
    answer: str
    claims: list[dict]
    grounded: bool
    citations: list[dict]
    trace: list[str]


class CrossEncoderReranker:
    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name)

    def __call__(self, question: str, evidence: list[dict]) -> list[dict]:
        if not evidence:
            return []
        scores = self._model.predict(
            [(question, item.get("text", "")) for item in evidence]
        )
        ranked = []
        for item, score in zip(evidence, scores, strict=True):
            enriched = dict(item)
            enriched["rerank_score"] = float(score)
            ranked.append(enriched)
        return sorted(ranked, key=lambda item: item["rerank_score"], reverse=True)


@lru_cache(maxsize=1)
def _default_reranker() -> CrossEncoderReranker:
    return CrossEncoderReranker(get_settings().reranker_model)


@dataclass(slots=True)
class InteractiveServices:
    retriever: Retriever
    graph_search: Callable[[str], list[dict]]
    llm: StructuredLLM
    reranker: Callable[[str, list[dict]], list[dict]] | None = None


def _graph_evidence(rows: list[dict]) -> list[dict]:
    evidence = []
    for index, row in enumerate(rows):
        details = " ".join(
            str(value)
            for value in (
                row.get("entity"),
                row.get("relation"),
                row.get("related_entity"),
            )
            if value
        )
        if details:
            evidence.append(
                {
                    "id": f"graph:{row.get('paper_id')}:{index}",
                    "paper_id": row.get("paper_id"),
                    "title": row.get("title"),
                    "text": details,
                    "source_type": "graph",
                    "score": 0.5,
                }
            )
    return evidence


def _answer_prompt(question: str, evidence: list[dict]) -> str:
    rendered = "\n".join(
        f"[{item['id']}] paper_id={item.get('paper_id')} | {item.get('text', '')}"
        for item in evidence
    )
    return (
        "Answer the research question using only the evidence below. Every claim "
        "must list one or more exact evidence IDs. If evidence is insufficient, "
        "say so and make no unsupported claim.\n\n"
        f"Question: {question}\nEvidence:\n{rendered}"
    )


def build_workflow(services: InteractiveServices):
    def retrieve(state: QueryState) -> dict:
        query = state["query"]
        vector = services.retriever.retrieve(query, state["retrieval_limit"])
        graph = _graph_evidence(services.graph_search(query))
        combined = {str(item["id"]): item for item in [*vector, *graph]}
        has_evidence = getattr(services.retriever, "has_evidence", None)
        corpus_empty = (
            not combined and callable(has_evidence) and not bool(has_evidence())
        )
        return {
            "evidence": list(combined.values()),
            "corpus_empty": corpus_empty,
            "trace": [*state.get("trace", []), f"retrieve:{len(combined)}"],
        }

    def assess(state: QueryState) -> dict:
        sufficient = any(item.get("text") for item in state.get("evidence", []))
        return {
            "sufficient": sufficient,
            "trace": [*state.get("trace", []), f"assess:{sufficient}"],
        }

    def route_after_assess(state: QueryState) -> str:
        if state["sufficient"]:
            return "rerank"
        if state.get("corpus_empty", False):
            return "insufficient"
        if state["retry_count"] >= state["max_rounds"] - 1:
            return "insufficient"
        return "reformulate"

    def insufficient(state: QueryState) -> dict:
        return {
            "answer": (
                "No indexed evidence was found for this question. "
                "Collect papers and run the evidence pipeline before querying."
            ),
            "claims": [],
            "citations": [],
            "grounded": False,
            "trace": [*state.get("trace", []), "insufficient"],
        }

    def reformulate(state: QueryState) -> dict:
        raw = services.llm.generate_json(
            "Reformulate this research question for semantic and graph retrieval. "
            f"Return only a focused query.\nQuestion: {state['question']}\n"
            f"Previous query: {state['query']}",
            QueryDraft.model_json_schema(),
        )
        query = QueryDraft.model_validate(raw).query
        return {
            "query": query,
            "retry_count": state["retry_count"] + 1,
            "trace": [*state.get("trace", []), "reformulate"],
        }

    def rerank(state: QueryState) -> dict:
        evidence = state.get("evidence", [])
        if services.reranker is not None:
            evidence = services.reranker(state["question"], evidence)
        else:
            evidence = sorted(
                evidence, key=lambda item: item.get("score", 0.0), reverse=True
            )
        return {
            "evidence": evidence[: state["retrieval_limit"]],
            "trace": [*state.get("trace", []), "rerank"],
        }

    def generate(state: QueryState) -> dict:
        raw = services.llm.generate_json(
            _answer_prompt(state["question"], state.get("evidence", [])),
            AnswerDraft.model_json_schema(),
        )
        draft = AnswerDraft.model_validate(raw)
        return {
            "answer": draft.answer,
            "claims": [claim.model_dump() for claim in draft.claims],
            "trace": [*state.get("trace", []), "generate"],
        }

    def grounding_check(state: QueryState) -> dict:
        evidence_by_id = {str(item["id"]): item for item in state.get("evidence", [])}
        claims = state.get("claims", [])
        structurally_grounded = bool(claims) and all(
            claim.get("evidence_ids")
            and all(item_id in evidence_by_id for item_id in claim["evidence_ids"])
            for claim in claims
        )
        grounded = False
        if structurally_grounded:
            evidence_text = "\n".join(
                f"[{item_id}] {item.get('text', '')}"
                for item_id, item in evidence_by_id.items()
            )
            raw = services.llm.generate_json(
                "Verify whether every claim is directly supported by its cited evidence. "
                "Mark grounded=false if any claim adds information not present in the evidence.\n"
                f"Claims: {claims}\nEvidence:\n{evidence_text}",
                GroundingVerdict.model_json_schema(),
            )
            verdict = GroundingVerdict.model_validate(raw)
            grounded = verdict.grounded and not verdict.unsupported_claim_indexes
        used_ids = {
            item_id for claim in claims for item_id in claim.get("evidence_ids", [])
        }
        citations = [
            evidence_by_id[item_id] for item_id in used_ids if item_id in evidence_by_id
        ]
        return {
            "grounded": grounded,
            "citations": citations,
            "trace": [*state.get("trace", []), f"grounding:{grounded}"],
        }

    def route_after_grounding(state: QueryState) -> str:
        if state["grounded"] or state["retry_count"] >= state["max_rounds"] - 1:
            return END
        return "reformulate"

    builder = StateGraph(QueryState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("assess", assess)
    builder.add_node("insufficient", insufficient)
    builder.add_node("reformulate", reformulate)
    builder.add_node("rerank", rerank)
    builder.add_node("generate", generate)
    builder.add_node("grounding_check", grounding_check)
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "assess")
    builder.add_conditional_edges("assess", route_after_assess)
    builder.add_edge("insufficient", END)
    builder.add_edge("reformulate", "retrieve")
    builder.add_edge("rerank", "generate")
    builder.add_edge("generate", "grounding_check")
    builder.add_conditional_edges("grounding_check", route_after_grounding)
    return builder.compile()


def run_query(
    question: str,
    services: InteractiveServices,
    max_rounds: int = 3,
    retrieval_limit: int = 12,
) -> dict:
    initial: QueryState = {
        "question": question,
        "query": question,
        "retry_count": 0,
        "max_rounds": max(1, min(max_rounds, 3)),
        "retrieval_limit": retrieval_limit,
        "trace": [],
    }
    result = build_workflow(services).invoke(initial)
    return {
        "answer": result.get("answer", "Insufficient evidence."),
        "claims": result.get("claims", []),
        "citations": result.get("citations", []),
        "grounded": result.get("grounded", False),
        "retry_count": result.get("retry_count", 0),
        "trace": result.get("trace", []),
        "provider": services.llm.provider,
        "model": services.llm.model,
    }


def default_services(settings: Settings | None = None) -> InteractiveServices:
    settings = settings or get_settings()
    index = EvidenceIndex(settings)

    def search(query: str) -> list[dict]:
        with graph_session(settings) as session:
            return query_graph(session, query, settings.retrieval_top_n)

    def rerank(question: str, evidence: list[dict]) -> list[dict]:
        return _default_reranker()(question, evidence)

    return InteractiveServices(index, search, get_llm_client(settings), rerank)
