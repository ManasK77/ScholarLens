"""Deterministic vetoes on Mode B claims, checked against each claim's own citations.

A valid citation id only proves the passage was retrieved. It does not prove
the passage says what the claim says. The LLM grounding check is the same
model that wrote the draft, and the 2026-10-13 live review found it passing a
claim about "low-resource languages" no cited passage mentions and an
English-focus finding about *the literature* restated as particular papers'
own limitation.

These checks can only **reject** a claim; none of them can establish support.
A claim is accepted only when the LLM verdict passes *and* no veto fires. The
vetoes are deliberately narrow, explainable patterns:

* `unsupported_absolute` -- exclusivity or universal wording ("the only",
  "no other", "all studies", "first to") that no cited passage states.
* `unsupported_term` -- a coverage term the cited passages never mention: a
  language name, a resource level ("low-resource"), a number or percentage.
* `misattributed` -- the claim attributes a finding to particular studies
  ("papers report", "restricted to"), but every cited sentence carrying the
  finding is about the literature in general ("most existing literature").
* `low_overlap` -- under half of the claim's content terms occur in its cited
  passages (or the cited papers' titles): a topical mention is not support. A
  heuristic veto only; enough overlap is never treated as proof.
* `polarity_mismatch` -- the passage clause that best matches a claim clause
  is negated and the claim clause is not, or the reverse ("does not fully
  eliminate" restated as "fully eliminates").
* `unsupported_name` -- the claim names a model, dataset or system (a token
  such as "GPT-4o", "BIPIA", "HarmBench-28K") that neither the cited passages
  nor the cited papers' titles mention.
* `unsupported_cause` -- the claim adds an explanation ("because ...", "due
  to ...") whose terms the cited passages do not carry.
* `unsupported_absence` -- the claim asserts that something is missing from
  the literature ("no study has ...", "has not been explored", "remains
  unexplored") but no cited passage states an absence. A gap a paper states is
  evidence; a gap inferred from what the retrieved passages leave out is not.
* `wrong_paper` -- the claim names a paper by its id ("Paper 5 reports ...")
  that none of its citations comes from. A reference to a cited paper is
  attribution, not a number the passage must state.
* `future_as_done` -- every cited passage is a future-work statement that
  states a plan ("Future work will address ..."), but the claim has no future
  or modal wording: a plan reported as an accomplishment.

`partial_total` -- a claim about the whole study states a figure that its
  cited passage shows to be one component of a labelled total (the numbers
  before "Total 7,892" sum to it), without scoping the figure or giving the
  total: a part reported as the whole.

Matching is deliberately forgiving where a mismatch would only be a spelling
difference: numbers are compared by value (`6,000` = `6000`, `0.70` = `0.7`,
`85 %` = `85%`), inflections the stemmer leaves apart still overlap
(`requir` / `require`), a closed compound matches its hyphenated form
(`ChatGPT` / `Chat-GPT`), and an acronym matches its expansion (`FPR` /
"false positive rate"). Forgiveness only ever withholds a veto.

Everything here is pure: text in, reason codes out. No model, no store.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.clustering.claims import content_terms

MIN_TERM_OVERLAP = 0.5
# A claim clause and its best passage clause must share this much content
# before a polarity difference between them is read as a contradiction.
MIN_POLARITY_OVERLAP = 0.5
# Shortest stem that may match a longer term it prefixes ("requir" / "require").
MIN_PREFIX_STEM = 4

_HYPHENS = re.compile(r"[‐-―−­-]+")
# Extracted text often runs sentences together ("... [15].Since ..."): a
# capital right after a full stop that follows a word, number, ")" or "]".
_SENTENCE = re.compile(r"(?<=[.!?])\s+|(?<=[a-z0-9\)\]][.!?])(?=[A-Z])")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# ", and" joins two clauses with their own polarity ("184 questions resulted in
# a jailbreak, and all the English versions do not lead to one").
_CLAUSE = re.compile(r"\b(?:but|however|whereas|although|while|yet)\b|[;:]|,\s+and\b")
_NEGATORS = frozenset({"not", "no", "never", "cannot", "neither", "nor", "none"})
# How many following words one negator governs.
NEGATION_SCOPE = 4
_CAUSE = re.compile(r"\b(?:because|due to|caused by|owing to|as a result of)\b")
_ACRONYM = re.compile(r"\b([A-Z]{2,6})s?\b")
_NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.‐-―−-][A-Za-z0-9]+)*")
# Field acronyms a claim may use without its passages spelling them out.
GENERIC_ACRONYMS = frozenset(
    {
        "ai", "api", "apis", "asr", "auc", "auprc", "auroc", "bleu", "cot", "cpu",
        "dl", "f1", "fnr", "fpr", "genai", "gpu", "gpus", "hitl", "id", "ids", "ipi",
        "llm", "llms", "ml", "mt", "nli", "nlp", "nmt", "ocr", "os", "pdf", "pia",
        "pias", "pmt", "qa", "rag", "sota", "tpr", "ui", "url", "urls",
    }
)  # fmt: skip

ABSOLUTE = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bthe only\b",
        r"\bonly (one|a single)\b",
        r"\bsole(ly)?\b",
        r"\bno other\b",
        r"\bnone of\b",
        r"\b(all|every) (stud(y|ies)|papers?|works?|defen[cs]es?|methods?|models?|languages?|systems?|llms?)\b",
        r"\balways\b",
        r"\bnever\b",
        r"\bfirst (to|study|work|paper|method|defen[cs]e)\b",
        r"\bexclusive(ly)?\b",
        r"\bunique(ly)?\b",
    )
)
# Evidence wording that states an absolute in other words. "none of the English
# versions" is what "all the English versions ... do not" says.
_ABSOLUTE_EQUIVALENTS = {
    r"\bnone of\b": (re.compile(r"\ball\b[^.;]{0,80}(?:\b(?:not|never)\b|n't\b)"),),
}

# A claim that something is absent from the literature. Matched on normalized
# text (hyphens folded to spaces: "under-explored" -> "under explored").
ABSENCE = tuple(
    re.compile(pattern)
    for pattern in (
        (
            r"\bno (prior |previous |existing |other |published )?(stud(y|ies)|"
            r"papers?|works?|research|evaluations?|benchmarks?|methods?|approaches|"
            r"defen[cs]es?)\b"
        ),
        (
            r"\b(has|have|had) (not|never) (yet )?been (\w+ )?(studied|explored|"
            r"examined|investigated|addressed|evaluated|tested|considered)\b"
        ),
        (
            r"\b(unexplored|under ?explored|under ?studied|unaddressed|overlooked|"
            r"unstudied|uninvestigated)\b"
        ),
        r"\black(s|ing)? (of )?(any )?(research|studies|work|evaluations?|attention)\b",
        r"\b(little|limited) (prior )?(research|work|attention)\b",
    )
)
# "Paper 3", "paper_id 3", "paper #3": the prompt shows every passage's paper_id.
_PAPER_REF = re.compile(r"\bpaper(?:[ _-]?id)?\s*[#:]?\s*(\d+)\b", re.IGNORECASE)
# A sentence that reports cited work: a reference marker ("[13], [16]", "Li et
# al.") with no first-person or this-paper wording of its own.
_CITATION_MARKER = re.compile(r"\[\s*\d+(?:\s*[, ]\s*\d+)*\s*\]|\bet al\b")
_OWN_WORK = re.compile(r"\b(we|our|ours|us|this (paper|study|work|article))\b")
# Wording that keeps a plan a plan: a claim with none of it, citing only
# future-work statements, reports as done what the paper only intends.
_FUTURE = re.compile(
    r"\b(future|will|would|could|may|might|plans?|planned|propos\w*|intend\w*|"
    r"aims? to|should|can be|to be|further|next|extend\w*|explor\w*)\b"
)
# A passage that itself states a plan. Required as well as the passage type:
# Stage 3 types some result sentences `future_work` ("Evaluation on
# HarmBench-28K yielded 99.98% accuracy"), and those support results.
_PLAN = re.compile(
    r"\bfuture (work|research|directions?|studies)\b|\bwill (\w+ )?(address|explore|"
    r"investigate|extend|study|focus|examine|evaluate|consider)\b|\bwe (plan|intend|"
    r"aim) to\b|\bremains? to be\b|\bleft for future\b"
)

# A claim that speaks for the study as a whole ("The study generated N ...",
# "This paper analyzed N ...", "Paper 3 evaluated N ..."), as opposed to one
# that reports a count for some part of it.
_WHOLE_STUDY = re.compile(
    r"^(?:the|this)\s+(?:[\w'’-]+\s+){0,3}?(?:study|paper|work|research|experiment|"
    r"evaluation|dataset|benchmark|corpus|authors?)\b|^paper\s*#?\d+\b",
    re.IGNORECASE,
)
# Wording that scopes a figure to a part (or gives its base), so it is not
# presented as the whole.
_SCOPED = re.compile(
    r"\b(rq\s?\d+|research questions?|subsets?|portion|part of|of which|out of|"
    r"of the|only|among|per|each|sampled?|selected|alone|including)\b"
)
# A total a passage labels: "Total 7,892", "a total of 7,892", "7,892 ... in total".
_LABELLED_TOTAL = re.compile(
    r"\btotal(?:\s+of)?\s+(\d+)(?![\d.%])|(?<![\d.])(\d+)(?:\s+[a-z&]+){0,4}?\s+in total\b"
)
_INTEGER = re.compile(r"(?<![\d.])\d+(?![\d.%])")
# How far before a labelled total its components are looked for, and the most
# components considered: bounded, so the subset search stays trivial.
_TOTAL_WINDOW = 600
_MAX_COMPONENTS = 24

# Language names a claim or question may use, each its own term group.
LANGUAGES = (
    "english", "hindi", "urdu", "tamil", "telugu", "marathi", "nepali",
    "sinhala", "chinese", "mandarin", "cantonese", "japanese", "korean",
    "arabic", "hebrew", "turkish", "russian", "ukrainian", "polish",
    "german", "french", "spanish", "portuguese", "italian", "dutch",
    "greek", "swahili", "yoruba", "hausa", "amharic", "vietnamese",
    "thai", "indonesian", "malay", "tagalog", "bangla", "persian",
)  # fmt: skip
# Coverage vocabulary whose presence in a claim must be matched by the cited
# text. Synonyms share a group. Matched on whole words after hyphen folding.
_TERM_GROUPS = (
    ("low resource",),
    ("high resource",),
    ("non english",),
    ("monolingual",),
    ("bilingual",),
    ("cross lingual", "crosslingual"),
    ("bangla", "bengali"),
    ("persian", "farsi"),
    *((language,) for language in LANGUAGES if language not in ("bangla", "persian")),
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_LITERATURE = re.compile(
    r"\b(most|much|many) (of the )?(existing|prior|previous|publicly documented|"
    r"current|recent)\b|\bprior (work|research|studies)\b|\bexisting (literature|"
    r"work|research|studies|evaluations?)\b|\bprevious (work|studies|research)\b|"
    r"\bthe literature\b"
)
_ATTRIBUTION = re.compile(
    r"\b(papers?|stud(y|ies)|authors|they|their|these works?|each work)\b[^.]*\b("
    r"reports?|reported|notes?|noted|acknowledges?|acknowledged|states?|stated|"
    r"admits?|observes?|observed|finds?|found|shows?|showed|shown|demonstrates?|"
    r"demonstrated|suggests?|suggested|highlights?|argues?)\b|"
    r"\b(restricted|limited|confined) to\b|\bevaluat\w* only\b"
)


def normalize(text: object) -> str:
    """Casefolded, hyphen variants folded to spaces, thousands separators
    dropped (`6,000` -> `6000`), whitespace collapsed."""
    folded = _HYPHENS.sub(" ", _THOUSANDS.sub("", str(text or ""))).casefold()
    return " ".join(folded.split())


def _joined(text: object) -> str:
    """`normalize`, but hyphens close the compound (`Chat-GPT` -> `chatgpt`)."""
    folded = _HYPHENS.sub("", _THOUSANDS.sub("", str(text or ""))).casefold()
    return " ".join(folded.split())


def _has(term: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def _number_value(token: str) -> str:
    """A number's value as text: `0.70` -> `0.7`, `10.0` -> `10`, `007` -> `7`."""
    whole, _, fraction = token.partition(".")
    fraction = fraction.rstrip("0")
    whole = whole.lstrip("0") or "0"
    return f"{whole}.{fraction}" if fraction else whole


