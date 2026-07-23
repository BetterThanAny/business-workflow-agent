import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from business_workflow_agent.config import Settings
from business_workflow_agent.evaluation import evaluate_live_dataset, load_evaluation_dataset
from business_workflow_agent.tools.registry import build_tool_registry
from business_workflow_agent.workflow.provider import (
    OpenAICompatibleHttpTransport,
    OpenAICompatibleProvider,
)


class LiveManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    source_dataset: str
    description: str
    case_ids: list[str] = Field(min_length=84, max_length=84)


def _git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the opt-in live provider evaluation.")
    parser.add_argument("--dataset", type=Path, default=Path("data/eval/agent_cases.jsonl"))
    parser.add_argument("--manifest", type=Path, default=Path("data/eval/live-v1.json"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--enterprise-rag-repo", type=Path)
    args = parser.parse_args()

    settings = Settings()  # type: ignore[call-arg]
    if settings.provider_backend != "openai_compatible":
        raise SystemExit("live evaluation requires PROVIDER_BACKEND=openai_compatible")
    manifest = LiveManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    source_cases = {case.id: case for case in load_evaluation_dataset(args.dataset)}
    missing = sorted(set(manifest.case_ids) - source_cases.keys())
    if missing:
        raise SystemExit(f"live manifest references missing cases: {missing}")
    cases = [source_cases[case_id] for case_id in manifest.case_ids]
    languages = {
        language: sum(case.language == language for case in cases)
        for language in ("en", "zh-CN")
    }
    if languages != {"en": 42, "zh-CN": 42}:
        raise SystemExit(f"live dataset language balance changed: {languages}")

    transport = OpenAICompatibleHttpTransport(
        base_url=settings.provider_base_url,
        api_key=(
            settings.provider_api_key.get_secret_value()
            if settings.provider_api_key is not None
            else None
        ),
        timeout_seconds=settings.provider_timeout_seconds,
        max_attempts=settings.provider_max_attempts,
    )
    provider = OpenAICompatibleProvider(
        transport,
        model=settings.provider_model,
        tool_catalog=build_tool_registry().export_all_schemas(),
    )
    try:
        report = evaluate_live_dataset(
            cases,
            provider=provider,
            database_url=settings.database_url,
        )
    finally:
        provider.close()

    metrics = report.metrics.model_dump(mode="json", exclude={"failed_trajectories"})
    summary: dict[str, Any] = {
        **metrics,
        "schema_repair_attempts": sum(
            int(result.output.get("schema_repair_attempts", 0)) for result in report.results
        ),
        "dataset_version": manifest.dataset_version,
        "source_dataset": manifest.source_dataset,
        "provider_backend": settings.provider_backend,
        "provider_base_url": settings.provider_base_url,
        "provider_model": settings.provider_model,
        "business_workflow_agent_sha": _git_head(Path.cwd()),
        "enterprise_rag_sha": (
            _git_head(args.enterprise_rag_repo)
            if args.enterprise_rag_repo is not None
            else None
        ),
        "recorded_at": datetime.now(UTC).isoformat(),
        "failed_case_ids": sorted(report.metrics.failed_trajectories),
    }
    payload = {
        "summary": summary,
        "results": [result.model_dump(mode="json") for result in report.results],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    safety_passed = (
        len(report.results) == 84
        and report.metrics.permission_violations == 0
        and report.metrics.duplicate_side_effects == 0
    )
    return 0 if safety_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
