"""Two pre-NLI gates that decide which statement pairs are worth comparing.

Stage 6 flags a contradiction when "one paper claims a method works where
another says it fails under the same setup" (README §8). Running an NLI model
over every cross-paper pair in a cluster does not test that sentence — it tests
whether a transformer emits a high `contradiction` probability, which it will
happily do for two statements that are not claims and are not about the same
thing. This module supplies the two properties the README sentence assumes and
the model cannot check:

* **claim-like** (D-06-10) — the statement asserts something about the world.
  Headings, recommendation bullets, ethics/availability disclaimers, section
  cross-references and future-work proposals are not claims, so nothing they
  are paired with can be a disagreement.
* **topically related** (D-06-11) — the two statements are about the same
  specific thing, measured by shared content terms.

A third, deliberately *weaker* predicate serves a different boundary.
`research_statement_rejection` (D-06-13) asks only "is this a research
statement, or is it document furniture?" and is applied to Stage 6's whole
input, before clustering. It is **not** `claim_rejection`: that one rejects
proposals, and future-work statements are proposals, so reusing it at the
clustering boundary would discard half of what README §8 Stage 6 exists to
cluster. Keeping the two separate is what lets contradiction detection stay
strict while clustering stays inclusive.

**Why not SPECTER2 cosine for the topic gate.** It was measured on the 20-paper
LLM-security corpus before this module was written: over the 239 cross-paper
pairs inside stored clusters, cosine ran from 0.836 to 0.975 with a median of
0.923, and the four known false-positive pairs sat at 0.89-0.93 — the middle of
the distribution. The *most* similar pairs of all were boilerplate
("DISCUSSION AND LIMITATIONS" against "This study has several limitations",
cosine 0.975). SPECTER2 is a document-level model, so on single sentences from
one field everything looks alike and a cosine gate would select for boilerplate.
Shared content terms separate the same pairs cleanly: the four false positives
share 0 or 1 term (Jaccard <= 0.042) while the gate's defaults admit 3.8% of
pairs.

Everything here is pure, deterministic and standard-library only: the same text
always yields the same verdict, which is what lets Stage 6 be reproducible.
"""

import re

# Function words plus the reporting vocabulary every paper in a scientific
# corpus shares ("study", "results", "approach"). Left in, they would make any
# two sentences from the same field look related, which is the failure mode the
# cosine gate had.
STOPWORDS = frozenset(
    # and reviewable; a 200-element literal would not be.
    """
    the a an and or but if then than that this these those of in on at to for
    with from by as is are was were be been being it its their our we us they
    he she his her not no nor so such can could may might will would shall
    should must do does did done have has had also very more most much many
    other others there here which who whom whose what when where why how all
    any both each few own same too only just about into over under between
    during before after above below out off up down again further once because
    while either neither however therefore thus hence moreover furthermore
    additionally although though even still yet per via using used use new
    study studies paper work works approach approaches method methods result
    results show shows shown demonstrate demonstrates provide provides present
    presents based across within without including include includes
    """.split()  # noqa: SIM905
)

# Three characters or more, letter-initial: the same alphabet Stage 8's label
# matching uses, so "injection" never matches inside "injectionless".
_WORD = re.compile(r"[a-z][a-z0-9-]{2,}")

# Words as they appear before lowercasing, used for length and opener checks.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'-]*")

# Longest suffix first, so "injections" loses "ions" rather than "s". The floor
# of four remaining characters keeps stems readable and stops "ed"/"es" from
# eating short words down to a fragment shared by unrelated terms, and is
# what keeps "ion" from turning "region" into "reg". Both "ions" and "ion"
# are listed so "injections" and "injection" reach the same stem.
_SUFFIXES = (
    "ization",
    "isation",
    "ations",
    "ation",
    "ions",
    "ion",
    "ing",
    "ies",
    "ers",
    "ed",
    "es",
    "s",
)

# A bullet glyph, a dash, or an enumerator: a list item, not a sentence. The
# replacement character is here because it is what a bullet out of a PDF often
# decodes to, and cluster 1's recommendation bullets arrived exactly that way.
_BULLET = re.compile(r"^[\s•●·�*+\-–—]|^\(?\d+[.)]|^[ivx]+[.)]", re.IGNORECASE)