def _numbers(normalized: str) -> set[str]:
    return {_number_value(n) for n in _NUMBER.findall(normalized)}


def sentences(text: str) -> list[str]:
    return [s for s in (part.strip() for part in _SENTENCE.split(text or "")) if s]


def salient_terms(claim: str) -> list[tuple[str, ...]]:
    """The coverage terms a claim asserts: term groups plus numbers.

    A number is the one-element group `("#<value>",)`, matched by value.
    """
    text = normalize(claim)
    found = [group for group in _TERM_GROUPS if any(_has(t, text) for t in group)]
    found.extend((f"#{value}",) for value in sorted(_numbers(text)))
    return found


def mentions(group: tuple[str, ...], normalized: str) -> bool:
    if group[0].startswith("#"):
        return group[0][1:] in _numbers(normalized)
    return any(_has(term, normalized) for term in group)


def _acronyms(texts: Iterable[str]) -> set[str]:
    return {m.group(1).casefold() for t in texts for m in _ACRONYM.finditer(t)}


def _words(normalized: str) -> list[str]:
    return re.findall(r"[a-z][a-z0-9]*", normalized)


def _spelled_out(acronym: str, words: list[str]) -> list[str] | None:
    """The words of `words` whose initials spell `acronym`, if any run does."""
    size = len(acronym)
    for start in range(len(words) - size + 1):
        window = words[start : start + size]
        if "".join(w[0] for w in window) == acronym:
            return window
    return None


