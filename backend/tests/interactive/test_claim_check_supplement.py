"""Two narrow vetoes found by the supplementary claim set (mode_b_claims_s2).

* "all LLMs" is a universal claim like "all models": one paper's ChatGPT
  finding restated as true of every LLM (s12) passed every veto.
* A future-work statement ("Future work will address ... Unicode
  normalization") restated as something already done ("Sentra-Guard resolves
  homoglyph attacks through Unicode normalization", s26) passed too: a plan
  cannot support a claim of accomplishment. `future_as_done` fires only when
  every cited passage is a future-work statement and the claim carries no
  future or modal wording of its own.

Passages are exact texts from the frozen index (paper 8 and paper 10).
"""

import pytest

from app.interactive.claim_check import check_claim, check_claims

FUTURE = (
    "Future work will address: (i) Unicode normalization and homoglyph resolution; "
    "(ii) low-resource and code-mixed language robustness through adversarial "
    "augmentation; (iii) adaptive thresholding under distribution shift; (iv) "
    "generation-time monitoring for multi-turn adversarial contexts; (v) provenance "
    "tracking for interpretable audit trails; and (vi) multimodal defense spanning "
    "text, vision, and audio."
)
JAILBREAK = (
    "There are 184 (5.07%) questions that resulted in ChatGPT's jailbreak, and all "
    "the English versions of these questions do not lead to jailbreak.This implies "
    "that non-English questions might bypass ChatGPT's safety checks, potentially "
    "causing ChatGPT jailbreak."
)


def _veto(text, passage, section):
    item = {"id": "p", "paper_id": 8, "text": passage, "source_type": section}
    return check_claims([{"text": text, "evidence_ids": ["p"]}], {"p": item})[0]


def test_a_claim_about_all_llms_from_one_studys_passage_is_vetoed():
    claim = "Non-English questions bypass the safety checks of all LLMs."
    assert "unsupported_absolute" in check_claim(claim, [JAILBREAK])
    stated = "All LLMs were evaluated on the benchmark."
    assert "unsupported_absolute" not in check_claim(stated, [stated])


def test_future_work_restated_as_done_is_vetoed():
    claim = "Sentra-Guard resolves homoglyph attacks through Unicode normalization."
    assert "future_as_done" in _veto(claim, FUTURE, "future_work")


@pytest.mark.parametrize(
    "claim",
    [
        "Future work will address low-resource and code-mixed language robustness.",
        "The authors plan to add Unicode normalization and homoglyph resolution.",
        "Homoglyph resolution is proposed as future work.",
        "Sentra-Guard could be extended with Unicode normalization.",
    ],
)
def test_a_claim_that_keeps_the_future_framing_is_not_vetoed(claim):
    assert "future_as_done" not in _veto(claim, FUTURE, "future_work")


def test_the_same_claim_citing_a_results_passage_is_not_future_as_done():
    claim = "Sentra-Guard resolves homoglyph attacks through Unicode normalization."
    assert "future_as_done" not in _veto(claim, FUTURE, "paragraph")


def test_a_result_sentence_stage_3_typed_future_work_still_supports_a_result():
    # mode_b_v1 c06: paper 8's results sentence is stored as `future_work`.
    passage = (
        "Evaluation on HarmBench-28K yielded 99.98% accuracy, AUC = 1.00, and "
        "ASR = 0.004% at 47 ms latency."
    )
    claim = "Sentra-Guard reports 99.98% accuracy on HarmBench-28K at 47 ms latency."
    assert "future_as_done" not in _veto(claim, passage, "future_work")


def test_one_results_passage_beside_a_future_work_one_lifts_the_veto():
    items = {
        "f": {"id": "f", "paper_id": 8, "text": FUTURE, "source_type": "future_work"},
        "r": {"id": "r", "paper_id": 8, "text": FUTURE, "source_type": "paragraph"},
    }
    claim = "Sentra-Guard resolves homoglyph attacks through Unicode normalization."
    vetoes = check_claims([{"text": claim, "evidence_ids": ["f", "r"]}], items)[0]
    assert "future_as_done" not in vetoes


def test_digits_inside_a_name_the_title_states_are_not_unsupported_numbers():
    # A passage rarely names its own system; the title does ("Llama-3-Guard: ...").
    vetoes = check_claim(
        "Llama-3-Guard achieves high recall on the benchmark.",
        ["Our system achieves high recall on the benchmark."],
        ["Llama-3-Guard: Guarding Multilingual Prompts"],
    )
    assert vetoes == []
    # A standalone number still has to be stated.
    assert "unsupported_term" in check_claim(
        "Llama-3-Guard achieves 97% recall on the benchmark.",
        ["Our system achieves high recall on the benchmark."],
        ["Llama-3-Guard: Guarding Multilingual Prompts"],
    )