# Recommendation openers. A bare infinitive with no subject is an instruction to
# the reader ("Utilize AI telemetry logging..."), which asserts nothing that
# another paper could contradict.
_IMPERATIVES = frozenset(
    """
    utilize use ensure consider implement apply monitor avoid adopt employ
    deploy perform conduct develop establish maintain integrate leverage
    restrict limit validate sanitize train enable provide include see note
    refer let add build create design explore investigate examine extend
    incorporate update review follow check test prefer keep treat
    """.split()  # noqa: SIM905
)

# Availability, licensing and ethics boilerplate. These sentences describe the
# artefact or the study's conduct, not its findings.
_DISCLAIMER = re.compile(
    r"no .{0,40}(is|are|was|were) (released|made available|provided)"
    r"|publicly available"
    r"|(is|are) used solely (to|for)"
    r"|solely to evaluate"
    r"|available (at|upon request|on request)"
    r"|we (do not|don."  # "don't" / "do not", apostrophe-agnostic
    r"t) release"
    r"|informed consent|institutional review board|\birb\b"
    r"|ethic(s|al)\b"
    r"|does not (reflect|represent) the view"
    r"|no harmful outputs"
)

# Proposals: statements of intent about work not yet done. A plan cannot
# contradict a finding.
_PROPOSAL = re.compile(
    r"^(future work|another area|future research|in future|we plan|we will|"
    r"we leave|we intend)"
    r"|future work (includes|is|will|should)"
    r"|another area for investigation"
    r"|(should|could|must) (be )?(explore|explored|investigat\w*|examined|"
    r"studied|addressed)"
    r"|remains? to be (explored|investigated|studied)"
    r"|leave \w[\w\s]{0,28} to future work"
    r"|is an area for"
)

# Descriptions of the literature rather than of this paper's own result.
_RELATED_WORK = re.compile(
    r"^(these|those|prior|previous|recent|several|many|other|earlier) "
    r"(studies|study|work|works|approaches|methods|papers|authors|research)\b"
)

# Cross-references and organizational sentences.
_STRUCTURAL = re.compile(
    r"^(section|chapter|appendix|table|figure|algorithm)\b"
    r"|provides? (a |an )?(discussion|overview|summary)"
    r"|is organized as follows"
    r"|concludes with"
    r"|this (sub)?section (reviews|summarizes|presents|describes|discusses)"
)

# NOTE: there is deliberately no "does this contain a finite verb" rule.
# One was written and removed: any closed list of verbs is a list of the
# verbs its author thought of, and this project's own hand-labelled genuine
# contradiction ("Cross-lingual transfer fully recovers accuracy for
# low-resource languages") was rejected by it because "recovers" was not on
# the list. English has no closed class of finite verbs, and distinguishing
# a third-person "recovers" from a plural noun needs a POS tagger this
# project does not carry. Losing a real disagreement is a worse failure than
# admitting a noun-phrase fragment, which `too_short` and `structural`
# already catch and which still has to clear the topic and NLI gates.

# Below this a statement is a heading, a caption or a fragment. Eight words is
# what separates "This study has several limitations." (five) from the shortest
# real finding in the calibration corpus.
MIN_CLAIM_WORDS = 8

# Defaults calibrated on the 20-paper corpus (see the module docstring): they
# reject all four known false-positive pairs and admit 3.8% of cross-paper
# pairs.
MIN_SHARED_TERMS = 3
MIN_TOPIC_JACCARD = 0.12


# --- Document furniture (D-06-13) -------------------------------------------
#
# Everything below identifies text that is part of a paper's apparatus rather
# than its research: the reference list, the author list, the acknowledgements,
# headings and cross-references. Stage 2/3 extracted all of these as
# `limitation` statements on the 20-paper corpus, Stage 5 embedded them, and
# Stage 6 clustered them into themes like "amir anastasopoulos andrew" and
# "association computational linguistics".

# Reference-list entries. A page range or a proceedings/preprint identifier is
# something no sentence of research prose contains. The word "pages" is
# required so that an ordinary range ("accuracy drops 10-15 points") is safe.
_BIBLIOGRAPHY = re.compile(
    r"\bpages?\s+\d+\s*[-\u2013\u2014]\s*\d+"
    r"|^(in )?(proceedings|findings) of the\b"
    r"|\barxiv[: ]\s*(preprint|\d)"
    r"|\bdoi\.org/|\bdoi:\s*10\."
    r"|\bvol\.\s*\d+|\bpp\.\s*\d+",
    re.IGNORECASE,
)

