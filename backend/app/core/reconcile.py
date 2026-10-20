"""Read-only store audit: python -m app.core.reconcile (run with writes idle).

`--papers` adds the per-paper, per-stage report: what each of S2-S5 holds for
every paper, whether each stage applied to it at all, and cross-stage
provenance checks (D-RECON-01). Counts and identifiers only, never text.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.pipeline import PaperStageState
from app.embeddings.indexer import EvidenceIndex, build_index_records
from app.extraction.rules import split_sentences
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper, get_engine
from app.ner_re.graph import NERRE_PROVENANCE_VERSION, graph_session
from app.ner_re.service import _mention_pattern, source_units
from app.parsing.storage import get_parsed_document


def compare_projections(expected, actual, paper_ids, graph_nodes):
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    stale = sorted(
        key for key in expected.keys() & actual.keys() if expected[key] != actual[key]
    )
    absent_graph = sorted(paper_ids - graph_nodes.keys())
    orphan_graph = sorted(graph_nodes.keys() - paper_ids)
    return {
        "consistent": not any((missing, extra, stale, absent_graph, orphan_graph)),
        "missing_points": missing,
        "extra_points": extra,
        "stale_points": stale,
        "missing_graph_papers": absent_graph,
        "orphan_graph_papers": orphan_graph,
    }


def inspect_stores(session, neo4j, index, pdf_directory=None):
    graph_nodes = {
        row["id"]: row["node_id"]
        for row in neo4j.run(
            "MATCH (p:Paper) RETURN p.id AS id, elementId(p) AS node_id"
        )
    }
    actual, offset = {}, None
    if index.client.collection_exists(index.collection):
        while True:
            page, offset = index.client.scroll(
                collection_name=index.collection,
                with_payload=True,
                with_vectors=False,
                limit=256,
                offset=offset,
            )
            actual.update((str(point.id), dict(point.payload or {})) for point in page)
            if offset is None:
                break
    papers = list(session.scalars(select(Paper).order_by(Paper.id)))
    expected = {}
    missing_files, referenced_files = [], set()
    for paper in papers:
        if paper.pdf_path:
            referenced_files.add(Path(paper.pdf_path).resolve())
            if not Path(paper.pdf_path).is_file():
                missing_files.append(paper.id)
        doc = get_parsed_document(session, paper.id)
        if doc is None:
            continue
        facts = list(
            session.scalars(
                select(ExtractedFact).where(ExtractedFact.paper_id == paper.id)
            )
        )
        for record in build_index_records(
            paper, doc, facts, graph_nodes.get(paper.id, "missing-graph-node")
        ):
            expected[record.record_id] = index.record_payload(record)
    report = compare_projections(expected, actual, {p.id for p in papers}, graph_nodes)
    report["counts"] = {
        "papers": len(papers),
        "expected_points": len(expected),
        "actual_points": len(actual),
        "graph_papers": len(graph_nodes),
    }
    report["missing_pdf_papers"] = missing_files
    report["unreferenced_pdf_count"] = (
        sum(
            path.resolve() not in referenced_files
            for path in Path(pdf_directory).glob("*.pdf")
        )
        if pdf_directory
        else 0
    )
    report["incomplete_checkpoints"] = []
    if inspect(session.get_bind()).has_table(PaperStageState.__tablename__):
        report["incomplete_checkpoints"] = [
            {"paper_id": s.paper_id, "stage": s.stage, "status": s.status}
            for s in session.scalars(
                select(PaperStageState).where(PaperStageState.status != "done")
            )
        ]
    report["consistent"] = report["consistent"] and not report["incomplete_checkpoints"]
    return report


def statement_location_mismatches(sections, facts) -> list[str]:
    """Locations of S3 statements that are not the sentence stored there.

    A statement is the whitespace-normalized sentence at (section, paragraph,
    sentence) of the parsed document -- GROBID's sentence list, or the same
    spaCy split Stage 3 applies when there is none.
    """
    bad = []
    for fact in facts:
        location = f"{fact.section_index}.{fact.paragraph_index}.{fact.sentence_index}"
        try:
            paragraph = sections[fact.section_index]["paragraphs"][fact.paragraph_index]
            sentences = paragraph.get("sentences") or split_sentences(
                paragraph.get("text", "")
            )
            sentence = " ".join(sentences[fact.sentence_index].split())
        except (IndexError, KeyError, TypeError):
            sentence = None
        if sentence != fact.text:
            bad.append(location)
    return sorted(set(bad))


def mention_problems(units, mentions) -> dict[str, int]:
    """Graph mentions that do not trace back to the paper's S4 input.

    `mentions` are (normalized entity name, evidence refs or None). An entity
    that occurs in no unit is `not_in_source`; one citing a unit that does not
    contain it is `bad_evidence`; one with no evidence recorded at all (a
    projection written before provenance existed) is `no_evidence`.
    """
    folded = {unit.ref: unit.text.casefold() for unit in units}
    problems: Counter[str] = Counter()
    for name, evidence in mentions:
        pattern = _mention_pattern(name)
        if not any(pattern.search(text) for text in folded.values()):
            problems["not_in_source"] += 1
        elif not evidence:
            problems["no_evidence"] += 1
        elif not all(ref in folded and pattern.search(folded[ref]) for ref in evidence):
            problems["bad_evidence"] += 1
    return dict(problems)


def stage_status(checkpoint: str | None, applicable: bool) -> str:
    """One stage's state for one paper, from its checkpoint row.

    `not_applicable` (the stage has nothing to do for this paper, e.g. S2 for a
    metadata-only record) is kept apart from `not_started` (it should have run
    and has not) -- a missing artifact means different things in each case.
    """
    if checkpoint is None:
        return "not_applicable" if not applicable else "not_started"
    return {"done": "completed"}.get(checkpoint, checkpoint)


def _pdf_state(paper) -> str:
    if not paper.pdf_path:
        return "none"
    path = Path(paper.pdf_path)
    if not path.is_file():
        return "missing_file"
    with path.open("rb") as handle:
        return "valid_pdf" if handle.read(5) == b"%PDF-" else "not_a_pdf"


def _graph_paper(neo4j, paper_id):
    row = next(
        iter(
            neo4j.run(
                "MATCH (p:Paper {id: $id}) "
                "OPTIONAL MATCH ()-[r]->() WHERE r.paper_id = p.id "
                "RETURN properties(p) AS props, count(r) AS relations",
                id=paper_id,
            )
        ),
        None,
    )
    mentions = [
        (row_["name"], row_["evidence"])
        for row_ in neo4j.run(
            "MATCH (p:Paper {id: $id})-[m:MENTIONS]->(e) "
            "RETURN properties(e)['normalized_name'] AS name, "
            "properties(m)['evidence'] AS evidence",
            id=paper_id,
        )
    ]
    return row, mentions


def _paper_row(session, neo4j, index, paper, states, graph_nodes, actual):
    warnings: list[str] = []
    document = get_parsed_document(session, paper.id)
    facts = list(
        session.scalars(select(ExtractedFact).where(ExtractedFact.paper_id == paper.id))
    )
    pdf = _pdf_state(paper)

    s2 = {
        "status": stage_status(
            states.get("s2"), pdf == "valid_pdf" or document is not None
        )
    }
    if document is not None:
        paragraphs = [
            p for section in document.sections for p in section.get("paragraphs") or []
        ]
        s2.update(
            parser=document.parser_used,
            sections=len(document.sections),
            paragraphs=len(paragraphs),
            text_chars=sum(len(p.get("text") or "") for p in paragraphs),
        )
        if document.parser_used != "grobid":
            warnings.append(f"s2_parser_{document.parser_used}")
    if pdf in ("missing_file", "not_a_pdf"):
        warnings.append(f"s2_pdf_{pdf}")

    s3 = {
        "status": stage_status(states.get("s3"), document is not None),
        "statements": dict(Counter(f.fact_type for f in facts)),
    }
    if document is not None:
        s3["location_mismatches"] = len(
            statement_location_mismatches(document.sections, facts)
        )
        if s3["location_mismatches"]:
            warnings.append("s3_location_mismatch")

    units = source_units(session, paper)
    props_row, mentions = _graph_paper(neo4j, paper.id)
    s4 = {"status": stage_status(states.get("s4"), bool(units))}
    if props_row is None:
        if units:
            warnings.append("s4_not_projected")
    else:
        props = props_row["props"] or {}
        s4.update(
            mentions=len(mentions),
            relations=props_row["relations"],
            provenance=props.get("nerre_provenance"),
            input_basis=props.get("nerre_input_basis"),
            model_revision=props.get("nerre_model_revision"),
            **mention_problems(units, mentions),
        )
        if props.get("nerre_provenance") != NERRE_PROVENANCE_VERSION:
            warnings.append("s4_provenance_missing")
        if s4.get("not_in_source") or s4.get("bad_evidence"):
            warnings.append("s4_evidence_invalid")

    expected = {}
    if document is not None:
        for record in build_index_records(
            paper, document, facts, graph_nodes.get(paper.id, "missing-graph-node")
        ):
            expected[record.record_id] = index.record_payload(record)
    stored = {k: v for k, v in actual.items() if v.get("paper_id") == paper.id}
    s5 = {
        "status": stage_status(states.get("s5"), True),
        "points": dict(Counter(v.get("source_type") for v in stored.values())),
        "missing": len(expected.keys() - stored.keys()),
        "extra": len(stored.keys() - expected.keys()),
        "stale": sum(expected[k] != stored[k] for k in expected.keys() & stored.keys()),
    }
    if s5["missing"] or s5["extra"] or s5["stale"]:
        warnings.append("s5_points_out_of_sync")

    for stage, row in (("s2", s2), ("s3", s3), ("s4", s4), ("s5", s5)):
        if row["status"] in ("failed", "pending", "not_started"):
            warnings.append(f"{stage}_{row['status']}")
    return {
        "paper_id": paper.id,
        "title": (paper.title or "")[:80],
        "pdf": pdf,
        "input_basis": "full_text"
        if document is not None and document.parser_used != "none"
        else ("abstract_only" if paper.abstract else "none"),
        "s2": s2,
        "s3": s3,
        "s4": s4,
        "s5": s5,
        "warnings": warnings,
    }


def paper_reconciliation(session, neo4j, index) -> dict:
    """Every paper's S2-S5 state and provenance checks, plus per-stage totals."""
    graph_nodes = {
        row["id"]: row["node_id"]
        for row in neo4j.run(
            "MATCH (p:Paper) RETURN p.id AS id, elementId(p) AS node_id"
        )
    }
    actual, offset = {}, None
    if index.client.collection_exists(index.collection):
        while True:
            page, offset = index.client.scroll(
                collection_name=index.collection,
                with_payload=True,
                with_vectors=False,
                limit=256,
                offset=offset,
            )
            actual.update((str(point.id), dict(point.payload or {})) for point in page)
            if offset is None:
                break
    states: dict[int, dict[str, str]] = {}
    if inspect(session.get_bind()).has_table(PaperStageState.__tablename__):
        for state in session.scalars(select(PaperStageState)):
            states.setdefault(state.paper_id, {})[state.stage] = state.status
    rows = [
        _paper_row(
            session, neo4j, index, paper, states.get(paper.id, {}), graph_nodes, actual
        )
        for paper in session.scalars(select(Paper).order_by(Paper.id))
    ]
    stages = {
        stage: dict(Counter(row[stage]["status"] for row in rows))
        for stage in ("s2", "s3", "s4", "s5")
    }
    totals = {
        "papers": len(rows),
        "statements": sum(sum(row["s3"]["statements"].values()) for row in rows),
        "mentions": sum(row["s4"].get("mentions", 0) for row in rows),
        "relations": sum(row["s4"].get("relations", 0) for row in rows),
        "points": len(actual),
        "papers_with_warnings": sum(bool(row["warnings"]) for row in rows),
    }
    return {"papers": rows, "stages": stages, "totals": totals}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--papers", action="store_true", help="add the per-paper, per-stage report"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    with Session(get_engine()) as session, graph_session(settings) as neo4j:
        index = EvidenceIndex(settings)
        report = inspect_stores(session, neo4j, index, settings.pdf_storage_path)
        if args.papers:
            report["per_paper"] = paper_reconciliation(session, neo4j, index)
    print(json.dumps(report, indent=2))
    return 0 if report["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
