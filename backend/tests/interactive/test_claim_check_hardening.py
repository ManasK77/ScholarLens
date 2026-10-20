"""Claim-check false rejections and false acceptances found by the Mode B
evaluation set (`evaluation/datasets/mode_b_v1/claims.csv`, 2026-10-18).

Passages are verbatim excerpts of stored Qdrant payloads. The checks can still
only veto. These tests pin which supported phrasings they must leave alone and
which unsupported ones they must reject, and neither result proves support.
"""

import json
from pathlib import Path

import pytest

from app.interactive.claim_check import check_claim, check_claims

# Paper 3 (malicious-prompt compliance study).
COMPLIANCE = (
    "The analysis is conducted on a total of 15540 evaluated model responses, and "
    "the findings demonstrate that malicious request fulfillment is not an isolated "
    "edge case but a systematic phenomenon.Across all experimental conditions, "
    "68.76% of requests result in full malicious compliance, while an additional "
    "12.08% produce partial or stalling assistance."
)
CONSERVATIVE = (
    "GPT-5 Mini is the most conservative in terms of full compliance (44.09%) but "
    "compensates with a high stalling rate (30.23%), so that its overall fulfillment "
    "rate remains 74.32%.By contrast, Deepseek and Meta Llama 4 Scout Instruct, "
    "which are among the most compliant models, warn less often."
)
ENCODINGS = (
    "Lastly, non-English languages consistently exhibit higher compliance than "
    "English, while simple character encodings reduce but do not prevent malicious "
    "outputs, underscoring the critical need for multilingual and encoding aware "
    "safety mechanisms."
)
# Paper 1 (hidden prompt injection in academic reviewing).
TRUNCATION = (
    "In addition, only the first 6,000 characters of each paper were provided to "
    "the model, which may cause some injected prompts to fall outside the model's "
    "input and lead to conservative estimates of vulnerability."
)
ARABIC = (
    "In contrast, Arabic prompt injection exhibits markedly weaker effects, with "
    "limited score drift and fewer decision changes.A plausible explanation is "
    "uneven multilingual alignment and instruction-following reliability, as many "
    "alignment techniques and training resources remain English-centric."
)
# Paper 7 (LLM relevance judges).
PROMPTARMOR = (
    "Table 2 shows that PromptArmor reduces attack effectiveness but does not fully "
    "eliminate false-positive inflation.Across attacks, instruction-based "
    "injections are effective but remain the easiest to mitigate."
)
# Paper 6 (Sarvam-105B case study).
NO_RANKING = (
    "Language-and attack-family-level denominators are smaller still, so this paper "
    "does not rank English, Tamil, or Tanglish by safety, establish attack-family "
    "effects, test significance, or claim a causal effect of reasoning mode."
)
# Paper 8 (Sentra-Guard); the passage does not name the system itself.
HARMBENCH = (
    "Evaluation on HarmBench-28K yielded 99.98% accuracy, AUC = 1.00, and "
    "ASR = 0.004% at 47 ms latency."
)
SENTRA_TITLE = (
    "Sentra-Guard: A Real-Time Multilingual Defense Against Adversarial LLM Prompts"
)
# Paper 9 (tourism chatbot) and paper 10 (ChatGPT fuzzing).
RELEVANCE_GATE = (
    "Relevance Check.Compute cosine similarity between the query embedding and "
    "top-k RAG chunks.Require maxSim ≥ τ (default τ = 0.70)."
)
FUZZING = (
    "This is currently the most comprehensive testing for Chat-GPT's "
    "cross-linguistic proficiency.It encompasses an extensive list of 121 languages."
)
# Paper 20 (phishing detection).
TRANSLATED_FPR = (
    "Table VII evaluates the detection of cross-lingual phishing in 179 samples per "
    "language.Claude Sonnet 4's FPR increases from 2.4% to 24.1% averaged across "
    "Bangla, Chinese, and Hindi, eliminating its baseline advantage."
)
ATTACK_RATE = (
    "As shown in Table IV, Grok-3 exhibited maximum susceptibility with an attack "
    "success rate (ASR) of 12.3%."
)


# --- numbers: supported values are not vetoed, changed values are ---------------


