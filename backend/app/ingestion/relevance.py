"""S1 final relevance gate (D-S1-GATE-01).

Source search results are *candidates*, not proof of relevance. Before this
gate existed, whatever arXiv, Semantic Scholar or the ACL Anthology returned
was written to the corpus until a source's cap was full, and the ACL rule
accepted a record when each topic word occurred somewhere as a substring. That
is how "A Comprehensive Evaluation of Multilingual Chain-of-Thought Reasoning"
entered a "multilingual prompt injection" corpus: its abstract says "error
injection".

The gate judges a candidate's title and abstract against the topic as a
research topic, from four kinds of evidence, all of which are recorded:

* **Phrase evidence** -- which contiguous topic phrases occur verbatim.
* **Concept evidence** -- the topic is split into *facets* through the
  versioned concept lexicon (`concept_lexicon.json`); every facet must be
  evidenced, by one of its surface forms or, where the lexicon allows it, by
  enough distinct language names. Words the lexicon does not know are facets
  of their own, matched on word boundaries (never as substrings).
* **Negative evidence** -- a facet's head word used with a different modifier
  ("error injection" where the facet is "prompt injection").
* **Semantic evidence** -- a cross-encoder relevance score of the topic
  against the text, compared with a configurable floor.

Decision order, first match wins:

1. no title and no abstract              -> reject ``insufficient_text``
2. a facet unevidenced, its head word used in another sense
                                         -> reject ``sense_mismatch:<facet>``
3. a facet unevidenced and no abstract   -> reject ``insufficient_text``
4. a facet unevidenced                   -> reject ``missing_facet:<facet>``
5. semantic score below the floor        -> reject ``low_semantic_relevance``
6. otherwise                             -> admit ``relevant:strong`` or
                                            ``relevant:conceptual``

Only candidates that reach step 5 are scored, in one batch, so the model is
never loaded for a run whose candidates all fail the lexical checks.

**Calibration caveat.** The semantic floor (-1.0) and the strong-tier
threshold (3.5) were calibrated on one topic -- "multilingual prompt
injection" -- against a 50-document hand-labelled pool (2026-09-24). They are
settings, not validated constants; re-check them on any new topic.
"""

import json
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.ingestion.topic import meaningful_terms

GATE_VERSION = "relevance-gate-v1"
LEXICON_PATH = Path(__file__).with_name("concept_lexicon.json")

_LATEX = re.compile(r"[{}\\]")
_SEPARATORS = re.compile(r"[-_/‐-―]+")
# Words that precede a head word without making it a different sense:
# "the injection", "against injection", "of injection".
_NOT_MODIFIERS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "against",
        "as",
        "by",
        "each",
        "for",
        "from",
        "in",
        "its",
        "no",
        "of",
        "on",
        "or",
        "such",
        "that",
        "the",
        "their",
        "these",
        "this",
        "those",
        "to",
        "via",
        "with",
        "without",
    }
)


def normalize(text: str | None) -> str:
    """Lowercase; LaTeX braces dropped; hyphens, underscores, slashes as spaces."""
    text = _LATEX.sub("", text or "").lower()
    return " ".join(_SEPARATORS.sub(" ", text).split())


def _pattern(form: str) -> re.Pattern:
    """A normalized form on word boundaries, with an optional plural."""
    return re.compile(rf"(?<!\w){re.escape(form)}(?:e?s)?(?!\w)")


# ---------------------------------------------------------------------------
# The concept lexicon
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Concept:
    name: str
    forms: tuple[str, ...]
    sense_head: str | None = None
    same_sense_modifiers: frozenset[str] = frozenset()
    min_language_names: int | None = None


@dataclass(frozen=True)
class Lexicon:
    version: str
    concepts: tuple[Concept, ...]
    language_names: frozenset[str]


@lru_cache(maxsize=4)
def load_lexicon(path: Path = LEXICON_PATH) -> Lexicon:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    concepts = []
    for name, spec in raw["concepts"].items():
        sense = spec.get("sense") or {}
        concepts.append(
            Concept(
                name=name,
                forms=tuple(normalize(form) for form in spec["forms"]),
                sense_head=sense.get("head"),
                same_sense_modifiers=frozenset(sense.get("same_sense_modifiers", ())),
                min_language_names=spec.get("min_language_names"),
            )
        )
    return Lexicon(
        version=raw["version"],
        concepts=tuple(concepts),
        language_names=frozenset(raw.get("language_names", ())),
    )


