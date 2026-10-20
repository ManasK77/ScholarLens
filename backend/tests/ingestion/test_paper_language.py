"""The S1 paper-language check (D-S1-LANG-01).

A corpus paper must itself be written in English, in the Latin alphabet. It
may *study* any language: an English paper about attacks written in Tamil is
in scope; a paper written in Tamil script is not. The evidence is the
abstract -- a title alone never proves the language of a paper -- and text too
short to judge is reported as ``unverified``, never as verified English.
"""

import json
from pathlib import Path

import pytest

from app.ingestion.paper_language import LanguagePolicy, assess

ENGLISH_ABOUT_TAMIL = (
    "We study prompt injection attacks written in Tamil and Tanglish against "
    "multilingual large language models. Our results show that attacks in "
    "low-resource languages bypass safety filters that were trained on English "
    "data, and we propose a defense based on machine translation."
)
# An English abstract quoting a Tamil attack string and a Hindi word.
ENGLISH_WITH_FOREIGN_TERMS = (
    "We show that the instruction 'முந்தைய வழிமுறைகளை புறக்கணி' (ignore previous "
    "instructions) and its Hindi form 'अनदेखा' are followed by the model. "
    + ENGLISH_ABOUT_TAMIL
)
ENGLISH_WITH_MATH = (
    "We bound the attack success rate by ε ≤ 0.05 and show that a perturbation "
    "δ with Σ α_i ≥ β leaves the detector's accuracy within 2% of the clean "
    "baseline [12, 14]. The analysis holds for every model we evaluate, and "
    "the bound is tight on the benchmark of Liu et al. (2024)."
)
TAMIL_SCRIPT = (
    "இந்த ஆய்வில் பெரிய மொழி மாதிரிகளுக்கு எதிரான பன்மொழி தூண்டல் உட்செலுத்தல் "
    "தாக்குதல்களை நாங்கள் ஆராய்கிறோம். தமிழ் மற்றும் ஆங்கில மொழிகளில் உள்ள "
    "தாக்குதல்கள் பாதுகாப்பு வடிகட்டிகளைத் தவிர்க்கின்றன என்பதை எங்கள் "
    "முடிவுகள் காட்டுகின்றன. நாங்கள் GPT-4 மற்றும் LLaMA மாதிரிகளை மதிப்பிடுகிறோம்."
)
HINDI_SCRIPT = (
    "इस शोध में हम बड़े भाषा मॉडलों पर बहुभाषी प्रॉम्प्ट इंजेक्शन हमलों का अध्ययन "
    "करते हैं। हमारे परिणाम दिखाते हैं कि हिंदी में लिखे गए हमले सुरक्षा फ़िल्टर को "
    "दरकिनार कर देते हैं और हम मशीनी अनुवाद पर आधारित एक बचाव प्रस्तावित करते हैं।"
)
FRENCH = (
    "Nous étudions les attaques par injection d'invites multilingues contre les "
    "grands modèles de langage. Nos résultats montrent que les attaques rédigées "
    "dans des langues peu dotées contournent les filtres de sécurité entraînés "
    "sur des données anglaises, et nous proposons une défense fondée sur la "
    "traduction automatique."
)
TURKISH = (
    "Bu çalışmada büyük dil modellerine karşı çok dilli istem enjeksiyonu "
    "saldırılarını inceliyoruz. Sonuçlarımız, düşük kaynaklı dillerde yazılmış "
    "saldırıların İngilizce veriler üzerinde eğitilmiş güvenlik filtrelerini "
    "atlattığını göstermektedir ve makine çevirisine dayalı bir savunma "
    "öneriyoruz."
)


def test_an_english_paper_about_tamil_attacks_is_english():
    result = assess(ENGLISH_ABOUT_TAMIL)

    assert result["status"] == "english"
    assert result["basis"] == "abstract"


def test_a_few_foreign_script_terms_do_not_make_an_english_abstract_foreign():
    result = assess(ENGLISH_WITH_FOREIGN_TERMS)

    assert result["status"] == "english"
    assert 0 < result["latin_fraction"] < 1
    assert {"TAMIL", "DEVANAGARI"} <= set(result["non_latin_scripts"])


def test_greek_letters_and_math_symbols_do_not_count_against_english():
    result = assess(ENGLISH_WITH_MATH)

    assert result["status"] == "english"
    assert result["latin_fraction"] == 1.0


@pytest.mark.parametrize("text", [TAMIL_SCRIPT, HINDI_SCRIPT])
def test_a_paper_written_in_a_non_latin_script_fails(text):
    result = assess(text)

    assert result["status"] == "non_latin_script"
    assert result["latin_fraction"] < 0.5


@pytest.mark.parametrize("text", [FRENCH, TURKISH])
def test_a_latin_script_paper_not_written_in_english_fails(text):
    result = assess(text)

    assert result["status"] == "not_english"
    assert result["english_marker_ratio"] < 0.10


@pytest.mark.parametrize("abstract", [None, "", "   "])
def test_no_abstract_is_unverified_not_english(abstract):
    result = assess(abstract)

    assert result["status"] == "unverified"
    assert result["basis"] == "none"


def test_a_short_abstract_is_unverified_rather_than_guessed():
    result = assess("Abstract. Injected instructions in Hindi and Tamil.")

    assert result["status"] == "unverified"
    assert result["basis"] == "abstract"
    assert "too short" in result["reason"]


def test_a_short_non_latin_abstract_is_unverified_not_rejected():
    """Too few letters to judge the script: held for review, not rejected."""
    result = assess("தமிழ் LLM")

    assert result["status"] == "unverified"


def test_thresholds_come_from_the_policy():
    strict = LanguagePolicy(min_words=1000)

    assert assess(ENGLISH_ABOUT_TAMIL, strict)["status"] == "unverified"


def test_the_assessment_records_its_measurements_and_version():
    result = assess(ENGLISH_ABOUT_TAMIL)

    assert result["version"]
    assert result["words"] >= 20
    assert result["letters"] >= 40
    assert result["english_marker_ratio"] >= 0.10
    assert result["reason"]


# ---------------------------------------------------------------------------
# The labelled fixture set (2026-10-12): synthetic, deterministic, every rule
# ---------------------------------------------------------------------------

CASES = json.loads(
    (Path(__file__).with_name("data") / "language_gate_cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_every_labelled_case_gets_its_expected_status(case):
    result = assess(case["text"])

    assert result["status"] == case["expected_status"], result["reason"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_every_decision_records_its_rule_and_measurements(case):
    result = assess(case["text"])

    assert result["rule"] in {
        "no_abstract",
        "min_letters",
        "min_latin_fraction",
        "min_words",
        "min_english_ratio",
    }
    assert result["reason"]
    assert result["version"]
    for key in ("letters", "words", "latin_fraction", "english_marker_ratio"):
        assert key in result


def test_the_fixture_set_covers_every_status():
    assert {c["expected_status"] for c in CASES} == {
        "english",
        "non_latin_script",
        "not_english",
        "unverified",
    }