@pytest.mark.parametrize(
    "claim",
    [
        (
            "Across all experimental conditions, 68.76% of malicious requests "
            "resulted in full compliance."
        ),
        "GPT-5 Mini was the most conservative model in terms of full compliance (44.09%).",
        "Fulfillment remained 74.32 % for GPT-5 Mini.",
    ],
)
def test_a_percentage_its_passage_states_is_not_vetoed(claim):
    assert "unsupported_term" not in check_claim(claim, [COMPLIANCE, CONSERVATIVE])


def test_a_changed_percentage_is_still_vetoed():
    claim = "Across all experimental conditions, 78.76% of requests result in full compliance."
    assert "unsupported_term" in check_claim(claim, [COMPLIANCE])


@pytest.mark.parametrize("written", ["6000", "6,000"])
def test_thousands_separators_do_not_change_a_number(written):
    claim = (
        f"Only the first {written} characters of each paper were provided to the model."
    )
    assert check_claim(claim, [TRUNCATION]) == []
    assert "unsupported_term" in check_claim(
        "Only the first 8,000 characters of each paper were provided.", [TRUNCATION]
    )


def test_a_trailing_zero_does_not_change_a_decimal():
    claim = "Retrieved chunks needed a cosine similarity of at least 0.7 to the query."
    assert "unsupported_term" not in check_claim(claim, [RELEVANCE_GATE])


# --- term overlap: inflections, compounds and acronyms ----------------------------


def test_inflections_the_stemmer_misses_still_count_as_overlap():
    # content_terms gives "requir" for "required" but "require" for "Require".
    claim = (
        "The tourism chatbot required a cosine similarity of at least 0.70 between "
        "the query and the retrieved chunks."
    )
    assert check_claim(claim, [RELEVANCE_GATE]) == []


def test_a_closed_compound_matches_its_hyphenated_form():
    # Without the compound, only "languages" of {chatgpt, covered, languages} overlaps.
    assert check_claim("ChatGPT covered 121 languages.", [FUZZING]) == []
    # "e-mails" splits into "e" + "mails"; neither prefixes "emails".
    passage = "Bangla e-mails induce maximum degradation across models."
    assert check_claim("Emails in Bangla fail.", [passage]) == []


def test_an_acronym_matches_its_expansion_in_either_direction():
    expanded = "Claude Sonnet 4's false positive rate rose from 2.4% to 24.1% on translated emails."
    assert check_claim(expanded, [TRANSLATED_FPR]) == []
    spelled_out = ATTACK_RATE.replace(" (ASR)", "")
    assert check_claim("Grok-3 ASR reached 12.3%.", [spelled_out]) == []


def test_lenient_overlap_still_vetoes_a_merely_topical_passage():
    assert "low_overlap" in check_claim(
        "Detectors fail on Hindi prompts in chatbots.", [TRUNCATION]
    )


# --- polarity: a dropped negation is vetoed, a kept one is not --------------------


@pytest.mark.parametrize(
    ("claim", "passage"),
    [
        ("PromptArmor fully eliminates false-positive inflation.", PROMPTARMOR),
        ("Simple character encodings prevent malicious outputs.", ENCODINGS),
        ("The Sarvam paper ranks English, Tamil and Tanglish by safety.", NO_RANKING),
    ],
)
def test_a_claim_that_drops_its_passages_negation_is_vetoed(claim, passage):
    assert "polarity_mismatch" in check_claim(claim, [passage])


def test_a_negation_of_a_different_predicate_is_not_a_mismatch():
    drift = (
        "In contrast, Arabic injection exhibits near-zero mean score drift and no "
        "statistically significant effect."
    )
    assert (
        check_claim("Arabic injection showed near-zero mean score drift.", [drift])
        == []
    )
    labels = (
        "All 17 benign-correct outputs were labelled intent-to-ignore yes and "
        "intent-to-follow no.All three attack-success outputs were labelled otherwise."
    )
    claim = "All 17 benign-correct outputs were labelled with an intent to ignore the injection."
    assert "polarity_mismatch" not in check_claim(claim, [labels])


@pytest.mark.parametrize(
    "claim",
    [
        "PromptArmor reduces attack effectiveness.",
        "PromptArmor does not fully eliminate false-positive inflation.",
        (
            "PromptArmor reduces attack effectiveness but does not fully eliminate "
            "false-positive inflation."
        ),
    ],
)
def test_a_claim_that_keeps_the_passages_polarity_is_not_vetoed(claim):
    assert "polarity_mismatch" not in check_claim(claim, [PROMPTARMOR])


# --- names: a model or dataset the citations never mention ------------------------


