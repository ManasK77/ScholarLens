"""Topic → search terms (D-QUERY-02), shared by the arXiv and ACL sources.

D-QUERY-01 sent a topic to arXiv as one all-fields *phrase*
(`all:"multilingual prompt injection"`) and matched it against the Anthology
export as one literal substring. Both demand the words adjacent, in the order
typed, inside a single field — far narrower than what a researcher means by a
topic. A `/collect` run for "Multilingual prompt injection" returned nothing
from either source while Semantic Scholar, which tokenizes the query itself,
returned ten papers; the corpus then looked like a Semantic Scholar corpus
even though all three sources had answered successfully.

Splitting the topic here, once, keeps the two sources' notion of "matches the
topic" identical: every meaningful term must be present (an AND at arXiv, an
all-terms substring test at the Anthology), but they no longer have to sit
next to each other. This deliberately does not become an OR — a topic's terms
all still constrain the search, so the result stays a topic search rather than
a keyword dragnet.
"""

import re

# Function words carry no topical meaning and appear in virtually every paper,
# so requiring them costs recall and buys no precision. Kept deliberately
# short: only words that show up inside research-topic phrasings, never
# domain vocabulary.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "of",
        "on",
        "or",
        "over",
        "the",
        "their",
        "to",
        "using",
        "via",
        "with",
        "within",
    }
)

# Kept inside a term: letters (any script), digits, and the two characters that
# occur *inside* research vocabulary — `-` ("low-resource", "GPT-4") and `'`
# ("Müller's"). Everything else — parentheses, colons, ampersands, commas — is
# a separator here, and at arXiv would otherwise be read as query grammar.
_TERM_EDGE = re.compile(r"^[^\w'-]+|[^\w'-]+$", re.UNICODE)

_MIN_TERM_LENGTH = 2


def meaningful_terms(topic: str) -> list[str]:
    """The distinct, topically meaningful terms in `topic`, in the order typed.

    Double quotes are removed outright: arXiv's grammar documents no escape for
    one inside a quoted phrase, so a quote left in a term would close that
    phrase early and change the query's meaning.

    A topic made up entirely of function words (or of single characters) is
    returned as a single term — the whole topic as typed. Searching for exactly
    what the user asked for is always better than searching for nothing, and
    this function must never empty a query out. An empty/whitespace topic
    yields no terms at all; callers decide what that means for them.
    """
    cleaned = topic.replace('"', "").strip()
    if not cleaned:
        return []

    terms: list[str] = []
    seen: set[str] = set()
    for token in cleaned.split():
        term = _TERM_EDGE.sub("", token)
        lowered = term.lower()
        if len(term) < _MIN_TERM_LENGTH or lowered in _STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        terms.append(term)

    return terms or [cleaned]