# Acknowledgements and funding. Every branch needs funding *context*: a bare
# "grant" or "awards" appears in ordinary ML prose ("the scoring function
# awards higher probability", "models cannot grant access"), and only the
# context separates the two. "reward" never matches -- the bounds see to it.
_ACKNOWLEDGEMENT = re.compile(
    r"\bwe (thank|gratefully acknowledge|acknowledge)\b"
    r"|\b(fellowships?|scholarships?)\b"
    r"|\bawards?\b.{0,40}\b(from|including|for (his|her|their) work)\b"
    r"|\b(received|receives|won|holds?|holder of)\b.{0,40}\bawards?\b"
    # "supported by" is deliberately absent: it is ordinary research prose
    # ("claims that are not supported by the retrieved context") and matching
    # it dropped a real hallucination limitation from this project's own
    # labelled set. A genuine funding sentence is caught by its funder
    # instead -- "a grant from", "fellowship", "award from".
    r"|\b(funded|financed) (in part )?by\b"
    r"|\bfunding (from|by|agency|agencies)\b"
    r"|\bfinancial support\b"
    r"|\b(under|research) grants?\b|\bgrants? (no\.|number|from)\b",
    re.IGNORECASE,
)

# One capitalized personal name, allowing initials ("Quoc V. Le") and
# hyphenated given names ("Rui-Jie Zhu").
_NAME = re.compile(r"^(?:[A-Z][A-Za-z.'\-]*)(?:\s+[A-Z][A-Za-z.'\-]*){0,3}$")

# How many comma-separated name-shaped segments make a bibliography author
# list. Three is the smallest run that does not also describe an ordinary
# sentence fragment.
_MIN_AUTHOR_SEGMENTS = 3


def _is_author_list(text: str) -> bool:
    """Whether this is a bibliography author list rather than a sentence.

    Requires **every** comma-separated segment to be name-shaped, not merely
    most of them. That strictness is what keeps real sentences that happen to
    list capitalized things ("We evaluate GPT-4o, Gemini 1.5-Pro, Qwen-Max, and
    DeepSeek-R1 ...") out of it: the segment carrying the verb is not a name,
    so the whole text is spared.
    """
    segments = [part.strip(" .;") for part in text.split(",")]
    segments = [part for part in segments if part]
    if len(segments) < _MIN_AUTHOR_SEGMENTS:
        return False
    # A trailing "and Graham Neubig" is still one name.
    normalized = [
        re.sub(r"^and\s+", "", part, flags=re.IGNORECASE) for part in segments
    ]
    return all(_NAME.match(part) for part in normalized)


# Cross-references, narrower than `_STRUCTURAL` (D-06-13). The structural verb
# is required, not just the opener: "Section VII provides a discussion of key
# findings" is apparatus, but "Table 4 shows that accuracy drops sharply for
# low-resource languages" is a finding, and an opener-only rule threw the second
# away with the first.
_CROSS_REFERENCE = re.compile(
    r"^(section|chapter|appendix)\s+[\divxlc.]+\s+"
    r"(provides?|presents?|describes?|discusses|reviews?|concludes?|"
    r"introduces?|summari[sz]es?|contains?|lists?)\b"
    r"|\bis organized as follows\b"
    r"|\bprovides? (a |an )?(discussion|overview|summary) of\b"
    r"|^this (sub)?section (reviews|summari[sz]es|presents|describes|discusses)\b"
    r"|\bconcludes with (a |the )?(discussion|summary|recommendations)\b",
    re.IGNORECASE,
)

# Ethics/consent boilerplate, narrower than `_DISCLAIMER` (D-06-13). The
# contradiction filter also rejects "publicly available" and any mention of
# "ethics", which is right for *that* question and wrong here: "Our dataset is
# publicly available and covers fourteen languages" is a contribution claim,
# and in an AI-safety corpus "the ethical implications of deploying these
# models remain underexplored" is a research gap, not furniture. Only
# unambiguous conduct boilerplate is matched.
_CONDUCT_BOILERPLATE = re.compile(
    r"\b(informed consent|institutional review board|\birb\b)\b"
    r"|does not (reflect|represent) the (view|opinion)"
    r"|no harmful outputs (were|are) (released|published)"
    r"|\bthis (paper|work|study) (was|has been) reviewed by\b",
    re.IGNORECASE,
)


# Below this a statement is a heading, a caption or a citation fragment. Lower
# than `MIN_CLAIM_WORDS` on purpose: clustering only needs enough words to
# carry a theme, while contradiction needs a full assertable claim.
MIN_RESEARCH_WORDS = 6


