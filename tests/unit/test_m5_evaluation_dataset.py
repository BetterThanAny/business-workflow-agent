import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from business_workflow_agent.evaluation import (
    EvaluationCase,
    EvaluationMetrics,
    EvaluationResult,
    load_evaluation_dataset,
)


def test_live_manifest_selects_84_balanced_non_timeout_cases() -> None:
    manifest = json.loads(Path("data/eval/live-v1.json").read_text(encoding="utf-8"))
    cases = {case.id: case for case in load_evaluation_dataset(Path("data/eval/agent_cases.jsonl"))}
    selected = [cases[case_id] for case_id in manifest["case_ids"]]

    assert manifest["dataset_version"] == "live-v1"
    assert len(selected) == len(set(manifest["case_ids"])) == 84
    assert sum(case.language == "en" for case in selected) == 42
    assert sum(case.language == "zh-CN" for case in selected) == 42
    assert all(case.task_type != "provider_timeout" for case in selected)

DATASET = Path(__file__).parents[2] / "data/eval/agent_cases.jsonl"


def test_versioned_dataset_has_160_unique_platform_compatible_cases() -> None:
    cases = load_evaluation_dataset(DATASET)

    assert len(cases) == 160
    assert len({case.id for case in cases}) == 160
    assert {case.dataset_version for case in cases} == {"m5-v1"}
    assert Counter(case.task_type for case in cases) == {
        "knowledge_qa": 20,
        "missing_parameters": 20,
        "multi_tool": 20,
        "authorization": 20,
        "prompt_injection": 20,
        "provider_timeout": 20,
        "approval": 20,
        "replay": 20,
    }
    assert sum("adversarial" in case.tags for case in cases) >= 80

    first_row = json.loads(DATASET.read_text().splitlines()[0])
    assert {
        "id",
        "input",
        "expected_output",
        "tags",
        "language",
        "difficulty",
        "task_type",
    }.issubset(first_row)


def test_evaluation_case_rejects_unknown_fields_and_invalid_replay_count() -> None:
    valid = load_evaluation_dataset(DATASET)[0].model_dump(mode="json")

    with pytest.raises(ValidationError):
        EvaluationCase.model_validate({**valid, "model_authorized": True})
    valid["input"]["replay_count"] = 0
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate(valid)


def test_dataset_loader_rejects_empty_malformed_duplicate_and_inconsistent_cases(
    tmp_path: Path,
) -> None:
    valid_row = json.loads(DATASET.read_text().splitlines()[0])
    candidate = tmp_path / "candidate.jsonl"

    candidate.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="contains no cases"):
        load_evaluation_dataset(candidate)

    candidate.write_text("{not-json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid evaluation case at line 1"):
        load_evaluation_dataset(candidate)

    encoded = json.dumps(valid_row)
    candidate.write_text(f"{encoded}\n{encoded}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="IDs must be unique"):
        load_evaluation_dataset(candidate)

    valid_row["input"]["task_type"] = "approval"
    candidate.write_text(json.dumps(valid_row), encoding="utf-8")
    with pytest.raises(ValueError, match="inconsistent duplicated routing fields"):
        load_evaluation_dataset(candidate)


def test_metrics_use_explicit_denominators_and_zero_tolerance_gates() -> None:
    results = [
        EvaluationResult(
            case_id="pass",
            task_type="knowledge_qa",
            task_success=True,
            tool_matches=1,
            tool_denominator=1,
            argument_matches=1,
            argument_denominator=1,
            step_count=4,
            permission_violations=0,
            duplicate_side_effects=0,
            orchestration_ms=10,
            output={},
            trajectory=[],
        ),
        EvaluationResult(
            case_id="fail",
            task_type="authorization",
            task_success=False,
            tool_matches=0,
            tool_denominator=1,
            argument_matches=0,
            argument_denominator=1,
            step_count=3,
            permission_violations=1,
            duplicate_side_effects=1,
            orchestration_ms=20,
            output={},
            trajectory=[{"kind": "error", "error_code": "POLICY_DENY_ROLE"}],
        ),
    ]

    metrics = EvaluationMetrics.from_results(results)

    assert metrics.case_count == 2
    assert metrics.task_success_rate == 0.5
    assert metrics.preferred_tool_accuracy == 0.5
    assert metrics.argument_accuracy == 0.5
    assert metrics.permission_violations == 1
    assert metrics.duplicate_side_effects == 1
    assert metrics.orchestration_p95_ms == 20
    assert metrics.release_gate_passed is False
    assert metrics.failed_trajectories == {"fail": results[1].trajectory}

    with pytest.raises(ValueError, match="at least one result"):
        EvaluationMetrics.from_results([])