def _term_matches(term: str, evidence_terms: set[str]) -> bool:
    if term in evidence_terms:
        return True
    for other in evidence_terms:
        short, long = sorted((term, other), key=len)
        if len(short) >= MIN_PREFIX_STEM and long.startswith(short):
            return True
    return False


def _coverage(
    claim: str,
    evidence_texts: Sequence[str],
    acronyms: set[str] | None = None,
    vouched: set[str] = frozenset(),
) -> float | None:
    """Share of the claim's content terms its evidence carries; `None` under 2.

    Evidence terms are taken with hyphens both split and joined; the claim is
    read both ways too and the better reading counts. Acronyms bridge in
    either direction when `acronyms` (from the original-case texts) is given.
    `vouched` terms count as carried (the words of a name a title states).
    """
    evidence_norm = [normalize(t) for t in evidence_texts]
    evidence_terms = set().union(
        *(content_terms(t) for t in evidence_norm),
        *(content_terms(_joined(t)) for t in evidence_texts),
    )
    evidence_words = [w for t in evidence_norm for w in _words(t)]
    # Only a word the claim writes in capitals is read as an acronym: "tea"
    # is not one, even where "table evaluates all" spells it.
    claim_acronyms = _acronyms([claim])
    best: float | None = None
    for reading in (normalize(claim), _joined(claim)):
        terms = content_terms(reading)
        if len(terms) < 2:
            continue
        covered = {t for t in terms if t in vouched or _term_matches(t, evidence_terms)}
        if acronyms is not None:
            # A claim acronym the evidence spells out ("fpr" / "false positive rate").
            covered |= {
                t
                for t in terms
                if t in claim_acronyms and _spelled_out(t, evidence_words)
            }
            # Claim words that spell out an evidence acronym.
            claim_words = _words(reading)
            for acronym in acronyms:
                window = _spelled_out(acronym, claim_words)
                if window:
                    covered |= {
                        t for t in terms if any(w.startswith(t) for w in window)
                    }
        share = len(covered) / len(terms)
        best = share if best is None else max(best, share)
    return best


