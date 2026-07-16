from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.evaluation import evaluate_case, load_evaluation_dataset

DATASET = Path(__file__).parents[2] / "data/eval/agent_cases.jsonl"


def test_one_real_case_per_required_category_passes_end_to_end() -> None:
    cases = load_evaluation_dataset(DATASET)
    selected = {}
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
