import argparse
import json
from pathlib import Path

from business_workflow_agent.evaluation import evaluate_dataset, load_evaluation_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic M5 agent evaluation.")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional path for the complete sample-level report and failed trajectories.",
    )
    args = parser.parse_args()

    cases = load_evaluation_dataset(args.dataset)
    report = evaluate_dataset(cases)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            report.model_dump_json(indent=2),
            encoding="utf-8",
        )

    summary = report.metrics.model_dump(mode="json", exclude={"failed_trajectories"})
    summary["dataset_version"] = report.dataset_version
    summary["failed_case_ids"] = sorted(report.metrics.failed_trajectories)
    summary["report_path"] = str(args.report) if args.report is not None else None
    print(json.dumps(summary, sort_keys=True))
    return 0 if report.metrics.release_gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