def _clauses(text: str) -> list[str]:
    return [
        c.strip()
        for s in sentences(text)
        for c in _CLAUSE.split(normalize(s))
        if c and c.strip()
    ]


def _negators(words: list[str]) -> list[int]:
    """Positions of negating words. A bare trailing "no" is a label value
    ("intent-to-follow no"), not a negation, so "no" needs a following word."""
    return [
        i
        for i, w in enumerate(words)
        if w in _NEGATORS and (w != "no" or i + 1 < len(words))
    ]


def _negated(clause: str, terms: set[str] | None = None) -> bool:
    """Whether `clause` negates something; with `terms`, one of those terms.

    A negator governs the next `NEGATION_SCOPE` words. "near-zero drift and no
    significant effect" negates "significant effect", not "drift".
    """
    words = _words(clause.replace("n't", " not"))
    positions = _negators(words)
    if terms is None:
        return bool(positions)
    return any(
        _term_matches(_stem_word(w), terms)
        for i in positions
        for w in words[i + 1 : i + 1 + NEGATION_SCOPE]
    )


def _stem_word(word: str) -> str:
    stems = content_terms(word)
    return next(iter(stems)) if stems else word


def _polarity_mismatch(claim: str, cited_texts: Sequence[str]) -> bool:
    """A claim clause whose best-matching passage clauses all differ in polarity.

    The passage side counts as negated only when a negator governs one of the
    claim clause's own content terms.
    """
    evidence_clauses = [c for t in cited_texts for c in _clauses(t)]
    if not evidence_clauses:
        return False
    for clause in _clauses(claim):
        scored = [(_coverage(clause, [e]), e) for e in evidence_clauses]
        scored = [(s, e) for s, e in scored if s is not None]
        if not scored:
            continue
        top = max(s for s, _ in scored)
        if top < MIN_POLARITY_OVERLAP:
            continue
        terms = content_terms(clause)
        negated = _negated(clause)
        # An elliptic negation ("..., while their English versions did not")
        # governs none of its clause's own terms, so the passage side is read
        # the same way: negated anywhere, not on those terms.
        elliptic = negated and not _negated(clause, terms)
        if all(
            (_negated(e) if elliptic else _negated(e, terms)) != negated
            for s, e in scored
            if s == top
        ):
            return True
    return False


