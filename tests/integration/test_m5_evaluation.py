from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.evaluation import (
    EvaluationCase,
    evaluate_case,
    evaluate_live_dataset,
    load_evaluation_dataset,
)
from business_workflow_agent.workflow.provider import (
    DeterministicProvider,
    IntentProposal,
    ProviderRequest,
    RepairRequest,
)

DATASET = Path(__file__).parents[2] / "data/eval/agent_cases.jsonl"


class _PersistentlyInvalidProvider(DeterministicProvider):
    def classify(self, request: ProviderRequest) -> IntentProposal:
        proposal = super().classify(request)
        return proposal.model_copy(update={"arguments": {"unexpected": True}})

    def repair(self, request: RepairRequest) -> IntentProposal:
        return request.proposal


def test_live_evaluator_uses_injected_provider_and_runtime_database() -> None:
    cases = load_evaluation_dataset(DATASET)
    report = evaluate_live_dataset(
        [next(case for case in cases if case.id == "knowledge-001")],
        provider=DeterministicProvider(),
        database_url="sqlite+pysqlite:///:memory:",
    )

    assert report.metrics.case_count == 1
    assert report.results[0].output["schema_repair_attempts"] == 0


def test_live_evaluator_records_persistently_invalid_model_output() -> None:
    cases = load_evaluation_dataset(DATASET)
    report = evaluate_live_dataset(
        [next(case for case in cases if case.id == "approval-003")],
        provider=_PersistentlyInvalidProvider(),
        database_url="sqlite+pysqlite:///:memory:",
    )

    assert report.metrics.case_count == 1
    assert report.results[0].task_success is False
    assert report.results[0].output["schema_repair_attempts"] == 1


def test_one_real_case_per_required_category_passes_end_to_end() -> None:
    cases = load_evaluation_dataset(DATASET)
    selected: dict[str, EvaluationCase] = {}
    for case in cases:
        selected.setdefault(case.task_type, case)

    results = [evaluate_case(case) for case in selected.values()]

    assert len(results) == 8
    assert all(result.task_success for result in results)
    assert sum(result.permission_violations for result in results) == 0
    assert sum(result.duplicate_side_effects for result in results) == 0
    assert all(result.trajectory for result in results)


def test_llm_eval_platform_http_target_contract_is_authenticated_and_deterministic(
    client: TestClient,
    principal_factory: Callable[..., Principal],
    auth_headers: Callable[[Principal], dict[str, str]],
) -> None:
    case = load_evaluation_dataset(DATASET)[0]
    admin = principal_factory(Role.ADMIN)
    support = principal_factory(Role.SUPPORT_AGENT)

    payload = {"input": case.input.model_dump(mode="json")}
    missing_auth = client.post("/api/v1/evaluation/target", json=payload)
    insufficient_role = client.post(
        "/api/v1/evaluation/target",
        json=payload,
        headers=auth_headers(support),
    )
    response = client.post(
        "/api/v1/evaluation/target",
        json=payload,
        headers=auth_headers(admin),
    )

    assert missing_auth.status_code == 401
    assert insufficient_role.status_code == 403
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"output", "raw_response", "metadata"}
    assert body["output"]["final_state"] == case.expected_output.final_state
    assert body["output"]["tool_calls"][0]["name"] == "search_knowledge_base"
    assert body["output"]["side_effects"] == []
    assert body["raw_response"]["trajectory"]
    assert body["metadata"] == {
        "adapter": "llm-eval-platform-http",
        "dataset_version": "m5-v1",
    }
