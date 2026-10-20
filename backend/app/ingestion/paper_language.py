"""The S1 paper-language check (D-S1-LANG-01).

The corpus holds research papers *written* in English, in the Latin alphabet.
A paper may study any language -- an English paper about prompt injection in
Tamil is in scope -- so the check judges the language the text is written in,
never the languages it names.

**Evidence.** Only the abstract. A title is a handful of words, often English
even when the paper is not, so it never proves a paper's language. At
collection time the abstract is the only running text available: the gate
runs before any PDF is fetched.

**Two measurements, both deterministic and dependency-free:**

* *Script* -- the share of the abstract's letters that are Latin, by Unicode
  character name. Greek letters and mathematical alphanumerics are neutral
  (English papers use them as symbols), and digits, punctuation and math
  operators are not letters, so "ε ≤ 0.05" costs nothing. A few quoted Tamil
  or Hindi words leave an English abstract well above the floor; a paper
  written in Tamil script, with an English model name or two, falls below it.
* *English* -- the share of the abstract's words that are distinctively
  English function words ("the", "of", "and", "with", "we", ...). English
  prose runs at roughly 20-30%; French, Spanish, German or Turkish written in
  the Latin alphabet stays under a few percent. Words that are common in
  other Latin-script languages ("in", "a", "is", "was", "an", "on") are not
  markers.

**Conservative by construction.** Text too short to measure -- no abstract,
or fewer letters/words than the policy needs -- is ``unverified``: it is not
rejected and not reported as English. The caller records the status, so an
admitted paper whose language is unverified stays visible for review.

Statuses: ``english``, ``non_latin_script``, ``not_english``, ``unverified``.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

LANGUAGE_CHECK_VERSION = "paper-language-v1"

# Distinctively English function words. Kept free of forms that are frequent
# words in other Latin-script languages (German "was"/"an", Dutch "is",
# French "on", Italian "in", Spanish "a").
ENGLISH_MARKERS = frozenset(
    {
        "the",
        "of",
        "and",
        "to",
        "that",
        "for",
        "with",
        "we",
        "this",
        "these",
        "those",
        "are",
        "from",
        "by",
        "which",
        "our",
        "be",
        "been",
        "has",
        "have",
        "were",
        "their",
        "they",
        "than",
        "can",
        "not",
        "into",
        "such",
        "also",
        "between",
        "show",
        "shows",
        "however",
        "while",
        "when",
        "how",
        "what",
        "it",
        "its",
        "more",
        "only",
        "through",
        "across",
        "against",
        "without",
        "within",
        "both",
        "each",
        "other",
        "about",
    }
)
# Scripts whose letters English papers use as symbols.
_NEUTRAL_SCRIPTS = frozenset({"GREEK", "MATHEMATICAL"})
_WORD = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True)
class LanguagePolicy:
    """Thresholds for `assess`; production values come from Settings."""

    # Below this share of Latin letters the text is in another script.
    min_latin_fraction: float = 0.5
    # Below this share of English marker words the text is not English.
    min_english_ratio: float = 0.10
    # Fewer words than this and the language is not judged.
    min_words: int = 20
    # Fewer (non-neutral) letters than this and the script is not judged.
    min_letters: int = 40

    def describe(self) -> dict:
        """What a collection run records about the check that judged it."""
        return {
            "version": LANGUAGE_CHECK_VERSION,
            "basis": "abstract",
            "min_latin_fraction": self.min_latin_fraction,
            "min_english_ratio": self.min_english_ratio,
            "min_words": self.min_words,
            "min_letters": self.min_letters,
        }


def _script(char: str) -> str:
    name = unicodedata.name(char, "")
    return name.split(" ", 1)[0] if name else "UNKNOWN"


def assess(abstract: str | None, policy: LanguagePolicy | None = None) -> dict:
    """Judge the language `abstract` is written in. See the module docstring."""
    policy = policy or LanguagePolicy()
    text = " ".join((abstract or "").split())
    result: dict = {
        "version": LANGUAGE_CHECK_VERSION,
        "basis": "abstract" if text else "none",
        "letters": 0,
        "latin_fraction": None,
        "non_latin_scripts": [],
        "words": 0,
        "english_marker_ratio": None,
    }

    def verdict(status: str, rule: str, reason: str) -> dict:
        """`rule` names the check that decided, for the audit trail."""
        return {**result, "status": status, "rule": rule, "reason": reason}

    if not text:
        return verdict(
            "unverified",
            "no_abstract",
            "No abstract: a title alone does not establish the paper's language.",
        )

    scripts = Counter(_script(char) for char in text if char.isalpha())
    for neutral in _NEUTRAL_SCRIPTS:
        scripts.pop(neutral, None)
    letters = sum(scripts.values())
    latin = scripts.pop("LATIN", 0)
    result["letters"] = letters
    result["non_latin_scripts"] = [name for name, _ in scripts.most_common(3)]
    if letters:
        result["latin_fraction"] = round(latin / letters, 3)

    words = [word.lower() for word in _WORD.findall(text)]
    result["words"] = len(words)
    if words:
        markers = sum(word in ENGLISH_MARKERS for word in words)
        result["english_marker_ratio"] = round(markers / len(words), 3)

    if letters < policy.min_letters:
        return verdict(
            "unverified",
            "min_letters",
            f"The abstract is too short to judge its script ({letters} letters; "
            f"{policy.min_letters} needed).",
        )
    if latin / letters < policy.min_latin_fraction:
        return verdict(
            "non_latin_script",
            "min_latin_fraction",
            f"Only {result['latin_fraction']:.0%} of the abstract's letters are "
            f"Latin (mostly {', '.join(result['non_latin_scripts'])}): the paper "
            f"is not written in the Latin alphabet.",
        )
    if len(words) < policy.min_words:
        return verdict(
            "unverified",
            "min_words",
            f"The abstract is too short to judge its language ({len(words)} "
            f"words; {policy.min_words} needed).",
        )
    if result["english_marker_ratio"] < policy.min_english_ratio:
        return verdict(
            "not_english",
            "min_english_ratio",
            f"Only {result['english_marker_ratio']:.1%} of the abstract's words "
            f"are English function words (at least "
            f"{policy.min_english_ratio:.0%} expected): the abstract is not "
            f"written in English.",
        )
    return verdict(
        "english",
        "min_english_ratio",
        f"The abstract is in Latin script ({result['latin_fraction']:.0%} of "
        f"letters) and {result['english_marker_ratio']:.1%} of its words are "
        f"English function words.",
    )