def names(claim: str) -> list[str]:
    """Tokens that look like a specific model, dataset or system name.

    A trailing all-lowercase part is a description, not the name:
    "GPT-4-based" names "GPT-4".
    """
    names = []
    for raw in _NAME_TOKEN.findall(claim):
        parts = _HYPHENS.split(raw)
        while len(parts) > 1 and parts[-1].isalpha() and parts[-1].islower():
            parts.pop()
        token = "-".join(parts)
        letters = [c for c in token if c.isalpha()]
        uppers = sum(c.isupper() for c in token)
        inner_upper = any(c.isupper() for c in token[1:])
        mixed_digit = any(c.isdigit() for c in token) and bool(letters)
        looks_named = (inner_upper and uppers >= 2) or mixed_digit
        if looks_named and normalize(token) not in GENERIC_ACRONYMS:
            names.append(token)
    return names


def stated(name: str, texts: Sequence[str]) -> bool:
    """Whether any of `texts` states `name` (as a whole word, hyphens either way)."""
    spaced = " \n ".join(normalize(t) for t in texts)
    joined = " \n ".join(_joined(t) for t in texts)
    return _has(normalize(name), spaced) or _has(_joined(name), joined)


def _unsupported_name(
    claim: str, cited_texts: Sequence[str], titles: Sequence[str]
) -> bool:
    return any(not stated(name, [*cited_texts, *titles]) for name in names(claim))


