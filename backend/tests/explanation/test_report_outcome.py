"""What one Stage 9 pass reports about itself, and how it resumes (D-09-08).

The first clean baseline asked for five reports, stored two, and answered
`POST /pipeline/run` with a plain 200 and `reports=[2 ids]` -- nothing said
that three of the five had failed. It also marked S9 as current, so the next
run would have served the two reports and never retried the other three.

`generate_reports` returns a `ReportOutcome` that separates:

* **failures** -- a generation that came back unusable (schema-invalid,
  truncated, ungrounded). Retryable; they make a pass `partial` or `failed`.
* **skips** -- a gap with nothing to explain (no retrievable evidence, no
  label or members). Deterministic, so they never make a pass incomplete.

Everything runs against fakes; no test calls Groq.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.explanation.llm import (
    LLMAuthError,
    LLMConfigurationError,
    LLMOutputError,
    LLMRateLimitError,
    LLMTruncatedError,
)
from app.explanation.service import ReportOutcome, generate_reports
from app.explanation.storage import list_reports
from app.ingestion import corpus
from tests.explanation.test_service import (
    SETTINGS,
    FakeIndex,
    FakeLLM,
    _draft,
    _point,
    _seed,
)

VALIDATE = LLMOutputError(
    "Groq's structured output did not validate against the report schema: "
    "the generation is not parseable JSON (812 chars; cut off mid-object?)."
)
TRUNCATED = LLMTruncatedError(
    "Groq's response was truncated by max_completion_tokens (2048); raise "
    "GROQ_MAX_COMPLETION_TOKENS."
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _index():
    return FakeIndex(points=[_point("p1", 101, "English prompts only.")])


def _five(session):
    return _seed(
        session, labels=("a", "b", "c", "d", "e"), scores=[9.0, 7.0, 5.0, 3.0, 1.0]
    )


# ---------------------------------------------------------------------------
# D. Partial S9 success -- the baseline's exact shape
# ---------------------------------------------------------------------------


class TestPartialSuccess:
    def test_two_of_five_is_reported_as_partial_with_each_failure_named(self, session):
        ids = _five(session)
        llm = FakeLLM([VALIDATE, TRUNCATED, _draft(["p1"]), _draft(["p1"]), VALIDATE])

        outcome = generate_reports(session, SETTINGS, _index(), llm)

        assert outcome.status == "partial"
        assert outcome.requested == 5
        assert outcome.stored == [ids[2], ids[3]]
        assert [f["cluster_id"] for f in outcome.failures] == [ids[0], ids[1], ids[4]]
        assert "did not validate" in outcome.failures[0]["reason"]
        assert "truncated" in outcome.failures[1]["reason"]

    def test_the_successful_reports_are_stored_despite_the_failures(self, session):
        """C. One failed report never costs the others their storage."""
        ids = _five(session)
        llm = FakeLLM([VALIDATE, TRUNCATED, _draft(["p1"]), _draft(["p1"]), VALIDATE])

        generate_reports(session, SETTINGS, _index(), llm)

        assert [row.cluster_id for row in list_reports(session)] == [ids[2], ids[3]]

    def test_a_report_citing_nothing_retrieved_is_a_failure_not_a_skip(self, session):
        ids = _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        llm = FakeLLM([_draft(["invented"]), _draft(["p1"])])

        outcome = generate_reports(session, SETTINGS, _index(), llm)

        assert outcome.status == "partial"
        assert outcome.failures[0]["cluster_id"] == ids[0]
        assert "cited" in outcome.failures[0]["reason"]

    def test_a_malformed_draft_is_a_failure_with_a_bounded_reason(self, session):
        ids = _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        llm = FakeLLM(
            [{"gap": "no confidence", "evidence_ids": ["p1"]}, _draft(["p1"])]
        )

        outcome = generate_reports(session, SETTINGS, _index(), llm)

        assert outcome.failures[0]["cluster_id"] == ids[0]
        # The model's own output is not echoed back through the API.
        assert "no confidence" not in outcome.failures[0]["reason"]

    def test_the_summary_is_the_additive_api_shape(self, session):
        _five(session)
        llm = FakeLLM([VALIDATE, TRUNCATED, _draft(["p1"]), _draft(["p1"]), VALIDATE])

        summary = generate_reports(session, SETTINGS, _index(), llm).as_dict()

        assert summary["status"] == "partial"
        assert summary["requested"] == 5
        assert summary["succeeded"] == 2
        assert summary["failed"] == 3
        assert summary["skipped"] == 0
        assert len(summary["failures"]) == 3
        assert set(summary["failures"][0]) == {"cluster_id", "reason"}


# ---------------------------------------------------------------------------
# E. Complete S9 failure
# ---------------------------------------------------------------------------


class TestCompleteFailure:
    def test_every_generation_failing_is_reported_as_failed(self, session):
        _seed(session, labels=("a", "b"), scores=[9.0, 1.0])

        outcome = generate_reports(session, SETTINGS, _index(), FakeLLM([VALIDATE]))

        assert outcome.status == "failed"
        assert outcome.stored == []
        assert len(outcome.failures) == 2

    def test_a_failed_pass_keeps_the_reports_already_stored(self, session):
        """Nothing new to store is not a reason to wipe what is there."""
        [cluster_id] = _seed(session)
        generate_reports(session, SETTINGS, _index(), FakeLLM([_draft(["p1"])]))

        outcome = generate_reports(session, SETTINGS, _index(), FakeLLM([TRUNCATED]))

        assert outcome.status == "failed"
        assert [row.cluster_id for row in list_reports(session)] == [cluster_id]

    @pytest.mark.parametrize(
        "error",
        [
            LLMAuthError("Groq rejected the configured GROQ_API_KEY."),
            LLMConfigurationError("The model `nope` does not exist"),
        ],
    )
    def test_a_deployment_error_stops_the_pass_after_one_call(self, session, error):
        """A rejected key or unknown model fails every gap the same way; at a
        60s pacing interval, trying all five would cost five minutes."""
        _five(session)
        llm = FakeLLM([error])

        with pytest.raises(type(error)):
            generate_reports(session, SETTINGS, _index(), llm)

        assert len(llm.prompts) == 1


# ---------------------------------------------------------------------------
# F. Complete S9 success
# ---------------------------------------------------------------------------


class TestCompleteSuccess:
    def test_every_report_stored_is_complete(self, session):
        ids = _five(session)

        outcome = generate_reports(
            session, SETTINGS, _index(), FakeLLM([_draft(["p1"])])
        )

        assert outcome.status == "complete"
        assert outcome.stored == ids
        assert outcome.failures == []

    def test_a_gap_with_no_evidence_is_skipped_and_still_complete(self, session):
        ids = _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        index = FakeIndex(
            points_by_call=[[], [_point("p1", 101, "English prompts only.")]]
        )

        outcome = generate_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))

        assert outcome.status == "complete"
        assert outcome.stored == [ids[1]]
        assert [skip["cluster_id"] for skip in outcome.skips] == [ids[0]]

    def test_no_rankings_is_complete_with_nothing_requested(self, session):
        outcome = generate_reports(session, SETTINGS, _index(), FakeLLM([]))

        assert outcome.status == "complete"
        assert outcome.requested == 0


# ---------------------------------------------------------------------------
# Resume: retry only what failed, keep what succeeded
# ---------------------------------------------------------------------------


class TestResume:
    def test_resume_regenerates_only_the_gaps_without_a_report(self, session):
        ids = _five(session)
        generate_reports(
            session,
            SETTINGS,
            _index(),
            FakeLLM([VALIDATE, TRUNCATED, _draft(["p1"]), _draft(["p1"]), VALIDATE]),
        )
        retry = FakeLLM([_draft(["p1"])])

        outcome = generate_reports(session, SETTINGS, _index(), retry, resume=True)

        assert len(retry.prompts) == 3
        assert outcome.status == "complete"
        assert outcome.stored == ids
        assert sorted(outcome.reused) == sorted([ids[2], ids[3]])
        assert [row.cluster_id for row in list_reports(session)] == ids

    def test_a_reused_report_is_stored_unchanged(self, session):
        ids = _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        generate_reports(
            session,
            SETTINGS,
            _index(),
            FakeLLM([_draft(["p1"], gap="Kept gap."), VALIDATE]),
        )

        generate_reports(
            session, SETTINGS, _index(), FakeLLM([_draft(["p1"])]), resume=True
        )

        stored = {row.cluster_id: row.gap for row in list_reports(session)}
        assert stored[ids[0]] == "Kept gap."

    def test_a_resume_that_fails_again_keeps_the_earlier_successes(self, session):
        """C across passes: a retry can never lose a report it already had."""
        ids = _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        generate_reports(
            session, SETTINGS, _index(), FakeLLM([_draft(["p1"]), VALIDATE])
        )

        outcome = generate_reports(
            session, SETTINGS, _index(), FakeLLM([VALIDATE]), resume=True
        )

        assert outcome.status == "partial"
        assert [row.cluster_id for row in list_reports(session)] == [ids[0]]

    def test_resume_with_everything_stored_makes_no_llm_call(self, session):
        _seed(session)
        generate_reports(session, SETTINGS, _index(), FakeLLM([_draft(["p1"])]))

        outcome = generate_reports(session, SETTINGS, _index(), None, resume=True)

        assert outcome.status == "complete"

    def test_without_resume_every_gap_is_regenerated(self, session):
        """`POST /reports` and a changed report configuration regenerate all."""
        _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        generate_reports(session, SETTINGS, _index(), FakeLLM([_draft(["p1"])]))
        again = FakeLLM([_draft(["p1"])])

        generate_reports(session, SETTINGS, _index(), again)

        assert len(again.prompts) == 2

    def test_a_rate_limit_still_aborts_and_keeps_the_previous_reports(self, session):
        ids = _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        generate_reports(
            session, SETTINGS, _index(), FakeLLM([_draft(["p1"]), VALIDATE])
        )

        with pytest.raises(LLMRateLimitError):
            generate_reports(
                session,
                SETTINGS,
                _index(),
                FakeLLM([LLMRateLimitError("Groq rate limit reached.")]),
                resume=True,
            )

        assert [row.cluster_id for row in list_reports(session)] == [ids[0]]


def test_report_outcome_status_rules():
    assert ReportOutcome(requested=2, stored=[1, 2]).status == "complete"
    assert (
        ReportOutcome(
            requested=2, stored=[1], failures=[{"cluster_id": 2, "reason": "x"}]
        ).status
        == "partial"
    )
    assert (
        ReportOutcome(requested=1, failures=[{"cluster_id": 1, "reason": "x"}]).status
        == "failed"
    )