def test_a_swapped_model_name_is_vetoed():
    claim = "GPT-4o was the most conservative model in terms of full compliance."
    assert "unsupported_name" in check_claim(claim, [CONSERVATIVE])


def test_a_swapped_dataset_name_is_vetoed_even_with_the_right_numbers():
    claim = "Sentra-Guard reports 99.98% accuracy on the BIPIA benchmark."
    assert "unsupported_name" in check_claim(claim, [HARMBENCH], [SENTRA_TITLE])


def test_a_name_in_the_cited_papers_title_is_not_vetoed():
    claim = "Sentra-Guard reports 99.98% accuracy on HarmBench-28K at 47 ms latency."
    assert check_claim(claim, [HARMBENCH], [SENTRA_TITLE]) == []
    # Without the title, the passage alone never names the system.
    assert "unsupported_name" in check_claim(claim, [HARMBENCH])


def test_generic_acronyms_are_not_treated_as_names():
    claim = "LLMs and RAG pipelines report an ASR of 12.3%."
    assert "unsupported_name" not in check_claim(claim, [ATTACK_RATE])


# --- causes: an inserted explanation must itself be supported --------------------


def test_an_inserted_cause_the_passage_does_not_state_is_vetoed():
    claim = "Arabic injection had weaker effects because Arabic text is written right to left."
    assert "unsupported_cause" in check_claim(claim, [ARABIC])


def test_the_passages_own_explanation_is_not_vetoed():
    claim = (
        "Arabic injection had weaker effects, plausibly because alignment techniques "
        "and training resources remain English-centric."
    )
    assert check_claim(claim, [ARABIC]) == []


# --- titles reach the check through check_claims ----------------------------------


def test_check_claims_passes_the_cited_papers_titles():
    evidence = {"h": {"id": "h", "text": HARMBENCH, "title": SENTRA_TITLE}}
    claims = [{"text": "Sentra-Guard reports 99.98% accuracy.", "evidence_ids": ["h"]}]
    assert check_claims(claims, evidence) == [[]]


def test_a_titles_topic_words_never_count_as_support():
    # Every title in this corpus says "prompt injection"; that cannot make an
    # overgeneralised claim overlap with a passage that only lists two languages.
    mipiad = "The empirical validation covers English and Bangla only."
    title = "MIPIAD: Multilingual Indirect Prompt Injection Attack Defense"
    claim = "Prompt injection detectors in the literature are validated mainly on English and Bangla."
    assert "low_overlap" in check_claim(claim, [mipiad], [title])


# --- real model output: the recorded live claims keep their verdicts --------------


LIVE = json.loads(
    (
        Path(__file__).resolve().parent.parent / "fixtures" / "mode_b_live_run_2.json"
    ).read_text(encoding="utf-8")
)


def test_the_live_supported_claim_is_still_not_vetoed():
    by_id = {e["id"]: e for e in LIVE["evidence"]}
    claim = LIVE["draft"]["claims"][0]
    assert check_claims([claim], by_id) == [[]]


def test_the_live_overreaching_claim_is_still_vetoed():
    by_id = {e["id"]: e for e in LIVE["evidence"]}
    claim = LIVE["draft"]["claims"][1]
    assert "unsupported_term" in check_claims([claim], by_id)[0]


def test_only_a_word_written_as_an_acronym_is_read_as_one():
    # "tea" spells the initials of "table evaluates all"; it is not an acronym.
    passage = "The table evaluates all victims on the benchmark."
    # {tea, ceremonies, harm, victims}: only "victims" is in the passage.
    assert "low_overlap" in check_claim("Tea ceremonies harm victims.", [passage])


def test_a_descriptive_suffix_is_not_part_of_a_name():
    passage = "Even evaluation with GPT-4 makes mistakes."
    assert "unsupported_name" not in check_claim(
        "GPT-4-based evaluation makes mistakes.", [passage]
    )
    assert "unsupported_name" in check_claim(
        "GPT-5-based evaluation makes mistakes.", [passage]
    )


def test_every_veto_code_has_a_regeneration_hint():
    import inspect
    import re

    from app.interactive import claim_check
    from app.interactive.workflow import VETO_HINTS

    emitted = set(
        re.findall(r'reasons\.append\("(\w+)"\)', inspect.getsource(claim_check))
    )
    assert emitted and emitted == set(VETO_HINTS)