def _title_vouched_terms(claim: str, titles: Sequence[str]) -> set[str]:
    """Content terms of the claim's names that a cited paper's title states.

    A passage rarely names its own system ("the system achieved ..."), so
    "Sentra-Guard" in a claim is carried by the paper's title. Nothing else in
    a title counts: every title here says "prompt injection".
    """
    terms: set[str] = set()
    for name in names(claim):
        if titles and stated(name, titles):
            terms |= content_terms(normalize(name)) | content_terms(_joined(name))
    return terms


def _unsupported_cause(claim: str, cited_texts: Sequence[str]) -> bool:
    for sentence in sentences(claim):
        normalized = normalize(sentence)
        match = _CAUSE.search(normalized)
        if not match:
            continue
        share = _coverage(normalized[match.end() :], cited_texts)
        if share is not None and share < MIN_TERM_OVERLAP:
            return True
    return False


def _literature_level(sentence: str) -> bool:
    """Whether a normalized passage sentence is about other work, not the paper's own."""
    if _LITERATURE.search(sentence):
        return True
    return bool(_CITATION_MARKER.search(sentence)) and not _OWN_WORK.search(sentence)


def _absolute_unsupported(pattern: re.Pattern, text: str, evidence: str) -> bool:
    if not pattern.search(text) or pattern.search(evidence):
        return False
    return not any(
        e.search(evidence) for e in _ABSOLUTE_EQUIVALENTS.get(pattern.pattern, ())
    )


def _is_component_sum(target: int, others: list[int]) -> bool:
    """Whether some non-empty subset of `others` sums to exactly `target`."""
    if target <= 0:
        return False
    reachable = {0}
    for value in others[:_MAX_COMPONENTS]:
        reachable |= {r + value for r in reachable if r + value <= target}
    return target in reachable and target != 0


def _partial_total(claim: str, cited_texts: Sequence[str]) -> bool:
    """A whole-study claim stating a figure that is one row of a labelled total.

    A cited passage labels a total M and the integers before it include the
    claim's figure N plus other figures summing to M - N: N is one component of
    M. Vetoed unless the claim scopes N ("in RQ1", "of which", "out of") or
    states M itself.
    """
    if not _WHOLE_STUDY.search(claim.strip()):
        return False
    text = normalize(claim)
    if _SCOPED.search(text):
        return False
    claimed = {int(n) for n in _INTEGER.findall(text) if len(n) >= 2}
    if not claimed:
        return False
    for passage in cited_texts:
        body = normalize(passage)
        for match in _LABELLED_TOTAL.finditer(body):
            total = int(match.group(1) or match.group(2))
            if total in claimed:
                continue
            window = body[max(0, match.start() - _TOTAL_WINDOW) : match.start()]
            numbers = [int(n) for n in _INTEGER.findall(window) if len(n) >= 2]
            for figure in claimed:
                if figure >= total or figure not in numbers:
                    continue
                others = list(numbers)
                others.remove(figure)
                if _is_component_sum(total - figure, [n for n in others if n < total]):
                    return True
    return False


