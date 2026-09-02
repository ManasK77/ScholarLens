from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class GapSignals:
    mention_frequency: float
    trend_slope: float
    solution_density: float
    citation_impact: float
    contradiction_flag: float


@dataclass(frozen=True, slots=True)
class GapScore:
    score: float
    breakdown: dict[str, float]


def score_gap(signals: GapSignals, weights: dict[str, float]) -> GapScore:
    values = asdict(signals)
    breakdown = {
        name: value * weights[name] * (-1.0 if name == "solution_density" else 1.0)
        for name, value in values.items()
    }
    return GapScore(sum(breakdown.values()), breakdown)
