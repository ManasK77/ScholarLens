# Phase 3: Scientific Information Extraction — Context

**Authorized:** 2026-08-30

## Boundary

Read only Phase 2 `ParsedDocument.sections`, extract exact evidence sentences as
Contribution, Limitation, Future Work, Dataset, Model, or Metric facts, and
persist typed rows linked to `papers.id`. Phase 4—not this phase—will extract
entity spans and relations.

## Brainstorm decisions

- A sentence may yield multiple fact types. “Our BERT model reaches 92 F1 on
  SQuAD” is simultaneously Contribution, Model, Metric, and Dataset evidence.
- Facts retain the exact normalized source sentence and section/paragraph/
  sentence coordinates. The extractor does not summarize or call an LLM.
- No labeled rhetorical/fact dataset or fine-tuning stage exists in the roadmap.
  Therefore the specified spaCy + Transformer pipeline is implemented as strong,
  unit-testable spaCy token/cue rules followed by an injectable SciBERT semantic
  fallback for rule-unmatched sentences.
- Canonical Limitation and Future Work sections provide high-confidence priors;
  other fact types require lexical or semantic evidence.
- The semantic path reuses Phase 2’s lazy, cached SciBERT embedder. Fast tests
  inject tiny vectors; one explicit integration check covers the real model.
- Re-extraction is idempotent: all facts for one paper are replaced in one
  session flush, preventing stale or duplicate evidence after a parsed document
  changes.
- spaCy uses `spacy.blank("en")` plus `sentencizer`; no separate language-model
  package or extra model download is needed.

## Non-goals

- No NER, relation extraction, graph writes, LLM calls, new ingestion sources,
  or API endpoints.

