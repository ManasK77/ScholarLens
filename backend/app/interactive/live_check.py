"""One live `POST /query` acceptance check for Mode B (README Phase 10 Done-when).

Default is a **dry run**: it prints the plan, and confirms with read-only reads
that the API is up, a key is configured (never printed), the index has
evidence and the expected papers exist. Nothing is sent to `/query`.

With `--confirm-live` it sends **exactly one** authenticated `POST /query`
(no client retries) and then checks the response against the stores with
read-only lookups (Qdrant `retrieve`, a PostgreSQL SELECT, a Neo4j MATCH).

Cost of the one request, from `app.interactive.workflow` and `app.explanation.llm`:

* Groq: 2-6 logical calls -- 0-2 reformulations (3 retrieval rounds max),
  1-2 answer generations (1 strict regeneration max), 1-2 grounding checks.
  The client retries a 429 or transport error up to `GROQ_MAX_RETRIES` (4)
  times per call, so at worst 30 HTTP requests.
* Local models, loaded in the backend process from the model cache: SPECTER2
  base + proximity adapter (query embedding) and the MS MARCO MiniLM
  cross-encoder (reranking). No download is expected (both are cached); the
  Hugging Face client may still make a metadata request.
* No database write: Mode B only reads Qdrant, Neo4j and PostgreSQL.

Run inside the backend container (prints sanitized JSON; never the key):

    docker exec -i nlp-backend-1 python - < backend/app/interactive/live_check.py
    docker exec -i nlp-backend-1 python - --confirm-live \
        < backend/app/interactive/live_check.py > live-query-result.json

Exit codes: 0 grounded answer, all checks pass; 3 safe non-answer
(insufficient evidence / grounding failed); 1 failure; 4 dry run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Sequence

API_URL = "http://127.0.0.1:8000"

# Chosen from the stored corpus (read-only): cluster 12's members state the
# languages each defence was evaluated on and the coverage limits.
QUESTION = (
    "Which languages have multilingual prompt injection defenses been evaluated "
    "on, and what limitations do the papers report about language coverage?"
)
# Papers whose cluster-12 statements answer it: MIPIAD (2: English and Bangla
# only), 10 (restricted languages), 9 (major languages only), 6 (translation
# naturalness), 4 (translation test suite). Informational, not pass/fail:
# retrieval may legitimately cite other relevant papers.
EXPECTED_PAPER_IDS = (2, 4, 6, 9, 10)

CITATION_KEYS = {"id", "paper_id", "title", "text", "year", "source_type"}
RESPONSE_KEYS = {
    "answer": str,
    "claims": list,
    "citations": list,
    "grounded": bool,
    "retrieval_rounds": int,
    "retry_count": int,
    "regeneration_count": int,
    "trace": list,
    "provider": str,
    "model": str,
}
# workflow.MAX_EVIDENCE_CHARS: a vector citation is the stripped payload text cut there.
EVIDENCE_CHARS = 1_200
_GRAPH_ID = re.compile(r"^graph:(\d+):[0-9a-f]{20}$")

# Grounding reason codes that are only reached after the LLM verification call.
VERIFIER_REASONS = frozenset(
    {
        "ok",
        "ok_partial",
        "verifier_rejected",
        "verifier_contradictory",
        "verifier_malformed",
    }
)

OUTCOMES = (
    ("No indexed evidence", "empty_index"),
    ("Insufficient relevant evidence", "insufficient_evidence"),
    ("A grounded answer could not be produced", "grounding_failed"),
)


def classify(body: dict) -> str:
    """A genuine grounded answer, or which safe non-answer the workflow returned."""
    outcome = body.get("outcome")
    if outcome in {"grounded_answer", "grounding_failed", "insufficient_evidence"}:
        return outcome
    if outcome == "no_indexed_evidence":
        return "empty_index"
    if body.get("grounded") is True:
        return "grounded_answer"
    answer = str(body.get("answer") or "")
    for prefix, outcome in OUTCOMES:
        if answer.startswith(prefix):
            return outcome
    return "ungrounded_unknown"


def trace_summary(trace: Sequence[str]) -> dict:
    """What the graph did, read from the node trace (counts only)."""
    assess = [t.split(":", 1)[1] for t in trace if t.startswith("assess:")]
    grounding = [t.split(":", 1)[1] for t in trace if t.startswith("grounding:")]
    fields = [
        dict(part.split("=", 1) for part in g.split(";") if "=" in part)
        for g in grounding
    ]
    reasons = [f.get("reason") for f in fields]
    # Per check, per claim: the veto codes ("-" = none), never any text.
    veto_codes = [
        [[] if c == "-" else c.split("+") for c in f["veto_codes"].split(",")]
        if f.get("veto_codes")
        else []
        for f in fields
    ]
    reformulations = sum(t.startswith("reformulate") for t in trace)
    generations = sum(t.startswith("generate:") for t in trace)
    return {
        "retrieval_rounds": sum(t.startswith("retrieve:") for t in trace),
        "assessments": assess,
        "reformulations": reformulations,
        "invalid_reformulations": sum(t == "reformulate:invalid" for t in trace),
        "grounding_reasons": reasons,
        "veto_codes": veto_codes,
        # Logical model calls (each may be retried by the client on a 429): every
        # reformulation and generation, plus a verification for each draft that
        # reached the LLM check (a structural failure or a veto skips it).
        "llm_calls": reformulations
        + generations
        # `verified=` (2026-10-18) says it directly; older traces imply it by
        # the reason (an off_question after a verification would be missed).
        + sum(
            f["verified"] == "1" if "verified" in f else r in VERIFIER_REASONS
            for f, r in zip(fields, reasons, strict=True)
        ),
        "reranked": "rerank" in trace,
        "generations": sum(t.startswith("generate:") for t in trace),
        "regenerations": sum(t.startswith("regenerate:") for t in trace),
        "grounding_checks": grounding,
        "terminal": trace[-1] if trace else None,
    }


def check_response(status: int, body: dict, sources: dict) -> dict[str, dict]:
    """Every check, each `{"ok": bool, "detail": ...}`.

    `sources` comes from read-only lookups made after the response:
    `points` (citation id -> Qdrant payload), `titles` (paper id -> corpus
    title, every paper in the corpus) and `entities` (paper id -> names of
    entities that paper MENTIONS in Neo4j).
    """
    checks: dict[str, dict] = {}

    def record(name: str, ok: bool, detail=None) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    record("http_200", status == 200, status)
    wrong = sorted(
        k for k, t in RESPONSE_KEYS.items() if not isinstance(body.get(k), t)
    )
    record("response_schema", not wrong, wrong or None)
    if wrong:
        return checks

    outcome = classify(body)
    citations = body["citations"]
    record("answer_non_empty", bool(body["answer"].strip()))

    bad_shape = [
        c.get("id") if isinstance(c, dict) else repr(c)[:40]
        for c in citations
        if not isinstance(c, dict)
        or set(c) != CITATION_KEYS
        or not isinstance(c.get("id"), str)
        or not c["id"]
        or isinstance(c.get("paper_id"), bool)
        or not isinstance(c.get("paper_id"), int)
    ]
    record("citation_structure", not bad_shape, bad_shape or None)
    if bad_shape:
        return checks

    cited = {c["id"] for c in citations}
    dangling = [
        i
        for claim in body["claims"]
        for i in (claim.get("evidence_ids") or [])
        if i not in cited
    ]
    record("claims_cite_returned_citations", not dangling, dangling or None)

    # The reverse: a citation no returned claim cites belongs to a withheld
    # claim or a rejected draft and must not be listed (2026-10-18).
    claimed = {i for claim in body["claims"] for i in (claim.get("evidence_ids") or [])}
    orphaned = [c["id"] for c in citations if c["id"] not in claimed]
    record("citations_cited_by_claims", not orphaned, orphaned or None)

    # Additive field; absent from an older API. A count, and 0 on a refusal.
    removed = body.get("removed_claim_count", 0)
    record(
        "removed_claim_count_valid",
        isinstance(removed, int)
        and not isinstance(removed, bool)
        and removed >= 0
        and (removed == 0 or outcome == "grounded_answer"),
        removed,
    )

    mismatched = []
    for c in citations:
        graph = _GRAPH_ID.match(c["id"])
        if graph:
            entity = str(c["text"]).split(" | ", 1)[0]
            if int(graph.group(1)) != c["paper_id"] or entity not in sources[
                "entities"
            ].get(c["paper_id"], set()):
                mismatched.append(c["id"])
            continue
        payload = sources["points"].get(c["id"])
        if (
            payload is None
            or payload.get("paper_id") != c["paper_id"]
            or c["text"] != str(payload.get("text", "")).strip()[:EVIDENCE_CHARS]
        ):
            mismatched.append(c["id"])
    record("citations_match_stored_sources", not mismatched, mismatched or None)

    wrong_title = [
        c["id"]
        for c in citations
        if c["paper_id"] not in sources["titles"]
        or c["title"] != sources["titles"][c["paper_id"]]
    ]
    record("citation_titles_match_corpus", not wrong_title, wrong_title or None)

    if outcome == "grounded_answer":
        record(
            "grounded_answer_has_claims_and_citations",
            bool(body["claims"])
            and bool(citations)
            and all(c.get("evidence_ids") for c in body["claims"]),
        )
    else:
        record("non_answer_carries_no_claims", not body["claims"], outcome)
    if outcome != "grounded_answer":
        # A refusal supports nothing: no rejected-draft or merely retrieved
        # passage may be listed beside it (D-10-07, D-10-08).
        record("refusal_lists_no_citations", not citations, len(citations))

    summary = trace_summary(body["trace"])
    record(
        "loop_bounds",
        body["retrieval_rounds"] <= 3
        and body["regeneration_count"] <= 1
        and body["retry_count"] == max(body["retrieval_rounds"] - 1, 0)
        and summary["retrieval_rounds"] == body["retrieval_rounds"],
        {k: body[k] for k in ("retrieval_rounds", "retry_count", "regeneration_count")},
    )
    return checks


def verdict(outcome: str, checks: dict[str, dict]) -> tuple[str, int]:
    if not all(c["ok"] for c in checks.values()):
        return "FAIL", 1
    if outcome == "grounded_answer":
        return "PASS", 0
    if outcome in ("insufficient_evidence", "grounding_failed"):
        return "SAFE_NON_ANSWER", 3
    return "FAIL", 1


def sanitize(body: dict) -> dict:
    """The response minus anything not needed to audit it. No headers, no key."""
    return {
        "answer": body.get("answer"),
        "claims": body.get("claims"),
        "citations": [
            {
                "id": c.get("id"),
                "paper_id": c.get("paper_id"),
                "title": c.get("title"),
                "year": c.get("year"),
                "source_type": c.get("source_type"),
                "text_chars": len(str(c.get("text") or "")),
                "text_start": str(c.get("text") or "")[:160],
            }
            for c in body.get("citations") or []
            if isinstance(c, dict)
        ],
        "grounded": body.get("grounded"),
        "retrieval_rounds": body.get("retrieval_rounds"),
        "retry_count": body.get("retry_count"),
        "regeneration_count": body.get("regeneration_count"),
        "removed_claim_count": body.get("removed_claim_count"),
        "trace": body.get("trace"),
        "provider": body.get("provider"),
        "model": body.get("model"),
    }


def _api_key(settings) -> str | None:
    key = settings.api_key
    key = key.get_secret_value() if hasattr(key, "get_secret_value") else key
    return key.strip() if isinstance(key, str) and key.strip() else None


def _sources(settings, citations: list[dict]) -> dict:
    """Read-only lookups for the checks. No writes anywhere."""
    from qdrant_client import QdrantClient
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.ingestion.corpus import Paper, get_engine
    from app.ner_re.graph import graph_session

    vector_ids = [c["id"] for c in citations if not _GRAPH_ID.match(str(c.get("id")))]
    client = QdrantClient(url=settings.qdrant_url, timeout=60)
    points = {
        str(p.id): p.payload or {}
        for p in (
            client.retrieve(
                settings.qdrant_collection, ids=vector_ids, with_payload=True
            )
            if vector_ids
            else []
        )
    }
    with Session(get_engine()) as session:
        titles = dict(session.execute(select(Paper.id, Paper.title)).all())
        session.rollback()
    graph_papers = sorted(
        {c["paper_id"] for c in citations if _GRAPH_ID.match(str(c.get("id")))}
    )
    entities: dict[int, set[str]] = {}
    if graph_papers:
        with graph_session(settings) as neo4j:
            for row in neo4j.run(
                "MATCH (p:Paper)-[:MENTIONS|SUPPORTS]->(e:Entity) WHERE p.id IN $ids "
                "RETURN p.id AS pid, e.name AS name",
                ids=graph_papers,
            ):
                entities.setdefault(row["pid"], set()).add(str(row["name"]).strip())
    return {"points": points, "titles": titles, "entities": entities}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mode B live acceptance check")
    parser.add_argument(
        "--confirm-live", action="store_true", help="send the one real POST /query"
    )
    parser.add_argument("--question", default=QUESTION, help="the one question to send")
    parser.add_argument(
        "--expected-paper-ids",
        help="';'-separated papers expected to be cited (informational)",
    )
    parser.add_argument("--test-id", help="stable id recorded with the result")
    parser.add_argument(
        "--expected-outcome",
        choices=("grounded_answer", "insufficient_evidence", "grounding_failed"),
        help="the outcome the test expects; recorded, not used to pass or fail checks",
    )
    args = parser.parse_args(argv)
    question = args.question
    expected_papers = (
        list(EXPECTED_PAPER_IDS)
        if args.expected_paper_ids is None
        else [int(p) for p in args.expected_paper_ids.split(";") if p.strip()]
    )

    import httpx

    from app.core.config import get_settings
    from app.embeddings.indexer import EvidenceIndex

    settings = get_settings()
    key = _api_key(settings)
    plan = {
        "test_id": args.test_id,
        "question": question,
        "expected_paper_ids": expected_papers,
        "expected_outcome": args.expected_outcome,
        "api_url": API_URL,
        "api_key_configured": key is not None,
        "health": httpx.get(f"{API_URL}/health", timeout=30).status_code,
        "index_has_evidence": EvidenceIndex(settings).has_evidence(),
        "cost": "2-6 Groq calls (<=4 retries each); SPECTER2 + cross-encoder loaded locally",
    }
    if not args.confirm_live:
        json.dump(
            {"mode": "dry_run", "plan": plan, "sent": False}, sys.stdout, indent=1
        )
        sys.stdout.write("\n")
        return 4
    if key is None or plan["health"] != 200 or not plan["index_has_evidence"]:
        json.dump(
            {"mode": "live", "plan": plan, "sent": False, "verdict": "NOT_RUN"},
            sys.stdout,
            indent=1,
        )
        return 1

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.monotonic()
    # Exactly one request: httpx does not retry, and nothing here loops.
    response = httpx.post(
        f"{API_URL}/query",
        json={"question": question},
        headers={"X-API-Key": key},
        timeout=900,
    )
    elapsed = round(time.monotonic() - started, 2)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict):
        detail = body.get("detail") if isinstance(body, dict) else None
        result = {
            "mode": "live",
            "sent": True,
            "test_id": args.test_id,
            "question": question,
            "started_at": started_at,
            "elapsed_seconds": elapsed,
            "http_status": response.status_code,
            "detail": detail,
            "expected_outcome": args.expected_outcome,
            "verdict": "FAIL",
        }
        json.dump(result, sys.stdout, indent=1)
        return 1

    citations = [c for c in body.get("citations") or [] if isinstance(c, dict)]
    checks = check_response(response.status_code, body, _sources(settings, citations))
    outcome = classify(body)
    label, code = verdict(outcome, checks)
    workflow = trace_summary(body.get("trace") or [])
    result = {
        "mode": "live",
        "sent": True,
        "test_id": args.test_id,
        "question": question,
        "started_at": started_at,
        "http_status": response.status_code,
        "elapsed_seconds": elapsed,
        "outcome": outcome,
        "expected_outcome": args.expected_outcome,
        "outcome_as_expected": None
        if args.expected_outcome is None
        else outcome == args.expected_outcome,
        "verdict": label,
        "checks": checks,
        "workflow": workflow,
        "llm_calls": workflow["llm_calls"],
        "expected_paper_ids": expected_papers,
        "expected_paper_overlap": sorted(
            {c["paper_id"] for c in citations} & set(expected_papers)
        ),
        "response": sanitize(body),
    }
    json.dump(result, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
