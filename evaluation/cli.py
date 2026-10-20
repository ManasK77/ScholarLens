"""Command-line entrypoint for the ScholarLens evaluation harness."""

import argparse
from pathlib import Path

from evaluation.report import run_evaluation


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Evaluate ScholarLens research-gap reports"
    )
    command.add_argument("--gaps", required=True, type=Path)
    # Independent evidence. A method whose input is missing is reported as
    # unavailable, not scored as zero.
    command.add_argument("--future-papers", type=Path)
    command.add_argument("--surveys", type=Path)
    command.add_argument("--expert-scores", type=Path)
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--similarity-threshold", type=float, default=0.2)
    return command


def print_summary(report: dict, output: Path) -> None:
    summary = report["summary"]
    print(f"Evaluated {summary['gaps_evaluated']} gaps")
    index = summary["validity_index"]
    print(
        f"Validity index: {index:.3f} (from {', '.join(summary['components_used'])})"
        if index is not None
        else "Validity index: not computable -- no independent evidence supplied"
    )
    for method, state in report["availability"].items():
        if state["status"] != "computed":
            print(
                f"  {method}: unavailable ({state['reason']}); needs {state['requires']}"
            )
    print(f"Report: {output}")


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
    print_summary(report, args.output)


if __name__ == "__main__":
    main()
