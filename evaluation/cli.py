"""Command-line entrypoint for the ScholarLens evaluation harness."""

import argparse
from pathlib import Path

from evaluation.report import run_evaluation


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Evaluate ScholarLens research-gap reports"
    )
    command.add_argument("--gaps", required=True, type=Path)
    command.add_argument("--future-papers", required=True, type=Path)
    command.add_argument("--surveys", required=True, type=Path)
    command.add_argument("--expert-scores", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--similarity-threshold", type=float, default=0.2)
    return command


def main() -> None:
    args = parser().parse_args()
    report = run_evaluation(
        args.gaps,
        args.future_papers,
        args.surveys,
        args.expert_scores,
        args.output,
        args.similarity_threshold,
    )
    summary = report["summary"]
    print(f"Evaluated {summary['gaps_evaluated']} gaps")
    print(f"Validity index: {summary['validity_index']:.3f}")
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
