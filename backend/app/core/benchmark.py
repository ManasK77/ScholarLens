"""Offline, read-only benchmark harness — priority_fix.md §D step 5.

The riskier batching changes (P3 SPECTER2 length-sorted batches, P4 GLiNER2
`batch_extract`, P6 thread tuning) are gated on measured evidence, and P1/P2
carry a parity check. This module is that measurement, and it is deliberately
separate from the pipeline:

* **It never writes.** No Qdrant upsert, no Neo4j statement, no PostgreSQL
  INSERT/UPDATE. It opens one session over the stored corpus, reads parsed
  documents and papers, and computes everything in memory.
* **It is bounded.** Every suite takes a `limit` on how many stored documents
  it touches, because loading a model over the whole corpus to answer a
  benchmark question is exactly the resource failure README §13.6 warns about.
* **It reports, it does not decide.** Turning `EMBEDDING_SORT_BY_LENGTH` or
  `NERRE_BATCH_SIZE` on after reading a report is the owner's call.

Run one suite at a time:

    python -m app.core.benchmark extraction   # P1  SciBERT sentence batching
    python -m app.core.benchmark headings     # P2  heading exemplar cache
    python -m app.core.benchmark embeddings   # P3  SPECTER2 length sorting
    python -m app.core.benchmark nerre        # P4  GLiNER2 batch_extract
    python -m app.core.benchmark threads      # P6  torch/OMP vs CPU quota
"""

import argparse
import json
import logging
import math
import os
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.rules import extract_rule_types, split_sentences
from app.ingestion.corpus import Paper, get_engine
from app.parsing import section_classifier
from app.parsing.storage import ParsedDocument

logger = logging.getLogger(__name__)


# --- corpus sampling (read-only) -------------------------------------------


def sample_parsed_documents(session: Session, limit: int) -> list[ParsedDocument]:
    """The first `limit` stored parsed documents, ordered by paper id.

    Ordered rather than random so two runs of the same suite compare the same
    documents — a benchmark whose sample moves cannot be compared with itself.
    """
    return list(
        session.scalars(
            select(ParsedDocument).order_by(ParsedDocument.paper_id).limit(limit)
        )
    )


def sample_papers(session: Session, limit: int) -> list[Paper]:
    return list(session.scalars(select(Paper).order_by(Paper.id).limit(limit)))


def unmatched_sentences(sections) -> list[str]:
    """The sentences that reach SciBERT: the ones no Phase 3 rule matched.

    Mirrors `extraction.service.extract_sections`' semantic path, which is the
    only path P1 changes.
    """
    sentences: list[str] = []
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        section_type = section.get("type")
        for paragraph in section.get("paragraphs") or []:
            if not isinstance(paragraph, dict):
                continue
            raw = paragraph.get("sentences") or split_sentences(
                paragraph.get("text", "")
            )
            for candidate in raw:
                sentence = " ".join(candidate.split())
                if sentence and not extract_rule_types(sentence, section_type):
                    sentences.append(sentence)
    return sentences


def headings_of(sections) -> list[str]:
    return [
        section.get("heading")
        for section in sections or []
        if isinstance(section, dict) and (section.get("heading") or "").strip()
    ]


# --- comparisons ------------------------------------------------------------


def label_parity(single, batched) -> dict:
    """Agreement between a one-at-a-time run and a batched run.

    Parity means "same labels", not identical floats: padding shifts a vector
    by ~1e-6, so only an exact near-tie at the 0.55/0.03 thresholds can flip a
    decision. That flip is what this counts.
    """
    if len(single) != len(batched):
        raise ValueError(
            f"cannot compare {len(single)} single results with {len(batched)} batched"
        )
    if not single:
        return {"total": 0, "matched": 0, "mismatched": 0, "parity": 1.0}
    matched = sum(
        1 for a, b in zip(single, batched, strict=True) if _label(a) == _label(b)
    )
    return {
        "total": len(single),
        "matched": matched,
        "mismatched": len(single) - matched,
        "parity": matched / len(single),
    }