def check_claim(
    claim: str,
    cited_texts: Iterable[str],
    titles: Iterable[str] = (),
    paper_ids: Iterable[int] | None = None,
    sections: Iterable[str | None] | None = None,
) -> list[str]:
    """Reason codes for every veto that fires; `[]` means no veto (not proof).

    `titles` are the cited papers' titles. They can only vouch for a name the
    claim uses (and that name's own words in the overlap); never for coverage
    terms, absolutes, polarity or any other content a passage has to state.

    `paper_ids` are the cited papers' ids. When given, a "Paper N" reference
    to one of them is attribution and is left out of the term and overlap
    checks; a reference to any other paper is `wrong_paper`.

    `sections` are the cited passages' types (`paragraph`, `limitation`,
    `future_work`, ...), used only by `future_as_done`.
    """
    cited_texts = [str(t) for t in cited_texts]
    titles = [str(t) for t in titles if t]
    cited = [normalize(t) for t in cited_texts]
    whole_claim = claim
    evidence = " \n ".join(cited)
    reasons: list[str] = []
    # "Paper 1 observes ..." still attributes the finding to a paper: the
    # attribution check reads the reference as "paper", while the term and
    # overlap checks below leave it out entirely.
    attribution_text = normalize(_PAPER_REF.sub("paper", claim))
    if paper_ids is not None:
        cited_papers = {int(p) for p in paper_ids}
        if any(int(m.group(1)) not in cited_papers for m in _PAPER_REF.finditer(claim)):
            reasons.append("wrong_paper")
        claim = _PAPER_REF.sub(" ", claim)
    text = normalize(claim)

    if any(_absolute_unsupported(p, text, evidence) for p in ABSOLUTE):
        reasons.append("unsupported_absolute")

    # Digits inside a name the passages or titles state ("Llama-3-Guard") are
    # part of the name, not a figure the passages must repeat.
    term_text = claim
    for name in names(claim):
        if stated(name, [*cited_texts, *titles]):
            term_text = term_text.replace(name, " ")
    terms = salient_terms(term_text)
    if any(not mentions(group, evidence) for group in terms):
        reasons.append("unsupported_term")

    if _ATTRIBUTION.search(attribution_text):
        # Split before normalizing: run-together sentences ("x.In y") are
        # found by the capital letter, which casefolding removes.
        cited_sentences = [normalize(s) for t in cited_texts for s in sentences(t)]
        for group in terms:
            carrying = [s for s in cited_sentences if mentions(group, s)]
            if carrying and all(_literature_level(s) for s in carrying):
                reasons.append("misattributed")
                break

    share = _coverage(
        claim, cited_texts, _acronyms(cited_texts), _title_vouched_terms(claim, titles)
    )
    if share is not None and share < MIN_TERM_OVERLAP:
        reasons.append("low_overlap")

    if _polarity_mismatch(claim, cited_texts):
        reasons.append("polarity_mismatch")
    if _unsupported_name(claim, cited_texts, titles):
        reasons.append("unsupported_name")
    if _unsupported_cause(claim, cited_texts):
        reasons.append("unsupported_cause")
    if any(p.search(text) for p in ABSENCE) and not any(
        p.search(evidence) for p in ABSENCE
    ):
        reasons.append("unsupported_absence")
    if _partial_total(whole_claim, cited_texts):
        reasons.append("partial_total")
    sections = [s for s in sections or ()]
    if (
        sections
        and len(sections) == len(cited)
        and all(s == "future_work" for s in sections)
        and all(_PLAN.search(t) for t in cited)
        and not _FUTURE.search(text)
    ):
        reasons.append("future_as_done")
    return reasons


def check_claims(
    claims: list[dict], evidence_by_id: dict[str, dict]
) -> list[list[str]]:
    """`check_claim` for each claim against its *own* cited passages and the
    titles and ids of the papers they come from."""
    results = []
    for claim in claims:
        cited = [
            evidence_by_id[i]
            for i in claim.get("evidence_ids", [])
            if i in evidence_by_id
        ]
        results.append(
            check_claim(
                str(claim.get("text", "")),
                [str(item.get("text", "")) for item in cited],
                list(dict.fromkeys(str(i["title"]) for i in cited if i.get("title"))),
                [
                    i["paper_id"]
                    for i in cited
                    if isinstance(i.get("paper_id"), int)
                    and not isinstance(i.get("paper_id"), bool)
                ],
                [item.get("source_type") for item in cited],
            )
        )
    return results


def answer_from_claims(claims: list[dict]) -> str:
    """The user-visible answer of a grounded result: its checked claims only."""
    parts = []
    for claim in claims:
        text = " ".join(str(claim.get("text", "")).split())
        if text:
            parts.append(text if text[-1] in ".!?" else f"{text}.")
    return " ".join(parts)


def uncovered_sentences(answer: str, claims: list[dict]) -> int:
    """How many draft-answer sentences no claim represents (diagnostic only)."""
    claim_terms = content_terms(
        normalize(" ".join(str(c.get("text", "")) for c in claims))
    )
    count = 0
    for sentence in sentences(answer):
        terms = content_terms(normalize(sentence))
        if terms and len(terms & claim_terms) / len(terms) < MIN_TERM_OVERLAP:
            count += 1
    return count
