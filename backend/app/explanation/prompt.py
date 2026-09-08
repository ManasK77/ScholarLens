"""The single Stage 9 prompt, and the query used to retrieve its evidence.

Both functions are pure and deterministic: same cluster, same rows, same
string. That is what lets `service.py` be tested without a model, and what
makes a report reproducible from the evidence it cites.

CLAUDE.md hard constraint 6 — *the LLM comes last* — is what shapes this
prompt. Retrieval, clustering and ranking have already happened; the model is
handed the finished evidence and the Stage 8 numbers, and is asked only to
explain them. It is never asked to find a gap, and it is told in as many words
that anything not in the evidence block is out of bounds.
"""

# Cheap character guard on the retrieval query. SPECTER2 truncates at 512 word
# pieces anyway (`embedding_max_chars` is 4000 for the same reason), so a query
# longer than this is embedding budget spent on nothing.
MAX_QUERY_CHARS = 4000

# How many member statements go into the query. The cluster label plus a
# handful of its own statements describes the theme; the rest are near
# duplicates of those and would only dilute the centroid.
QUERY_MEMBER_LIMIT = 5


def retrieval_query(label: str | None, member_texts: list[str]) -> str:
    """The text Stage 9 searches Qdrant with, for one cluster.

    Built from Phase 6's label plus the cluster's own statements rather than
    from the label alone: labels are frequency-derived and can be as thin as
    three words (D-06-06), which is not enough to place a query in SPECTER2
    space. Member order is Phase 6's stored order, so this is deterministic.
    """
    parts = [label.strip()] if label and label.strip() else []
    parts.extend(text.strip() for text in member_texts[:QUERY_MEMBER_LIMIT] if text)
    return " ".join(parts)[:MAX_QUERY_CHARS]


def _evidence_block(evidence: list[dict]) -> str:
    """The retrieved paragraphs, each addressable by its real Qdrant point id."""
    lines = []
    for item in evidence:
        source = item.get("title") or f"paper {item['paper_id']}"
        year = f", {item['year']}" if item.get("year") else ""
        lines.append(
            f"[{item['id']}] ({source}{year}; {item.get('source_type') or 'text'})\n"
            f"{item['text']}"
        )
    return "\n\n".join(lines)


def build_prompt(label: str | None, signals, evidence: list[dict]) -> str:
    """One cluster's complete prompt — evidence, Stage 8 numbers, instructions.

    `signals` is a `ClusterRanking` row (or anything exposing the same five
    attributes). The numbers are included because README §8's example report
    quotes them — *"27 papers mention it · only 3 propose mitigations"* — and
    the model cannot recover a corpus-wide count from a handful of paragraphs.
    They are given as computed facts, not as something to reinterpret.
    """
    theme = label.strip() if label and label.strip() else "(no label was derived)"
    return f"""You are a research analyst writing one evidence-grounded report about a \
research gap in the scientific literature. The gap has already been identified \
and scored by an upstream pipeline. Your only job is to explain it using the \
evidence below.

THEME: {theme}

PIPELINE MEASUREMENTS (computed from the corpus — treat these as facts):
- papers mentioning this theme: {signals.mention_frequency}
- mentions-per-year trend slope: {signals.trend_slope}
- solution density (papers proposing solutions / papers mentioning it): \
{signals.solution_density}
- mean citation count of the mentioning papers: {signals.citation_impact}
- cross-paper contradiction detected: {signals.contradiction_flag}

RETRIEVED EVIDENCE (each item is labelled with its id in square brackets):

{_evidence_block(evidence)}

RULES:
1. Use ONLY the retrieved evidence above and the pipeline measurements. Do not \
use outside knowledge, and do not mention any paper, method or result that does \
not appear above.
2. Every statement you make must be supported by at least one evidence item.
3. List in "evidence_ids" the ids of every evidence item you actually relied \
on. Use the ids exactly as they appear in square brackets. Do not invent ids.
4. "confidence" is a number between 0 and 1 reflecting how strongly the \
retrieved evidence and the measurements support the gap.
5. "suggested_directions" are short research directions (a few words each) that \
follow from the evidence.

Return only JSON matching this shape:
{{"gap": str, "confidence": float, "suggested_directions": [str], \
"evidence_ids": [str]}}"""
