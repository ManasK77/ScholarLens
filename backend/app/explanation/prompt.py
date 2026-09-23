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

from app.explanation.selection import evidence_summary

# Cheap character guard on the retrieval query. SPECTER2 truncates at 512 word
# pieces anyway (`embedding_max_chars` is 4000 for the same reason), so a query
# longer than this is embedding budget spent on nothing.
MAX_QUERY_CHARS = 4000

# Above this share of the passages coming from a single paper, the evidence
# base is one study rather than a literature. The gap that motivated the A/B/C
# split drew 7 of its 8 passages (88%) from one paper.
DOMINANCE_SHARE = 0.5

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


def _evidence_note(evidence: list[dict]) -> str:
    """One line separating how much evidence there is from how many papers.

    The model cannot see that seven of its eight passages come from one study;
    it sees eight quotations. Saying so is what lets rule 7 below mean
    anything.
    """
    summary = evidence_summary(evidence)
    passages = summary["passage_count"]
    papers = summary["unique_paper_count"]
    largest = summary["max_passages_from_one_paper"]
    note = (
        f"{passages} passage(s) from {papers} unique paper(s); the largest "
        f"single paper contributes {largest} of them"
    )
    if passages and largest > passages * DOMINANCE_SHARE and papers > 0:
        note += (
            " -- this evidence base is dominated by one paper, so it shows "
            "what one study found, not what the literature agrees on"
        )
    return note


def build_prompt(label: str | None, signals, evidence: list[dict]) -> str:
    """One cluster's complete prompt -- evidence, Stage 8 numbers, instructions.

    `signals` is a `ClusterRanking` row (or anything exposing the same five
    attributes). The numbers are included because README S8's example report
    quotes them, and the model cannot recover a corpus-wide count from a
    handful of paragraphs.

    They are given as *measurements*, not as facts, which is a change
    (D-09-06). The previous prompt called them "computed from the corpus --
    treat these as facts", and the model duly wrote "inconsistent findings
    across studies" out of a `contradiction_flag` of 1.0 that was a false
    positive, with no opposing passage anywhere in its evidence. A measurement
    is a heuristic over the corpus; the retrieved passages are the evidence.
    Where they disagree, the passages win.
    """
    theme = label.strip() if label and label.strip() else "(no label was derived)"
    return f"""You are a research analyst writing one evidence-grounded report about a \
research gap in the scientific literature. The candidate gap has already been \
identified and scored by an upstream pipeline. Your job is to explain it using \
the evidence below, and to say plainly how much of it the evidence supports.

THEME: {theme}

PIPELINE MEASUREMENTS (heuristics computed over the whole corpus, not \
evidence; the retrieved passages below are the evidence, and where the two \
disagree the passages win):
- papers mentioning this theme: {signals.mention_frequency}
- mentions-per-year trend slope: {signals.trend_slope}
- solution density (papers proposing solutions / papers mentioning it): \
{signals.solution_density}
- mean citation count of the mentioning papers: {signals.citation_impact}
- cross-paper contradiction signal: {signals.contradiction_flag} (a heuristic \
NLI measure that is known to produce false positives -- never report a \
disagreement on the strength of this number alone)

EVIDENCE BASE: {_evidence_note(evidence)}

RETRIEVED EVIDENCE (each item is labelled with its id in square brackets):

{_evidence_block(evidence)}

THE THREE CLAIMS, WHICH YOU MUST JUDGE SEPARATELY:
(A) problem_evidence -- the problem exists and is real.
(B) solution_insufficiency_evidence -- existing solutions are insufficient, \
incomplete, or fail under some condition.
(C) research_gap_evidence -- a specific research gap remains: something \
identified as unstudied, unaddressed, or open.

RULES:
1. Use ONLY the retrieved evidence above. Do not use outside knowledge, and do \
not mention any paper, method or result that does not appear above.
2. Judge (A), (B) and (C) independently. Evidence that a problem is common \
does not by itself establish (B) or (C): "prompt injection attacks frequently \
succeed" does not by itself show that adaptive defenses are inadequate, nor \
that they are an open research gap. Set "supported": true only when a \
retrieved passage states that claim, and cite the ids that do. A claim with no \
cited passage must have "supported": false.
3. If (A) holds but (B) or (C) does not, say so: write the gap statement so it \
does not assert the unsupported part as established, and record the shortfall \
in that claim's "statement".
4. Do not write that findings are inconsistent, conflicting, mixed or \
contradictory unless two retrieved passages actually state opposing things. If \
they do, set conflicting_evidence.supported to true and cite both. Otherwise \
set it to false, whatever the contradiction measurement above says.
5. Every statement in "gap" must be supported by at least one evidence item.
6. List in "evidence_ids" the ids of every evidence item you relied on. Use \
the ids exactly as they appear in square brackets. Do not invent ids.
7. "confidence" is a number between 0 and 1. Lower it when the evidence comes \
from few unique papers, when it is dominated by a single paper, when (B) or \
(C) lacks direct support, when the contradiction measurement is unconfirmed by \
the passages, or when the evidence is otherwise incomplete. Confidence is \
about the evidence base, not about how plausible the gap sounds.
8. "suggested_directions" are short research directions (a few words each) \
that follow from the evidence.

Return only JSON matching this shape:
{{"gap": str, "confidence": float, "suggested_directions": [str], \
"evidence_ids": [str], "problem_evidence": {{"supported": bool, "statement": \
str, "evidence_ids": [str]}}, "solution_insufficiency_evidence": \
{{"supported": bool, "statement": str, "evidence_ids": [str]}}, \
"research_gap_evidence": {{"supported": bool, "statement": str, \
"evidence_ids": [str]}}, "conflicting_evidence": {{"supported": bool, \
"statement": str, "evidence_ids": [str]}}}}"""