# ---------------------------------------------------------------------------
# Topic analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facet:
    name: str
    kind: str  # "concept" | "word"
    forms: tuple[str, ...]
    concept: Concept | None = None


@dataclass(frozen=True)
class TopicAnalysis:
    topic: str
    tokens: tuple[str, ...]
    facets: tuple[Facet, ...]
    phrases: tuple[str, ...]
    lexicon: Lexicon

    @property
    def query(self) -> str:
        """What the semantic model is asked about: the topic as typed."""
        return " ".join(self.topic.split())


def _concept_for(phrase: str, lexicon: Lexicon) -> Concept | None:
    for concept in lexicon.concepts:
        if any(
            re.fullmatch(rf"{re.escape(form)}(?:e?s)?", phrase)
            for form in concept.forms
        ):
            return concept
    return None


def analyze_topic(topic: str, lexicon: Lexicon) -> TopicAnalysis:
    """Split `topic` into facets: longest lexicon concept first, else a word."""
    tokens = normalize(" ".join(meaningful_terms(topic))).split()
    facets: list[Facet] = []
    seen: set[str] = set()
    position = 0
    while position < len(tokens):
        for end in range(len(tokens), position, -1):
            concept = _concept_for(" ".join(tokens[position:end]), lexicon)
            if concept is not None:
                if concept.name not in seen:
                    seen.add(concept.name)
                    facets.append(
                        Facet(concept.name, "concept", concept.forms, concept)
                    )
                position = end
                break
        else:
            word = tokens[position]
            if word not in seen:
                seen.add(word)
                facets.append(Facet(word, "word", (word,)))
            position += 1
    phrases = tuple(
        " ".join(tokens[start:end])
        for size in range(len(tokens), 1, -1)
        for start in range(len(tokens) - size + 1)
        for end in (start + size,)
    )
    return TopicAnalysis(topic, tuple(tokens), tuple(facets), phrases, lexicon)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelevanceDecision:
    admitted: bool
    reason_code: str
    reason: str
    score: float | None = None
    tier: str | None = None
    evidence: dict = field(default_factory=dict)

    def as_duplicate_of(self, paper_id: int) -> "RelevanceDecision":
        """This copy of a paper already admitted for the same topic."""
        return RelevanceDecision(
            admitted=True,
            reason_code="duplicate_of_admitted",
            reason=(
                f"Same paper as corpus paper {paper_id}, already admitted for this "
                f"topic; the gate's verdict on this copy alone was "
                f"'{self.reason_code}'."
            ),
            score=self.score,
            tier=self.tier,
            evidence={**self.evidence, "gate": self._verdict()},
        )

    def with_evidence(self, **extra) -> "RelevanceDecision":
        return replace(self, evidence={**self.evidence, **extra})

    def capped(self, source: str, limit: int) -> "RelevanceDecision":
        """Relevant, but the source's admission cap was already full."""
        score = "n/a" if self.score is None else f"{self.score:.2f}"
        return RelevanceDecision(
            admitted=False,
            reason_code="source_cap_reached",
            reason=(
                f"Passed the relevance gate ({self.reason_code}, score {score}) but "
                f"the {source} cap of {limit} was filled by higher-scoring candidates."
            ),
            score=self.score,
            tier=self.tier,
            evidence={**self.evidence, "gate": self._verdict()},
        )

    def _verdict(self) -> dict:
        return {
            "admitted": self.admitted,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


class SemanticScorer(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


@dataclass
class _Lexical:
    text: str
    evidence: dict
    decision: RelevanceDecision | None


class RelevanceGate:
    def __init__(
        self,
        analysis: TopicAnalysis,
        scorer: SemanticScorer,
        *,
        semantic_floor: float,
        strong_threshold: float,
        model_name: str | None = None,
    ) -> None:
        self.analysis = analysis
        self._scorer = scorer
        self.semantic_floor = semantic_floor
        self.strong_threshold = strong_threshold
        self.model_name = model_name
        self._patterns = {
            facet.name: [_pattern(form) for form in facet.forms]
            for facet in analysis.facets
        }
        self._names = [
            (name, _pattern(name)) for name in sorted(analysis.lexicon.language_names)
        ]

    # -- evidence ----------------------------------------------------------

    def _facet_evidence(self, facet: Facet, text: str) -> dict:
        matched = [
            form
            for form, pattern in zip(
                facet.forms, self._patterns[facet.name], strict=True
            )
            if pattern.search(text)
        ]
        evidence: dict = {"kind": facet.kind, "matched": matched}
        present = bool(matched)
        if facet.kind == "concept":
            names: list[str] = []
            minimum = facet.concept.min_language_names
            if minimum:
                names = [name for name, pattern in self._names if pattern.search(text)]
                present = present or len(names) >= minimum
            evidence["language_names"] = names
        evidence["present"] = present
        return evidence

    @staticmethod
    def _other_senses(concept: Concept, text: str) -> list[str]:
        head = re.escape(concept.sense_head)
        found = {
            f"{match.group(1)} {concept.sense_head}"
            for match in re.finditer(rf"(?<!\w)(\w+) {head}(?:e?s)?(?!\w)", text)
            if match.group(1) not in concept.same_sense_modifiers
            and match.group(1) not in _NOT_MODIFIERS
        }
        return sorted(found)

    def facets_evidenced(self, title: str | None, abstract: str | None) -> bool:
        """Cheap lexical check: does every facet have evidence? (ACL candidacy)"""
        text = normalize(f"{title or ''} {abstract or ''}")
        return all(
            self._facet_evidence(facet, text)["present"]
            for facet in self.analysis.facets
        )

    def prefilter_terms(self) -> list[str]:
        """Normalized phrases every record evidencing all facets must contain.

        One facet's forms suffice -- a record with evidence for every facet has
        evidence for that one. The facet chosen is the one with the fewest
        forms among those that language names alone cannot evidence; a
        substring test on normalized text is a superset of the word-boundary
        match, so nothing the gate would admit is filtered out.
        """
        facets = [
            f
            for f in self.analysis.facets
            if f.concept is None or not f.concept.min_language_names
        ]
        extra: set[str] = set()
        if not facets:
            facets = list(self.analysis.facets)
            extra = set(self.analysis.lexicon.language_names)
        facet = min(facets, key=lambda f: len(f.forms))
        return sorted(set(facet.forms) | extra)

    def _lexical(self, title: str | None, abstract: str | None) -> _Lexical:
        title = " ".join((title or "").split())
        abstract = " ".join((abstract or "").split())
        text = f"{title}. {abstract}" if title and abstract else (title or abstract)
        normalized = normalize(f"{title} {abstract}")
        facets = {
            facet.name: self._facet_evidence(facet, normalized)
            for facet in self.analysis.facets
        }
        negative = {
            facet.name: self._other_senses(facet.concept, normalized)
            for facet in self.analysis.facets
            if facet.concept is not None and facet.concept.sense_head
        }
        evidence = {
            "text_used": (
                "title+abstract"
                if title and abstract
                else "title"
                if title
                else "abstract"
            ),
            "phrase": {
                "exact_topic_phrase": bool(self.analysis.tokens)
                and _pattern(" ".join(self.analysis.tokens)).search(normalized)
                is not None,
                "topic_phrases_present": [
                    phrase
                    for phrase in self.analysis.phrases
                    if _pattern(phrase).search(normalized)
                ],
            },
            "facets": facets,
            "negative": negative,
            "semantic": {
                "score": None,
                "floor": self.semantic_floor,
                "strong_threshold": self.strong_threshold,
                "model": self.model_name,
            },
        }

        def reject(code: str, reason: str) -> _Lexical:
            return _Lexical(
                text,
                evidence,
                RelevanceDecision(False, code, reason, evidence=evidence),
            )

        if not title and not abstract:
            return reject(
                "insufficient_text", "Neither a title nor an abstract was available."
            )
        missing = [name for name, found in facets.items() if not found["present"]]
        if missing:
            mismatched = [name for name in missing if negative.get(name)]
            if mismatched:
                name = mismatched[0]
                return reject(
                    f"sense_mismatch:{name}",
                    f"No evidence for '{name}'; its head word appears only as "
                    f"{', '.join(repr(s) for s in negative[name])}.",
                )
            listed = ", ".join(f"'{name}'" for name in missing)
            if not abstract:
                return reject(
                    "insufficient_text",
                    f"No abstract, and the title alone shows no evidence for {listed}.",
                )
            return reject(
                f"missing_facet:{missing[0]}",
                f"No evidence in the title or abstract for {listed}.",
            )
        return _Lexical(text, evidence, None)

    def evaluate_many(
        self, items: list[tuple[str | None, str | None]]
    ) -> list[RelevanceDecision]:
        """Judge candidates; one batched semantic call for those that need it."""
        lexical = [self._lexical(title, abstract) for title, abstract in items]
        pending = [index for index, item in enumerate(lexical) if item.decision is None]
        scores = (
            self._scorer.score(self.analysis.query, [lexical[i].text for i in pending])
            if pending
            else []
        )
        decisions = [item.decision for item in lexical]
        for index, score in zip(pending, scores, strict=True):
            decisions[index] = self._semantic(lexical[index].evidence, float(score))
        return decisions

    def _semantic(self, evidence: dict, score: float) -> RelevanceDecision:
        evidence = {**evidence, "semantic": {**evidence["semantic"], "score": score}}
        if score < self.semantic_floor:
            return RelevanceDecision(
                False,
                "low_semantic_relevance",
                f"Every facet has evidence, but the semantic relevance score "
                f"{score:.2f} is below the floor {self.semantic_floor:.2f}.",
                score=score,
                evidence=evidence,
            )
        tier = "strong" if score >= self.strong_threshold else "conceptual"
        facets = "; ".join(
            f"{name}: {', '.join(found['matched'] or found.get('language_names', []))}"
            for name, found in evidence["facets"].items()
        )
        return RelevanceDecision(
            True,
            f"relevant:{tier}",
            f"Evidence for every facet ({facets}); semantic score {score:.2f} "
            f"(floor {self.semantic_floor:.2f}).",
            score=score,
            tier=tier,
            evidence=evidence,
        )

    def describe(self) -> dict:
        """What a collection run records about the gate that judged it."""
        return {
            "gate_version": GATE_VERSION,
            "lexicon_version": self.analysis.lexicon.version,
            "facets": [
                {"name": f.name, "kind": f.kind, "forms": list(f.forms)}
                for f in self.analysis.facets
            ],
            "semantic_floor": self.semantic_floor,
            "strong_threshold": self.strong_threshold,
            "model": self.model_name,
        }


# ---------------------------------------------------------------------------
# The semantic model
# ---------------------------------------------------------------------------


class CrossEncoderScorer:
    """Raw cross-encoder logits -- the scale the floor was calibrated on.

    Loaded on first use, once per process, and only when a candidate passes
    every lexical check.
    """

    def __init__(self, model_name: str, batch_size: int) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._loaded = None

    def _load(self):
        if self._loaded is None:
            self._loaded = _load_cross_encoder(self.model_name)
        return self._loaded

    def score(self, query: str, texts: list[str]) -> list[float]:
        import torch

        tokenizer, model = self._load()
        scores: list[float] = []
        with torch.no_grad():
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                encoded = tokenizer(
                    [query] * len(batch),
                    batch,
                    truncation="only_second",
                    max_length=512,
                    padding=True,
                    return_tensors="pt",
                )
                scores.extend(float(v) for v in model(**encoded).logits[:, 0])
        return scores


@lru_cache(maxsize=2)
def _load_cross_encoder(model_name: str):
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    return (
        AutoTokenizer.from_pretrained(model_name),
        AutoModelForSequenceClassification.from_pretrained(model_name).eval(),
    )


def build_gate(topic: str, settings) -> RelevanceGate:
    """The production gate for one `/collect` run."""
    return RelevanceGate(
        analyze_topic(topic, load_lexicon()),
        CrossEncoderScorer(settings.relevance_model, settings.relevance_batch_size),
        semantic_floor=settings.relevance_semantic_floor,
        strong_threshold=settings.relevance_strong_threshold,
        model_name=settings.relevance_model,
    )
