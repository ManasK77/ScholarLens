"""What a Mode B question asks for: one paper, a comparison, or a synthesis.

The sufficiency gate decides how many distinct papers an answer needs from
this. A fixed two-paper rule refused narrow questions one paper answers (all
twelve hits from the paper the question is about) while doing nothing against
off-topic questions, whose hits spread across papers anyway.

Deliberately simple and explainable: the question's own wording, plus whether
a name it uses is the system a retrieved paper's title introduces. No model.
In order:

* `comparison` -- a comparison word with two or more named systems.
* `single_paper` -- a definite singular reference to a paper or study ("the
  ... study", "this paper") and no plural one.
* `synthesis` -- the question asks across papers: plural research objects
  ("papers", "studies"), the literature, gaps, open problems, comparison or
  agreement words.
* `single_paper` -- a weaker singular reference ("the ... evaluation", "this
  benchmark"), or a name the question uses that a retrieved title introduces
  before its colon ("Sentra-Guard: A Real-Time ..."). A model a title merely
  mentions ("... Using GPT-4o") is no signal: a question about it may be broad.
* `unspecified` -- none of these. The gate then keeps the configured minimum:
  the conservative default when the scope cannot be read.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.interactive.claim_check import (
    LANGUAGES,
    mentions,
    names,
    normalize,
    salient_terms,
    stated,
)

SINGLE_PAPER = "single_paper"
COMPARISON = "comparison"
SYNTHESIS = "synthesis"
UNSPECIFIED = "unspecified"

# Matched on `normalize`d text: casefolded, hyphens folded to spaces.
_DEFINITE = r"\b(the|this|that)\s+(?:[\w'’]+\s+){0,6}?"
_SINGLE = re.compile(rf"{_DEFINITE}(paper|study|article|survey)\b")
# Nouns also used generically ("the evaluation of defenses"): they mark one
# paper only when nothing in the question asks across papers.
_SINGLE_WEAK = re.compile(
    rf"{_DEFINITE}(evaluation|experiment|benchmark|test suite|framework|"
    r"defen[cs]e|system)\b"
)
_PLURAL = re.compile(
    r"\b(papers|studies|articles|surveys|works|publications|literature|the field)\b|"
    r"\b(existing|prior|previous|other|recent) (work|research|findings)\b"
)
_ACROSS = re.compile(
    r"\b(across|gaps?|unsolved|open (problems?|questions?|challenges?)|"
    r"underexplored|under explored|unexplored|commonly|recurring|consensus|trends?)\b"
)
_COMPARE = re.compile(
    r"\b(compar\w*|versus|vs|contrast\w*|disagree\w*|agree\w*|contradict\w*)\b"
)
# Only with two named systems: "how do A and B differ".
_DIFFER = re.compile(r"\bdiffer\w*\b")


def _is_venue_like(name: str) -> bool:
    """A short all-caps acronym ("WMT", "ICML"): usually a venue or field term
    that passages about the work never spell out, so it is no subject."""
    return name.isalpha() and name.isupper() and len(name) <= 4


def _distinct_names(question: str) -> list[str]:
    """The systems, models and datasets the question names, venues excluded."""
    return [n for n in dict.fromkeys(names(question)) if not _is_venue_like(n)]


def question_scope(question: str, titles: Iterable[str] = ()) -> str:
    """The question's scope; `titles` are the retrieved papers' titles."""
    text = normalize(question)
    named = _distinct_names(question)
    if len(named) >= 2 and (_COMPARE.search(text) or _DIFFER.search(text)):
        return COMPARISON
    if _SINGLE.search(text) and not _PLURAL.search(text):
        return SINGLE_PAPER
    if _PLURAL.search(text) or _ACROSS.search(text) or _COMPARE.search(text):
        return SYNTHESIS
    if _SINGLE_WEAK.search(text):
        return SINGLE_PAPER
    # The system a paper introduces is named before its title's colon.
    introduced = [t.split(":", 1)[0] for t in titles if t and ":" in t]
    if introduced and any(stated(name, introduced) for name in named):
        return SINGLE_PAPER
    return UNSPECIFIED


def question_subjects(question: str) -> list[str]:
    """What the question is specifically about: the systems, models or datasets
    it names, and the languages or resource levels it names (numbers excluded)."""
    subjects = _distinct_names(question)
    terms = [g[0] for g in salient_terms(question) if not g[0].startswith("#")]
    # "non-English" also matches the "english" group; it is one subject.
    terms = [t for t in terms if not any(t != o and f" {t}" in f" {o}" for o in terms)]
    return subjects + terms


def _covers(subject: str, normalized: str, texts: Sequence[str]) -> bool:
    if subject == "non english":
        # Any named language other than English is a non-English one.
        return mentions((subject,), normalized) or any(
            mentions((language,), normalized)
            for language in LANGUAGES
            if language != "english"
        )
    group = next((g for g in salient_terms(subject) if g[0] == subject), None)
    return mentions(group, normalized) if group else stated(subject, texts)


def addresses_question(question: str, texts: Sequence[str]) -> bool:
    """Whether `texts` (an answer's claims, cited passages and titles) mention
    at least one of the question's subjects. True when it has none.

    Deliberately lenient: one subject is enough, so a partial answer about part
    of the question passes. What it refuses is an answer about something else
    entirely -- supported by its passage, but not about what was asked.
    """
    subjects = question_subjects(question)
    if not subjects:
        return True
    normalized = normalize(" \n ".join(texts))
    return any(_covers(subject, normalized, texts) for subject in subjects)


def uncovered_subjects(question: str, passages: Sequence[dict]) -> list[str]:
    """Names in the question that no passage (or its paper's title) states."""
    texts = [
        str(value)
        for item in passages
        for value in (item.get("text"), item.get("title"))
        if value
    ]
    return [name for name in _distinct_names(question) if not stated(name, texts)]