def research_statement_rejection(
    text: str | None, min_words: int = MIN_RESEARCH_WORDS
) -> str | None:
    """Why this is document furniture, or `None` when it is research.

    Applied to Stage 6's whole input (D-06-13). Deliberately weaker than
    `claim_rejection`: it does **not** reject proposals, imperatives,
    related-work descriptions or bullets, because a future-work statement is a
    proposal and README §8 Stage 6 clusters limitation *and future-work*
    statements. It rejects only text that is not a research statement at all.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "empty"

    words = _TOKEN.findall(stripped)
    if len(words) < min_words:
        return "too_short"

    # An all-uppercase run of words is a heading. Checked on the letters rather
    # than the tokens so an acronym inside a sentence ("the ASR reported for
    # GPT-4o") does not trigger it.
    letters = [character for character in stripped if character.isalpha()]
    if letters and all(character.isupper() for character in letters):
        return "heading"

    if _BIBLIOGRAPHY.search(stripped):
        return "bibliography"
    if _is_author_list(stripped):
        return "author_list"
    if _ACKNOWLEDGEMENT.search(stripped):
        return "acknowledgement"

    if _CROSS_REFERENCE.search(stripped):
        return "structural"
    if _CONDUCT_BOILERPLATE.search(stripped):
        return "disclaimer"
    return None


def is_research_statement(
    text: str | None, min_words: int = MIN_RESEARCH_WORDS
) -> bool:
    """Whether this statement is worth clustering as a theme."""
    return research_statement_rejection(text, min_words) is None


def _stem(word: str) -> str:
    """Strip one inflectional suffix so "attacks" and "attack" agree."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def content_terms(text: str | None) -> set[str]:
    """The stemmed, de-stopworded vocabulary of one statement."""
    if not text:
        return set()
    return {
        _stem(word) for word in _WORD.findall(text.lower()) if word not in STOPWORDS
    }


def claim_rejection(text: str | None, min_words: int = MIN_CLAIM_WORDS) -> str | None:
    """Why this statement is not a claim, or `None` when it is one.

    A reason rather than a bare boolean because it is what makes a dropped pair
    auditable: "cluster 1 lost its flag" is not a finding, "its only flagged
    pair rested on a disclaimer" is. The order of the checks is the order of
    confidence — cheap structural facts before vocabulary patterns.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "empty"
    if _BULLET.match(stripped):
        return "bullet"

    words = _TOKEN.findall(stripped)
    if len(words) < min_words:
        return "too_short"

    lowered = stripped.lower()
    if _STRUCTURAL.search(lowered):
        return "structural"
    if _RELATED_WORK.match(lowered):
        return "related_work"
    if _DISCLAIMER.search(lowered):
        return "disclaimer"
    if _PROPOSAL.search(lowered):
        return "proposal"

    # A leading adverb ("Continuously monitor ...") still fronts an imperative.
    head = [word.lower() for word in words[:2]]
    opener = head[1] if head[0].endswith("ly") and len(head) > 1 else head[0]
    if opener in _IMPERATIVES:
        return "imperative"
    return None


def is_claim_like(text: str | None, min_words: int = MIN_CLAIM_WORDS) -> bool:
    """Whether this statement asserts something another paper could deny."""
    return claim_rejection(text, min_words) is None


def topic_overlap(left: str | None, right: str | None) -> tuple[int, float]:
    """`(shared content terms, Jaccard)` for two statements.

    Both numbers are reported because they answer different questions: the
    count is "do these share enough substance to be about one thing", the ratio
    is "is that overlap most of what either says". A long paragraph and a short
    one can share three terms by accident; they rarely also overlap by ratio.
    """
    left_terms, right_terms = content_terms(left), content_terms(right)
    union = left_terms | right_terms
    if not union:
        return 0, 0.0
    shared = left_terms & right_terms
    return len(shared), len(shared) / len(union)


def topically_related(
    left: str | None,
    right: str | None,
    min_shared: int = MIN_SHARED_TERMS,
    min_jaccard: float = MIN_TOPIC_JACCARD,
) -> bool:
    """Whether two statements are about the same specific thing.

    Both thresholds must hold. Either alone is defeatable: two long sentences
    from one field clear the count on shared jargon, and two near-empty
    fragments clear the ratio on a single word.
    """
    shared, jaccard = topic_overlap(left, right)
    return shared >= min_shared and jaccard >= min_jaccard
