"""Typed records shared by the three evaluation strategies."""

from dataclasses import dataclass


def _score(value: object, field: str) -> int:
    score = int(value)
    if not 1 <= score <= 5:
        raise ValueError(f"{field} must be between 1 and 5")
    return score


@dataclass(frozen=True, slots=True)
class GapCandidate:
    gap_id: str
    description: str
    cutoff_year: int
    relevant_future_paper_ids: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, row: dict) -> "GapCandidate":
        return cls(
            gap_id=str(row["gap_id"]),
            description=str(row["description"]),
            cutoff_year=int(row["cutoff_year"]),
            relevant_future_paper_ids=tuple(
                str(value) for value in row.get("relevant_future_paper_ids", [])
            ),
        )


@dataclass(frozen=True, slots=True)
class PaperRecord:
    paper_id: str
    title: str
    year: int
    abstract: str = ""
    keywords: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, row: dict) -> "PaperRecord":
        return cls(
            paper_id=str(row["paper_id"]),
            title=str(row["title"]),
            year=int(row["year"]),
            abstract=str(row.get("abstract", "")),
            keywords=tuple(str(value) for value in row.get("keywords", [])),
        )

    @property
    def searchable_text(self) -> str:
        return " ".join((self.title, self.abstract, *self.keywords))


@dataclass(frozen=True, slots=True)
class SurveyRecord:
    survey_id: str
    title: str
    year: int
    open_problems: tuple[str, ...]

    @classmethod
    def from_dict(cls, row: dict) -> "SurveyRecord":
        problems = tuple(str(value) for value in row.get("open_problems", []))
        if not problems:
            raise ValueError("survey open_problems must not be empty")
        return cls(
            survey_id=str(row["survey_id"]),
            title=str(row["title"]),
            year=int(row["year"]),
            open_problems=problems,
        )


@dataclass(frozen=True, slots=True)
class ExpertScore:
    gap_id: str
    rater_id: str
    novelty: int
    validity: int
    evidence_quality: int
    actionability: int
    notes: str = ""

    @classmethod
    def from_dict(cls, row: dict) -> "ExpertScore":
        return cls(
            gap_id=str(row["gap_id"]),
            rater_id=str(row["rater_id"]),
            novelty=_score(row["novelty"], "novelty"),
            validity=_score(row["validity"], "validity"),
            evidence_quality=_score(row["evidence_quality"], "evidence_quality"),
            actionability=_score(row["actionability"], "actionability"),
            notes=str(row.get("notes", "")),
        )