def _label(match) -> str | None:
    if match is None:
        return None
    fact_type = getattr(match, "fact_type", match)
    return getattr(fact_type, "value", str(fact_type))


def padding_ratio(lengths, batch_size: int) -> float:
    """Σ(batch_max × batch_len) ÷ Σ(real tokens) for this ordering.

    1.0 means every padded position is a real token. 2.0 means the encoder
    computes twice the positions it needs. Tokenizer only — no model runs.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    real = sum(lengths)
    if not real:
        return 1.0
    padded = 0
    for start in range(0, len(lengths), batch_size):
        batch = lengths[start : start + batch_size]
        padded += max(batch) * len(batch)
    return padded / real


def padding_report(lengths, batch_size: int) -> dict:
    """Document order against length-sorted order — the P3 decision input."""
    current = padding_ratio(list(lengths), batch_size)
    ordered = padding_ratio(sorted(lengths), batch_size)
    return {
        "texts": len(lengths),
        "batch_size": batch_size,
        "current": current,
        "sorted": ordered,
        "speedup": current / ordered if ordered else 1.0,
    }


def _cosine(left, right) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def vector_drift(before, after) -> dict:
    """How far length sorting moves the stored vectors.

    The CLS vector is mathematically unaffected by padding, so anything below
    ~1e-5 of drift is float noise. A visibly lower `min_cosine` means the
    change is not the no-op the analysis assumed.
    """
    if len(before) != len(after):
        raise ValueError("vector sets must be the same length")
    if not before:
        return {"count": 0, "min_cosine": 1.0, "mean_cosine": 1.0}
    scores = [_cosine(a, b) for a, b in zip(before, after, strict=True)]
    return {
        "count": len(scores),
        "min_cosine": min(scores),
        "mean_cosine": sum(scores) / len(scores),
    }


def cluster_stability(labels_before, labels_after) -> float:
    """Adjusted Rand index between two HDBSCAN partitions of the same points."""
    from sklearn.metrics import adjusted_rand_score

    return float(adjusted_rand_score(list(labels_before), list(labels_after)))


def set_parity(single, batched) -> dict:
    """Entity/relation set agreement between per-chunk and batched extraction."""
    single_entities = _entity_keys(single)
    batched_entities = _entity_keys(batched)
    single_relations = _relation_keys(single)
    batched_relations = _relation_keys(batched)
    return {
        "entities_identical": single_entities == batched_entities,
        "relations_identical": single_relations == batched_relations,
        "entity_jaccard": _jaccard(single_entities, batched_entities),
        "relation_jaccard": _jaccard(single_relations, batched_relations),
        "entities_single": len(single_entities),
        "entities_batched": len(batched_entities),
        "relations_single": len(single_relations),
        "relations_batched": len(batched_relations),
    }


def _entity_keys(result) -> set:
    return {
        (" ".join(entity.text.casefold().split()), entity.entity_type)
        for entity in result.entities
    }


def _relation_keys(result) -> set:
    return {
        (
            " ".join(relation.head.casefold().split()),
            relation.relation_type,
            " ".join(relation.tail.casefold().split()),
        )
        for relation in result.relations
    }


def _jaccard(left: set, right: set) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


_EMPTY_PARITY = {
    "entities_identical": True,
    "relations_identical": True,
    "entity_jaccard": 1.0,
    "relation_jaccard": 1.0,
    "entities_single": 0,
    "entities_batched": 0,
    "relations_single": 0,
    "relations_batched": 0,
}


def _merge(reports: list[dict]) -> dict:
    """Sum the per-item set-parity reports into one corpus-level report."""
    if not reports:
        return dict(_EMPTY_PARITY)
    merged = {
        "entities_identical": all(r["entities_identical"] for r in reports),
        "relations_identical": all(r["relations_identical"] for r in reports),
        "entity_jaccard": sum(r["entity_jaccard"] for r in reports) / len(reports),
        "relation_jaccard": sum(r["relation_jaccard"] for r in reports) / len(reports),
    }
    for key in (
        "entities_single",
        "entities_batched",
        "relations_single",
        "relations_batched",
    ):
        merged[key] = sum(r[key] for r in reports)
    return merged


# --- suites -----------------------------------------------------------------


def run_extraction_suite(session: Session, classifier, limit: int) -> dict:
    """P1 — SciBERT one sentence at a time against one batched call."""
    sentences: list[str] = []
    for document in sample_parsed_documents(session, limit):
        sentences.extend(unmatched_sentences(document.sections))

    started = perf_counter()
    single = [classifier.classify(sentence) for sentence in sentences]
    single_seconds = perf_counter() - started

    started = perf_counter()
    batched = classifier.classify_many(sentences)
    batched_seconds = perf_counter() - started

    return {
        "suite": "extraction",
        "sentences": len(sentences),
        "parity": label_parity(single, batched),
        "single_seconds": single_seconds,
        "batched_seconds": batched_seconds,
        "speedup": single_seconds / batched_seconds if batched_seconds else None,
    }


def run_heading_suite(session: Session, embed_fn, limit: int) -> dict:
    """P2 — per-heading exemplar embedding against the cached, batched path."""
    headings: list[str] = []
    for document in sample_parsed_documents(session, limit):
        headings.extend(headings_of(document.sections))

    started = perf_counter()
    single = [
        section_classifier.classify_heading(heading, embed_fn) for heading in headings
    ]
    single_seconds = perf_counter() - started

    classifier = section_classifier.HeadingClassifier(embed_fn)
    started = perf_counter()
    batched = classifier.classify_many(headings)
    batched_seconds = perf_counter() - started

    return {
        "suite": "headings",
        "headings": len(headings),
        "parity": label_parity(single, batched),
        "single_seconds": single_seconds,
        "batched_seconds": batched_seconds,
        "speedup": single_seconds / batched_seconds if batched_seconds else None,
    }


def run_embedding_suite(session: Session, encoder, settings, limit: int) -> dict:
    """P3 — padding ratio (tokenizer only), then vector drift on a sample."""
    from app.embeddings.indexer import build_index_records
    from app.extraction.storage import ExtractedFact

    texts: list[str] = []
    for document in sample_parsed_documents(session, limit):
        paper = session.get(Paper, document.paper_id)
        if paper is None:
            continue
        facts = list(
            session.scalars(
                select(ExtractedFact).where(ExtractedFact.paper_id == paper.id)
            )
        )
        # The node id does not affect point ids or text, so a placeholder keeps
        # this suite off Neo4j entirely.
        texts.extend(
            record.text
            for record in build_index_records(paper, document, facts, "benchmark")
        )

    lengths = encoder.token_lengths(texts)
    report = {
        "suite": "embeddings",
        "padding": padding_report(lengths, settings.embedding_batch_size),
    }
    if encoder.can_encode:
        before = encoder.encode(texts, settings.embedding_batch_size)
        order = sorted(range(len(texts)), key=lambda index: len(texts[index]))
        sorted_vectors = encoder.encode(
            [texts[index] for index in order], settings.embedding_batch_size
        )
        after = [None] * len(texts)
        for position, index in enumerate(order):
            after[index] = sorted_vectors[position]
        report["drift"] = vector_drift(before, after)
    return report


def run_nerre_suite(
    session: Session,
    single_extractor,
    batched_extractor,
    limit: int,
    settings=None,
) -> dict:
    """P4 — per-chunk `extract` against `batch_extract`, set parity and timing."""
    from app.ner_re.service import collect_paper_text

    max_chars = getattr(settings, "nerre_max_chars_per_chunk", 1200)
    max_chunks = getattr(settings, "nerre_max_chunks_per_paper", 12)

    reports, papers, single_seconds, batched_seconds = [], 0, 0.0, 0.0
    for paper in sample_papers(session, limit):
        chunks = collect_paper_text(session, paper, max_chars, max_chunks)
        if not chunks:
            continue
        papers += 1
        started = perf_counter()
        single = single_extractor.extract_many(chunks)
        single_seconds += perf_counter() - started

        started = perf_counter()
        batched = batched_extractor.extract_many(chunks)
        batched_seconds += perf_counter() - started

        reports.append(set_parity(single, batched))

    return {
        "suite": "nerre",
        "papers": papers,
        "parity": _merge(reports),
        "single_seconds": single_seconds,
        "batched_seconds": batched_seconds,
        "speedup": single_seconds / batched_seconds if batched_seconds else None,
    }


def thread_report() -> dict:
    """P6 — what this process is actually allowed to use. Reports only.

    Changing thread counts is a machine/container configuration decision, so
    this never sets anything: it prints what torch chose against what the
    process is scheduled on, and the owner decides.
    """
    report = {
        "suite": "threads",
        "cpu_count": os.cpu_count() or 1,
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
        "mkl_num_threads": os.environ.get("MKL_NUM_THREADS"),
    }
    affinity = getattr(os, "sched_getaffinity", None)
    report["scheduled_cpus"] = len(affinity(0)) if affinity else None
    try:
        import torch

        report["torch_threads"] = torch.get_num_threads()
        report["torch_interop_threads"] = torch.get_num_interop_threads()
    except Exception:  # noqa: BLE001 — torch is optional for this report
        report["torch_threads"] = None
        report["torch_interop_threads"] = None
    return report


# --- CLI --------------------------------------------------------------------


class _TokenizingEncoder:
    """Wraps the real SPECTER2 encoder so the padding suite can skip the model."""

    def __init__(self, settings, with_model: bool):
        self.settings = settings
        self.can_encode = with_model
        self._encoder = None

    def _tokenizer(self):
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(
            self.settings.embedding_model,
            revision=self.settings.embedding_model_revision,
        )

    def token_lengths(self, texts):
        tokenizer = self._tokenizer()
        max_tokens = self.settings.embedding_max_tokens
        return [
            min(len(tokenizer.encode(text, add_special_tokens=True)), max_tokens)
            for text in texts
        ]

    def encode(self, texts, batch_size):
        if self._encoder is None:
            from app.embeddings.encoder import get_encoder

            self._encoder = get_encoder()
        return self._encoder.encode(texts, batch_size)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite", choices=["extraction", "headings", "embeddings", "nerre", "threads"]
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="stored documents/papers to measure (default 10; this is the "
        "resource guard — raise it deliberately)",
    )
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="embeddings: also load SPECTER2 and measure vector drift. Without "
        "it the suite reports the tokenizer-only padding ratio.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4,
        help="nerre: the batched extractor's chunk batch size (default 4)",
    )
    args = parser.parse_args(argv)

    # The thread report reads only this process, so it stays runnable without a
    # configured corpus — it is the first thing to check on a slow container.
    if args.suite == "threads":
        print(json.dumps(thread_report(), indent=2))
        return 0

    from app.core.config import get_settings
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)

    with Session(get_engine()) as session:
        report = _run(args, session, settings)
    print(json.dumps(report, indent=2, default=str))
    return 0


def _run(args, session: Session, settings) -> dict:
    if args.suite == "extraction":
        from app.extraction.semantic import get_semantic_classifier

        return run_extraction_suite(session, get_semantic_classifier(), args.limit)
    if args.suite == "headings":
        return run_heading_suite(session, section_classifier.get_embedder(), args.limit)
    if args.suite == "embeddings":
        return run_embedding_suite(
            session, _TokenizingEncoder(settings, args.with_model), settings, args.limit
        )
    from app.ner_re.extractor import JointEntityRelationExtractor, load_joint_model

    model = load_joint_model(settings)
    single = JointEntityRelationExtractor(
        model,
        entity_threshold=settings.nerre_entity_threshold,
        relation_threshold=settings.nerre_relation_threshold,
        batch_size=1,
    )
    batched = JointEntityRelationExtractor(
        model,
        entity_threshold=settings.nerre_entity_threshold,
        relation_threshold=settings.nerre_relation_threshold,
        batch_size=args.batch_size,
    )
    return run_nerre_suite(session, single, batched, args.limit, settings)


if __name__ == "__main__":
    raise SystemExit(main())
