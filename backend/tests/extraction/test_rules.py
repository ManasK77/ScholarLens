from app.extraction.rules import extract_rule_types, split_sentences
from app.extraction.schema import FactType


def test_contribution_and_model_sentence_is_multilabel() -> None:
    types = extract_rule_types("We propose a new Transformer architecture.")
    assert types == {FactType.CONTRIBUTION, FactType.MODEL}


def test_limitation_and_dataset_sentence_is_multilabel() -> None:
    types = extract_rule_types(
        "However, performance decreases for multilingual datasets."
    )
    assert types == {FactType.LIMITATION, FactType.DATASET}


def test_future_work_cues_are_extracted() -> None:
    assert extract_rule_types("Future work should explore few-shot learning.") == {
        FactType.FUTURE_WORK
    }


def test_named_dataset_and_model_are_extracted() -> None:
    assert extract_rule_types("We evaluate BERT on the SQuAD benchmark.") == {
        FactType.DATASET,
        FactType.MODEL,
    }


def test_contribution_model_and_metrics_are_extracted_together() -> None:
    assert extract_rule_types(
        "Our model achieves 92.4 F1 and 94 percent accuracy."
    ) == {FactType.CONTRIBUTION, FactType.MODEL, FactType.METRIC}


def test_section_priors_apply_only_to_limitation_and_future_work() -> None:
    sentence = "This behavior is especially pronounced in low-resource settings."
    assert extract_rule_types(sentence, "Limitations") == {FactType.LIMITATION}
    assert extract_rule_types(sentence, "Future Work") == {FactType.FUTURE_WORK}
    assert extract_rule_types(sentence, "Discussion") == set()


def test_neutral_sentence_is_not_a_fact() -> None:
    assert extract_rule_types("The paper is organized as follows.") == set()


def test_spacy_sentencizer_splits_fallback_paragraphs() -> None:
    assert split_sentences("One sentence. Future work remains open.") == [
        "One sentence.",
        "Future work remains open.",
    ]
