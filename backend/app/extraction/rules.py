"""spaCy-backed, sentence-level scientific fact cue rules."""

from functools import lru_cache

import spacy
from spacy.language import Language
from spacy.matcher import PhraseMatcher

from app.extraction.schema import FactType

_PHRASES: dict[FactType, tuple[str, ...]] = {
    FactType.CONTRIBUTION: (
        "we propose",
        "we present",
        "we introduce",
        "we develop",
        "we demonstrate",
        "we show",
        "our contribution",
        "achieves",
        "outperforms",
        "state-of-the-art",
    ),
    FactType.LIMITATION: (
        "limitation",
        "limitations",
        "limited by",
        "fails",
        "fail to",
        "cannot",
        "unable to",
        "drawback",
        "performance decreases",
        "performance degrades",
        "remains weak",
        "remains limited",
    ),
    FactType.FUTURE_WORK: (
        "future work",
        "future research",
        "future studies",
        "further research",
        "further investigation",
        "should explore",
        "we plan to",
        "remains to be explored",
    ),
    FactType.DATASET: (
        "dataset",
        "datasets",
        "corpus",
        "corpora",
        "benchmark",
        "benchmarks",
        "squad",
        "imagenet",
        "superglue",
        "glue",
        "wmt",
        "conll",
        "mnist",
        "cifar",
    ),
    FactType.MODEL: (
        "model",
        "models",
        "architecture",
        "network",
        "networks",
        "transformer",
        "bert",
        "roberta",
        "gpt",
        "t5",
        "llama",
        "qwen",
    ),
    FactType.METRIC: (
        "accuracy",
        "precision",
        "recall",
        "f1",
        "f-1",
        "bleu",
        "rouge",
        "auc",
        "mean average precision",
        "perplexity",
    ),
}

_SECTION_PRIORS = {
    "limitations": FactType.LIMITATION,
    "future work": FactType.FUTURE_WORK,
}


@lru_cache(maxsize=1)
def get_nlp() -> Language:
    """Return one dependency-light English tokenizer/sentencizer per process."""
    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    return nlp


@lru_cache(maxsize=1)
def _get_matcher() -> PhraseMatcher:
    nlp = get_nlp()
    matcher = PhraseMatcher(nlp.vocab, attr="LOWER")
    for fact_type, phrases in _PHRASES.items():
        matcher.add(fact_type.value, [nlp.make_doc(phrase) for phrase in phrases])
    return matcher


def extract_rule_types(sentence: str, section_type: str | None = None) -> set[FactType]:
    """Return every strongly supported fact type for one exact sentence."""
    doc = get_nlp().make_doc(sentence)
    matcher = _get_matcher()
    fact_types = {
        FactType(get_nlp().vocab.strings[match_id]) for match_id, _, _ in matcher(doc)
    }
    if section_type:
        prior = _SECTION_PRIORS.get(section_type.strip().lower())
        if prior:
            fact_types.add(prior)
    return fact_types


def split_sentences(text: str) -> list[str]:
    """Segment fallback paragraph text through spaCy's sentencizer."""
    normalized = " ".join(text.split())
    if not normalized:
        return []
    return [sentence.text.strip() for sentence in get_nlp()(normalized).sents]
