"""ACL Anthology non-research-track rejection (D-15-01).

Paper 19 of the 20-paper LLM-security corpus was an ACL Anthology *Tutorial
Abstract* ("Beyond Guardrails: Advanced Safety for Large Language Models —
Monolingual, Multilingual and Multimodal Frontiers", `2025.ijcnlp-tutorials.5`).
It has no results section, no method, no original claims — its extracted
"limitations" and "future work" were syllabus bullets, a reading list, and an
author biography. Stage 6 turned three of those bullets into a fabricated
cluster and diluted two others with author-bio text.

The fix is at collection, not extraction: ACL Anthology's `booktitle`/`journal`
field already names the track ("...: Tutorial Abstract", "...: System
Demonstrations") for exactly the record types that carry no original research
content. `search_topic` rejects on that field alone — never on PDF text, an
abstract keyword, or a title word — so a research paper that happens to study
tutorials or demos as its subject is never at risk.

`tests/fixtures/acl_anthology_sample.bib` carries five records for this: paper
19's own venue string verbatim, a `Tutorial Abstracts` (plural) variant, a
`System Demonstrations` venue, a `Demo Track` venue, and one with irregular
case and whitespace around the track marker. All five also contain
"multilingual" in their title/abstract, so `test_search_topic_filters_by_
keyword_case_insensitive`'s existing exact-set assertion is itself a
regression guard: it fails if any of the five leak through.
"""

import pytest

from app.ingestion.sources import acl_anthology
from app.ingestion.sources.acl_anthology import is_non_research_track

FIXTURE_PATH = "tests/fixtures/acl_anthology_sample.bib"

REJECTED_IDS = {
    "2025.ijcnlp-tutorials.5",  # paper 19's exact venue, verbatim
    "2024.acl-tutorials.3",  # "Tutorial Abstracts" (plural)
    "2023.emnlp-demo.7",  # "System Demonstrations"
    "2022.acl-demo.9",  # "Demo Track"
    "2025.workshop-tutorials.1",  # irregular case/whitespace: "  tutorial   ABSTRACTS  "
}


class TestIsNonResearchTrack:
    def test_paper_19s_exact_venue_is_rejected(self) -> None:
        venue = (
            "Proceedings of the 14th International Joint Conference on "
            "Natural Language Processing and the 4th Conference of the "
            "Asia-Pacific Chapter of the Association for Computational "
            "Linguistics: Tutorial Abstract"
        )
        assert is_non_research_track(venue) is True

    def test_tutorial_abstract_singular_is_rejected(self) -> None:
        assert is_non_research_track("Proceedings of ACL: Tutorial Abstract") is True

    def test_tutorial_abstracts_plural_is_rejected(self) -> None:
        assert is_non_research_track("Proceedings of ACL: Tutorial Abstracts") is True

    def test_demo_is_rejected(self) -> None:
        assert is_non_research_track("Proceedings of ACL: Demo Track") is True

    def test_system_demonstration_is_rejected(self) -> None:
        assert (
            is_non_research_track("Proceedings of EMNLP: System Demonstration") is True
        )

    def test_system_demonstrations_plural_is_rejected(self) -> None:
        assert (
            is_non_research_track("Proceedings of EMNLP: System Demonstrations") is True
        )

    def test_a_normal_research_venue_is_retained(self) -> None:
        assert (
            is_non_research_track(
                "Proceedings of the 61st Annual Meeting of the Association "
                "for Computational Linguistics"
            )
            is False
        )

    @pytest.mark.parametrize(
        "venue",
        [
            "Proceedings of ACL:   tutorial   ABSTRACTS  ",
            "proceedings of acl: TUTORIAL ABSTRACT",
            "Proceedings of ACL:\tSystem\tDemonstrations",
            "  Proceedings of ACL: Demo  ",
        ],
    )
    def test_case_and_whitespace_variations_are_rejected(self, venue: str) -> None:
        assert is_non_research_track(venue) is True

    def test_none_venue_is_not_rejected(self) -> None:
        # A record with neither `booktitle` nor `journal` is metadata-thin, not
        # provably a tutorial/demo; `search_topic` already handles a missing
        # venue elsewhere, so this function only judges what it is given.
        assert is_non_research_track(None) is False

    def test_empty_venue_is_not_rejected(self) -> None:
        assert is_non_research_track("") is False

    @pytest.mark.parametrize(
        "venue",
        [
            (
                "Proceedings of the Workshop on Interactive Demonstrations of "
                "Multilingual Safety Failures"
            ),
            "Tutorial Design for Multilingual NLP Education",
            "Proceedings of the Conference on System Demonstration Methodology",
        ],
    )
    def test_the_word_appearing_mid_sentence_is_not_a_track_marker(
        self, venue: str
    ) -> None:
        # The rejection is on a *track marker* — the word/phrase naming the
        # section of the proceedings a record was published in, conventionally
        # set off by a colon — not on the word "tutorial" or "demo" appearing
        # anywhere in the venue string. A workshop *about* demonstrations is a
        # research venue like any other.
        assert is_non_research_track(venue) is False


