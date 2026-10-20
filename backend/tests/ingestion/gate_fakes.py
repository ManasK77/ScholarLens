"""Test doubles for the S1 relevance gate. No test ever loads the real model."""

from app.ingestion.relevance import (
    RelevanceDecision,
    RelevanceGate,
    analyze_topic,
    load_lexicon,
)


class FakeScorer:
    """Returns `default` for every text, or the score given for its title in
    `by_title` (the scored text is "<title>. <abstract>" or the title alone)."""

    def __init__(self, default=5.0, by_title=None):
        self.default = default
        self.by_title = by_title or {}
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query, texts):
        self.calls.append((query, list(texts)))
        return [
            next(
                (
                    value
                    for title, value in self.by_title.items()
                    if text == title or text.startswith(f"{title}. ")
                ),
                self.default,
            )
            for text in texts
        ]


def real_gate(topic, scorer=None, floor=-1.0, strong=3.5):
    """The production gate logic, with a fake semantic scorer."""
    return RelevanceGate(
        analyze_topic(topic, load_lexicon()),
        scorer or FakeScorer(),
        semantic_floor=floor,
        strong_threshold=strong,
    )


class PermissiveGate:
    """Admits every candidate. For route tests about caps, dedup and source
    failures, whose placeholder titles ("A paper") share nothing with their
    placeholder topic ("anything")."""

    def __init__(self, topic):
        self._real = real_gate(topic)

    def evaluate_many(self, items):
        return [
            RelevanceDecision(
                admitted=True,
                reason_code="relevant:strong",
                reason="admitted by the permissive test gate",
                score=0.0,
                tier="strong",
                evidence={},
            )
            for _ in items
        ]

    def facets_evidenced(self, title, abstract):
        return True

    def prefilter_terms(self):
        return self._real.prefilter_terms()

    def query_groups(self):
        return self._real.query_groups()

    def describe(self):
        return self._real.describe()
