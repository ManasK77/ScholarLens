"""ACL Anthology candidate generation for the S1 relevance gate (D-S1-GATE-01).

The legacy rule (every topic word as a substring of title/abstract) found two
ACL records for "multilingual prompt injection" -- both off-topic -- and missed
"Scaling Behavior of Machine Translation with Large Language Models under
Prompt Injection Attacks", which never says "multilingual". With the gate's
concept matcher, a record is a candidate when the legacy rule *or* the gate's
facet evidence says so; the gate then decides. Legacy-only matches stay
candidates so that their rejection is recorded, not silent.
"""

from app.ingestion.sources import acl_anthology
from tests.ingestion.gate_fakes import real_gate

TOPIC = "multilingual prompt injection"

BIB = r"""@inproceedings{mt-pi,
    title = "Scaling Behavior of Machine Translation with Large Language Models under Prompt Injection Attacks",
    author = "Sizemore, Zhifan",
    booktitle = "Proceedings of the First Workshop on Scaling LLMs",
    year = "2024",
    url = "https://aclanthology.org/2024.scalellm-1.2/",
    abstract = "Injected instructions in the source text redirect the output.",
}
@inproceedings{braced,
    title = "Cross-Lingual {P}rompt-{I}njection Detection",
    author = "Doe, Jane",
    booktitle = "Proceedings of ACL 2025 (Volume 1: Long Papers)",
    year = "2025",
    url = "https://aclanthology.org/2025.acl-long.9/",
    abstract = "We detect attacks across languages.",
}
@inproceedings{error-injection,
    title = "A Comprehensive Evaluation of Multilingual Chain-of-Thought Reasoning",
    author = "Roe, Rich",
    booktitle = "Findings of the Association for Computational Linguistics: EACL 2026",
    year = "2026",
    url = "https://aclanthology.org/2026.findings-eacl.276/",
    abstract = "We use prompt perturbations, truncation and error injection.",
}
@inproceedings{unrelated,
    title = "Parsing Old Norse Sagas",
    author = "Poe, Pat",
    booktitle = "Proceedings of LaTeCH 2024",
    year = "2024",
    url = "https://aclanthology.org/2024.latech-1.3/",
    abstract = "A dependency parser for sagas.",
}
"""


def _bib(tmp_path):
    path = tmp_path / "anthology.bib"
    path.write_text(BIB, encoding="utf-8")
    return str(path)


def _ids(results):
    return sorted(result["acl_id"] for result in results)


def test_the_legacy_rule_alone_finds_only_the_error_injection_record(tmp_path):
    """Unchanged default behaviour -- and the baseline's problem in miniature."""
    assert _ids(acl_anthology.search_topic(_bib(tmp_path), TOPIC)) == [
        "2026.findings-eacl.276"
    ]


def test_the_gate_matcher_adds_concept_matches_and_keeps_legacy_ones(tmp_path):
    gate = real_gate(TOPIC)

    results = acl_anthology.search_topic(
        _bib(tmp_path),
        TOPIC,
        matches=gate.facets_evidenced,
        prefilter_terms=gate.prefilter_terms(),
    )

    assert _ids(results) == [
        "2024.scalellm-1.2",
        "2025.acl-long.9",
        "2026.findings-eacl.276",
    ]


def test_the_prefilter_reads_hyphenated_and_brace_protected_spellings(tmp_path):
    """`{P}rompt-{I}njection` in the raw BibTeX is "prompt injection"."""
    gate = real_gate(TOPIC)

    results = acl_anthology.search_topic(
        _bib(tmp_path),
        TOPIC,
        matches=gate.facets_evidenced,
        prefilter_terms=["prompt injection"],
    )

    assert "2025.acl-long.9" in _ids(results)
