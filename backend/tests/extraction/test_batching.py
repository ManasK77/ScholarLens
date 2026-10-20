from app.extraction.semantic import SemanticStatementClassifier
from app.parsing.section_classifier import HeadingClassifier


def test_sentence_batching_preserves_order_and_abstention():
    calls = []

    def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0] if "limitation" in t else [0.0, 1.0] for t in texts]

    individual = SemanticStatementClassifier(embed)
    sentences = ["a limitation", "neutral context", "a limitation"]
    expected = [individual.classify(s) for s in sentences]
    calls.clear()
    batched = SemanticStatementClassifier(embed)
    assert batched.classify_many(sentences, batch_size=2) == expected
    assert len(calls) <= 3
    assert batched.classify_many([]) == []


def test_heading_cache_does_not_reembed_exemplars():
    calls = []

    def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    classifier = HeadingClassifier(embed)
    assert classifier.classify_many(["Methods", "Unknown", None, "Another"]) == [
        "Methodology",
        "Introduction",
        "Introduction",
        "Introduction",
    ]
    classifier.classify_many(["Different"])
    assert sum("introduction background related work" in c for c in calls) == 1
    assert calls[-1] == ["Different"]
