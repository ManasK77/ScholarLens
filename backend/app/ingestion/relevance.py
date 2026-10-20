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

**Meaningful evidence (gate v2, S1 audit 2026-10-10).** v1 counted a facet
as evidenced on any occurrence of one of its forms, and the audit of the first
"multilingual prompt injection" corpus found that admits papers that do not
study the topic. An occurrence now does not count when it sits in a
future-work sentence ("Future research should explore multilingual
defenses") or when the form only names a tool ("multilingual BERT
embeddings"); such a facet is *discounted*, and the rejection says which. Two
whole-text checks follow: a concept used with a technique context and no
threat context anywhere is a benign sense ("prompt injection and freezing
strategies" in a fine-tuning method), and a text whose title names a
distinct neighbouring topic but not the facet, and which mentions that
neighbour at least as often, is adjacent (a jailbreak paper that says
"prompt injection" once). All of it is data in the lexicon.

Decision order, first match wins:

1. no title and no abstract              -> reject ``insufficient_text``
1b. the abstract is not written in English in the Latin alphabet
                                         -> reject ``paper_language:non_latin_script``
                                            or ``paper_language:not_english``
2. a facet unevidenced, its head word used in another sense
                                         -> reject ``sense_mismatch:<facet>``
3. a facet unevidenced and no abstract   -> reject ``insufficient_text``
4. a facet with occurrences, all discounted
                                         -> reject ``incidental_mention:<facet>``
                                            or ``future_work_only:<facet>``
5. a facet unevidenced                   -> reject ``missing_facet:<facet>``
6. a benign sense of a concept facet     -> reject ``benign_sense:<facet>``
7. a distinct neighbour dominates        -> reject ``adjacent_topic:<neighbour>``
8. semantic score below the floor        -> reject ``low_semantic_relevance``
9. otherwise                             -> admit ``relevant:strong``,
                                            ``relevant:conceptual``, or -- with
                                            no abstract -- ``relevant:title_only``

Only candidates that reach step 8 are scored, in one batch, so the model is
never loaded for a run whose candidates all fail the lexical checks.

**Paper language (gate v3, D-S1-LANG-01).** A corpus paper must be written
in English in the Latin alphabet; it may study any language. Step 1b judges
the abstract only (`app.ingestion.paper_language`) -- a title never proves a
paper's language. An abstract too short to judge, or none at all, is
``unverified``: the relevance verdict stands and `evidence["language"]`
records the status, so such an admission stays visible for review.

**Title-only candidates** are admitted only in the ``title_only`` tier, which
`rank_key` places below every candidate that had an abstract: a source cap
reaches them only once nothing better is left. Within a tier, candidates whose
titles evidence more facets rank first, then the semantic score -- the score
alone ranked a fine-tuning paper above a Tamil/Tanglish prompt-injection
study in run 1.

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

from app.ingestion.paper_language import LanguagePolicy
from app.ingestion.paper_language import assess as assess_language
from app.ingestion.topic import meaningful_terms

GATE_VERSION = "relevance-gate-v3"
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


def _alternatives(forms, suffix: str = r"(?:e?s)?(?!\w)") -> re.Pattern | None:
    """Any of `forms` from a word start; `suffix` closes each form."""
    if not forms:
        return None
    joined = "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{joined}){suffix}")


_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")


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
    technique_context: tuple[str, ...] = ()
    threat_context: tuple[str, ...] = ()
    distinct_neighbours: tuple[str, ...] = ()
    incidental_followers: tuple[str, ...] = ()
    component_followers: tuple[str, ...] = ()
    component_target_cues: tuple[str, ...] = ()
    query_forms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Lexicon:
    version: str
    concepts: tuple[Concept, ...]
    language_names: frozenset[str]
    future_work_markers: tuple[str, ...] = ()

    def concept(self, name: str) -> Concept | None:
        return next((c for c in self.concepts if c.name == name), None)


@lru_cache(maxsize=4)
def load_lexicon(path: Path = LEXICON_PATH) -> Lexicon:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    concepts = []
    for name, spec in raw["concepts"].items():
        sense = spec.get("sense") or {}
        benign = spec.get("benign_sense") or {}
        concepts.append(
            Concept(
                name=name,
                forms=tuple(normalize(form) for form in spec["forms"]),
                sense_head=sense.get("head"),
                same_sense_modifiers=frozenset(sense.get("same_sense_modifiers", ())),
                min_language_names=spec.get("min_language_names"),
                technique_context=tuple(
                    normalize(f) for f in benign.get("technique_context", ())
                ),
                threat_context=tuple(
                    normalize(f) for f in benign.get("threat_context", ())
                ),
                distinct_neighbours=tuple(spec.get("distinct_neighbours", ())),
                incidental_followers=tuple(
                    normalize(f) for f in spec.get("incidental_followers", ())
                ),
                component_followers=tuple(
                    normalize(f) for f in spec.get("component_followers", ())
                ),
                component_target_cues=tuple(
                    normalize(f) for f in spec.get("component_target_cues", ())
                ),
                # Sent to sources as typed: arXiv reads the hyphens.
                query_forms=tuple(spec.get("query_forms", ())),
            )
        )
    return Lexicon(
        version=raw["version"],
        concepts=tuple(concepts),
        language_names=frozenset(raw.get("language_names", ())),
        future_work_markers=tuple(
            normalize(m) for m in raw.get("future_work_markers", ())
        ),
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
    # The topic's own words this facet was read from.
    tokens: tuple[str, ...] = ()


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
                        Facet(
                            concept.name,
                            "concept",
                            concept.forms,
                            concept,
                            tuple(tokens[position:end]),
                        )
                    )
                position = end
                break
        else:
            word = tokens[position]
            if word not in seen:
                seen.add(word)
                facets.append(Facet(word, "word", (word,), None, (word,)))
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
        return self._then(
            "duplicate_promotion",
            admitted=True,
            reason_code="duplicate_of_admitted",
            reason=(
                f"Same paper as corpus paper {paper_id}, already admitted for this "
                f"topic; the gate's verdict on this copy alone was "
                f"'{self._original()['reason_code']}'."
            ),
            paper_id=paper_id,
        )

    def with_evidence(self, **extra) -> "RelevanceDecision":
        return replace(self, evidence={**self.evidence, **extra})

    def capped(self, source: str, limit: int) -> "RelevanceDecision":
        """Relevant, but the source's admission cap was already full."""
        score = "n/a" if self.score is None else f"{self.score:.2f}"
        original = self._original()["reason_code"]
        admitted_as = (
            f"Passed the relevance gate ({self.reason_code}, score {score})"
            if original == self.reason_code
            else f"Admitted as {self.reason_code} (the gate's own verdict: "
            f"'{original}', score {score})"
        )
        return self._then(
            "source_cap",
            admitted=False,
            reason_code="source_cap_reached",
            reason=(
                f"{admitted_as} but the {source} cap of {limit} was filled by "
                f"higher-scoring candidates."
            ),
            source=source,
            limit=limit,
        )

    def _verdict(self) -> dict:
        return {
            "admitted": self.admitted,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }

    def _original(self) -> dict:
        """The gate's own verdict: recorded at the first transition, else this."""
        return self.evidence.get("gate") or self._verdict()

    def _then(
        self, step: str, *, admitted: bool, reason_code: str, reason: str, **detail
    ) -> "RelevanceDecision":
        """A later decision about this candidate (Phase 1 closeout, 2026-10-12).

        `evidence["gate"]` keeps the gate's original verdict -- set at the
        first transition and never overwritten -- and `evidence["gate_history"]`
        lists every verdict in order: the gate's (step ``gate``), then each
        ``duplicate_promotion`` / ``source_cap``. Promoting a rejected copy and
        then capping it used to replace `gate` with the promotion, hiding why
        the gate had rejected it.
        """
        history = list(
            self.evidence.get("gate_history") or [{"step": "gate", **self._verdict()}]
        )
        history.append(
            {
                "step": step,
                "admitted": admitted,
                "reason_code": reason_code,
                "reason": reason,
                **detail,
            }
        )
        return RelevanceDecision(
            admitted=admitted,
            reason_code=reason_code,
            reason=reason,
            score=self.score,
            tier=self.tier,
            evidence={
                **self.evidence,
                "gate": self._original(),
                "gate_history": history,
            },
        )


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
        language_policy: LanguagePolicy | None = None,
    ) -> None:
        self.analysis = analysis
        self.language_policy = language_policy or LanguagePolicy()
        self._scorer = scorer
        self.semantic_floor = semantic_floor
        self.strong_threshold = strong_threshold
        self.model_name = model_name
        self._patterns = {
            facet.name: [_pattern(form) for form in facet.forms]
            for facet in analysis.facets
        }
        lexicon = analysis.lexicon
        self._names = [
            (name, _pattern(name)) for name in sorted(lexicon.language_names)
        ]
        self._future = _alternatives(lexicon.future_work_markers)
        facet_names = {facet.name for facet in analysis.facets}
        self._followers: dict[str, re.Pattern] = {}
        self._components: dict[str, tuple[re.Pattern, re.Pattern | None]] = {}
        self._benign: dict[str, tuple[re.Pattern, re.Pattern]] = {}
        self._neighbours: dict[str, list[tuple[str, list[re.Pattern]]]] = {}
        for facet in analysis.facets:
            concept = facet.concept
            if concept is None:
                continue
            if concept.incidental_followers:
                self._followers[facet.name] = _alternatives(
                    concept.incidental_followers
                )
            if concept.component_followers:
                # Target cues are word stems: "attack" for attacks/attacked.
                self._components[facet.name] = (
                    _alternatives(concept.component_followers),
                    _alternatives(concept.component_target_cues, suffix=r"\w*"),
                )
            if concept.technique_context and concept.threat_context:
                # Threat context is matched on word stems: "adversar" for
                # adversarial, "defen" for defense/defending.
                self._benign[facet.name] = (
                    _alternatives(concept.technique_context),
                    _alternatives(concept.threat_context, suffix=""),
                )
            neighbours = [
                lexicon.concept(name)
                for name in concept.distinct_neighbours
                if name not in facet_names
            ]
            self._neighbours[facet.name] = [
                (n.name, [_pattern(form) for form in n.forms]) for n in neighbours if n
            ]

    # -- evidence ----------------------------------------------------------

    def _facet_evidence(
        self, facet: Facet, sentences: list[str], future: list[bool]
    ) -> dict:
        """Which of `facet`'s forms occur, and which occurrences count.

        An occurrence does not count inside a future-work sentence, nor when
        the next word makes the form a tool's name ("multilingual BERT").
        `mentioned` is the v1 notion -- any occurrence at all -- and `present`
        is whether any occurrence counts.
        """
        followers = self._followers.get(facet.name)
        components = self._components.get(facet.name)
        matched: list[str] = []
        counted = False
        incidental: list[str] = []
        future_only: list[str] = []
        for form, pattern in zip(facet.forms, self._patterns[facet.name], strict=True):
            hits = []
            for sentence, in_future in zip(sentences, future, strict=True):
                for match in pattern.finditer(sentence):
                    following = sentence[match.end() : match.end() + 1] == " "
                    tool = bool(
                        followers is not None
                        and following
                        and followers.match(sentence, match.end() + 1)
                    )
                    if (
                        not tool
                        and components is not None
                        and following
                        and components[0].match(sentence, match.end() + 1)
                    ):
                        # A generic component ("multilingual embeddings") names
                        # the subject only when it is what is being attacked.
                        before = " ".join(sentence[: match.start()].split()[-5:])
                        tool = not (components[1] and components[1].search(before))
                    hits.append((in_future, tool))
            if not hits:
                continue
            matched.append(form)
            if any(not in_future and not tool for in_future, tool in hits):
                counted = True
            elif any(tool for _, tool in hits):
                incidental.append(form)
            else:
                future_only.append(form)
        evidence: dict = {"kind": facet.kind, "matched": matched}
        present = counted
        mentioned = bool(matched)
        # Which rule evidenced the facet (2026-10-12): a counted surface form,
        # or -- for a concept that allows it -- enough distinct language names.
        evidenced_by = "surface_form" if counted else None
        discounted = {"incidental": incidental, "future_work": future_only}
        if facet.kind == "concept":
            names: list[str] = []
            minimum = facet.concept.min_language_names
            if minimum:
                counted_names = []
                for name, pattern in self._names:
                    found = [
                        f
                        for s, f in zip(sentences, future, strict=True)
                        if pattern.search(s)
                    ]
                    if found:
                        names.append(name)
                        if not all(found):
                            counted_names.append(name)
                if not present and len(counted_names) >= minimum:
                    evidenced_by = "language_names"
                present = present or len(counted_names) >= minimum
                mentioned = mentioned or len(names) >= minimum
                discounted["future_work_language_names"] = [
                    name for name in names if name not in counted_names
                ]
            evidence["language_names"] = names
        evidence["present"] = present
        evidence["evidenced_by"] = evidenced_by
        evidence["mentioned"] = mentioned
        evidence["discounted"] = discounted
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

    def _sentences(self, title: str, abstract: str) -> tuple[list[str], list[bool]]:
        """The title, then the abstract's sentences, each normalized; and
        whether each is a future-work sentence."""
        sentences = [normalize(title)] if title else []
        if abstract:
            sentences += [s for s in _SENTENCE_END.split(normalize(abstract)) if s]
        future = [
            bool(self._future is not None and self._future.search(s)) for s in sentences
        ]
        return sentences, future

    def facets_evidenced(self, title: str | None, abstract: str | None) -> bool:
        """Cheap lexical check: is every facet mentioned? (ACL candidacy)

        Deliberately the broad v1 test -- any occurrence -- so a record the
        gate will reject as incidental or future-work-only still becomes a
        candidate and its rejection is recorded.
        """
        title = " ".join((title or "").split())
        abstract = " ".join((abstract or "").split())
        sentences, future = self._sentences(title, abstract)
        return all(
            self._facet_evidence(facet, sentences, future)["mentioned"]
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

    def query_groups(self) -> list[list[str]] | None:
        """The topic's facets as source query groups, synonyms OR'd within one.

        A concept facet contributes its validated `query_forms`; any other
        facet contributes the topic words it was read from, one group each,
        exactly as the topic-term query did. None when no facet has query
        forms -- the source then builds its query from the topic as before.
        """
        facets = self.analysis.facets
        if not any(f.concept is not None and f.concept.query_forms for f in facets):
            return None
        groups: list[list[str]] = []
        for facet in facets:
            if facet.concept is not None and facet.concept.query_forms:
                groups.append(list(facet.concept.query_forms))
            else:
                groups.extend([token] for token in facet.tokens)
        return groups

    def _count(self, patterns: list[re.Pattern], text: str) -> int:
        return sum(len(pattern.findall(text)) for pattern in patterns)

    def _lexical(self, title: str | None, abstract: str | None) -> _Lexical:
        title = " ".join((title or "").split())
        abstract = " ".join((abstract or "").split())
        text = f"{title}. {abstract}" if title and abstract else (title or abstract)
        normalized = normalize(f"{title} {abstract}")
        normalized_title = normalize(title)
        sentences, future = self._sentences(title, abstract)
        facets = {
            facet.name: self._facet_evidence(facet, sentences, future)
            for facet in self.analysis.facets
        }
        title_facets = (
            sum(
                self._facet_evidence(facet, sentences[:1], future[:1])["present"]
                for facet in self.analysis.facets
            )
            if title
            else 0
        )
        negative = {
            facet.name: self._other_senses(facet.concept, normalized)
            for facet in self.analysis.facets
            if facet.concept is not None and facet.concept.sense_head
        }
        language = assess_language(abstract, self.language_policy)
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
            "language": language,
            "facets": facets,
            "title_facets": title_facets,
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
        if language["status"] in ("non_latin_script", "not_english"):
            return reject(f"paper_language:{language['status']}", language["reason"])
        missing = [name for name, found in facets.items() if not found["present"]]
        if missing:
            mismatched = [
                name
                for name in missing
                if negative.get(name) and not facets[name]["mentioned"]
            ]
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
            discounted = [name for name in missing if facets[name]["mentioned"]]
            if discounted:
                name = discounted[0]
                found = facets[name]["discounted"]
                if found["incidental"]:
                    return reject(
                        f"incidental_mention:{name}",
                        f"'{name}' appears only as part of a tool's name "
                        f"({', '.join(found['incidental'])} followed by a model or "
                        f"encoder), not as what the text studies.",
                    )
                return reject(
                    f"future_work_only:{name}",
                    f"'{name}' appears only in sentences about future work.",
                )
            return reject(
                f"missing_facet:{missing[0]}",
                f"No evidence in the title or abstract for {listed}.",
            )
        for name, (technique, threat) in self._benign.items():
            techniques = sorted({m.group(0) for m in technique.finditer(normalized)})
            facets[name]["benign_sense"] = []
            if techniques and not threat.search(normalized):
                facets[name]["benign_sense"] = techniques
                return reject(
                    f"benign_sense:{name}",
                    f"'{name}' is used alongside {', '.join(techniques)} with no "
                    f"attack, threat or defense context: a technique, not the "
                    f"security topic.",
                )
        for name, neighbours in self._neighbours.items():
            own = self._patterns[name]
            if any(p.search(normalized_title) for p in own):
                continue
            for neighbour, patterns in neighbours:
                if not any(p.search(normalized_title) for p in patterns):
                    continue
                own_count = self._count(own, normalized)
                neighbour_count = self._count(patterns, normalized)
                if neighbour_count >= own_count:
                    evidence["adjacent"] = {
                        "neighbour": neighbour,
                        "neighbour_mentions": neighbour_count,
                        "facet": name,
                        "facet_mentions": own_count,
                    }
                    return reject(
                        f"adjacent_topic:{neighbour}",
                        f"The title names '{neighbour}' and not '{name}', and the "
                        f"text mentions '{neighbour}' {neighbour_count} time(s) "
                        f"against {own_count} for '{name}': a related but "
                        f"distinct topic.",
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
        facets = "; ".join(
            f"{name}: {', '.join(found['matched'] or found.get('language_names', []))}"
            for name, found in evidence["facets"].items()
        )
        if evidence["text_used"] == "title":
            return RelevanceDecision(
                True,
                "relevant:title_only",
                f"No abstract: admitted on the title alone ({facets}; semantic "
                f"score {score:.2f}), ranked below every candidate with an abstract.",
                score=score,
                tier="title_only",
                evidence=evidence,
            )
        tier = "strong" if score >= self.strong_threshold else "conceptual"
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
            "language_check": self.language_policy.describe(),
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


def rank_key(decision: RelevanceDecision) -> tuple:
    """How admitted candidates compete for a source's slots; higher first.

    A candidate with an abstract beats a title-only one; then the more topic
    facets its title evidences, the better (a title states what a paper is
    about); then the semantic score. A decision with no score sorts last.
    """
    evidence = decision.evidence or {}
    return (
        decision.tier != "title_only",
        evidence.get("title_facets", 0),
        decision.score if decision.score is not None else float("-inf"),
    )


def build_gate(topic: str, settings) -> RelevanceGate:
    """The production gate for one `/collect` run."""
    return RelevanceGate(
        analyze_topic(topic, load_lexicon()),
        CrossEncoderScorer(settings.relevance_model, settings.relevance_batch_size),
        semantic_floor=settings.relevance_semantic_floor,
        strong_threshold=settings.relevance_strong_threshold,
        model_name=settings.relevance_model,
        language_policy=LanguagePolicy(
            min_latin_fraction=settings.paper_language_min_latin_fraction,
            min_english_ratio=settings.paper_language_min_english_ratio,
            min_words=settings.paper_language_min_words,
            min_letters=settings.paper_language_min_letters,
        ),
    )