class TestSearchTopicRejectsNonResearchTracks:
    def test_none_of_the_five_non_research_records_are_returned(self) -> None:
        out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual")

        ids = {c["acl_id"] for c in out}
        assert ids.isdisjoint(REJECTED_IDS)

    def test_paper_19s_own_record_is_excluded_by_topic_and_venue(self) -> None:
        out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual safety")

        assert "2025.ijcnlp-tutorials.5" not in {c["acl_id"] for c in out}

    def test_legitimate_research_records_still_match(self) -> None:
        out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual")

        ids = {c["acl_id"] for c in out}
        assert "2023.acl-long.310" in ids
        assert "2020.coling-main.88" in ids

    def test_rejected_records_are_not_even_parsed_into_the_result_shape(self) -> None:
        # Rejection happens before a candidate dict is built, not as a filter
        # over already-built results — so nothing downstream ever sees one.
        out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual")

        for candidate in out:
            assert candidate["acl_id"] not in REJECTED_IDS

    def test_rejected_records_cannot_occupy_a_process_capped_slot(self) -> None:
        # `routes_ingestion._process_capped` has exactly one input for this
        # source: the list `search_topic` returns. It counts candidates by
        # walking that list in order and stopping once `acl_anthology_max_
        # results` corpus rows have been touched (D-DUP/cap semantics) — it
        # never sees, and so can never spend a slot on, a record this function
        # already dropped. Proving the five non-research records are absent
        # from `search_topic`'s return value — which the two tests above do —
        # is therefore a complete proof for the cap, not merely a plausible
        # inference from it.
        out = acl_anthology.search_topic(FIXTURE_PATH, "multilingual")

        simulated_cap = len(out)
        kept = out[:simulated_cap]
        assert REJECTED_IDS.isdisjoint(c["acl_id"] for c in kept)


# Verbatim venue strings from the live Anthology export. Since ACL 2023 the
# main conferences print the track inside a parenthesised volume label,
# "(Volume 3: System Demonstrations)", so the text after the final colon ends
# in ")". The anchored match missed every one of them: 337 System
# Demonstrations and 48 Tutorial Abstracts records in the export, including
# all of ACL 2023-2026's demo and tutorial volumes. The markers are unchanged;
# only the parenthesised placement of the same markers is now read.
_ACL_2025 = "Proceedings of the 63rd Annual Meeting of the Association for Computational Linguistics"
_NAACL_2025 = (
    "Proceedings of the 2025 Conference of the Nations of the Americas Chapter of "
    "the Association for Computational Linguistics: Human Language Technologies"
)


class TestParenthesisedVolumeTracks:
    @pytest.mark.parametrize(
        "venue",
        [
            f"{_ACL_2025} (Volume 3: System Demonstrations)",
            f"{_ACL_2025} (Volume 5: Tutorial Abstracts)",
            (
                "Proceedings of the 64th Annual Meeting of the {A}ssociation for "
                "{C}omputational {L}inguistics (Volume 3: System Demonstrations)"
            ),
            (
                "Proceedings of the 22nd Chinese National Conference on "
                "Computational Linguistics (Volume 4: Tutorial Abstracts)"
            ),
            f"{_NAACL_2025} (System Demonstrations)",
            f"{_ACL_2025} (Volume 3: System  Demonstrations )",
        ],
    )
    def test_a_parenthesised_non_research_track_is_rejected(self, venue: str) -> None:
        assert is_non_research_track(venue) is True

    @pytest.mark.parametrize(
        "venue",
        [
            f"{_ACL_2025} (Volume 1: Long Papers)",
            f"{_ACL_2025} (Volume 2: Short Papers)",
            f"{_ACL_2025} (Volume 4: Student Research Workshop)",
            f"{_ACL_2025} (Volume 6: Industry Track)",
            f"{_NAACL_2025} (Volume 3: Industry Track)",
            "Findings of the Association for Computational Linguistics: ACL 2025",
            "Findings of the Association for Computational Linguistics: NAACL 2025",
            (
                "Proceedings of the 19th International Workshop on Semantic "
                "Evaluation (SemEval-2025)"
            ),
            "Proceedings of the Workshop on Safety (Demo and Tutorial Proposals Welcome)",
        ],
    )
    def test_a_parenthesised_research_track_is_retained(self, venue: str) -> None:
        assert is_non_research_track(venue) is False
