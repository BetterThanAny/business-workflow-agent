from typing import Any

import pytest
from jsonschema.validators import validator_for
from pydantic import ValidationError

from business_workflow_agent.schemas import CreateTicketInput, CustomerCreateInput
from business_workflow_agent.tools.registry import RiskClass, build_tool_registry

EXPECTED_TOOLS = {
    "search_knowledge_base",
    "get_customer",
    "list_customer_tickets",
    "create_ticket",
    "update_ticket",
    "calculate_refund",
    "issue_refund",
    "request_human_approval",
}


def test_registry_exports_complete_valid_strict_json_schemas() -> None:
    registry = build_tool_registry()

    assert set(registry.names()) == EXPECTED_TOOLS
    for name in registry.names():
        exported: dict[str, Any] = registry.export_schema(name)
        input_schema = exported["input_schema"]
        output_schema = exported["output_schema"]
        validator_for(input_schema).check_schema(input_schema)
        validator_for(output_schema).check_schema(output_schema)
        assert input_schema["additionalProperties"] is False
        assert exported["version"] == "1.0.0"
        assert exported["timeout_seconds"] > 0
        assert exported["required_scope"]
        assert exported["audit_event_fields"]
        assert exported["deterministic_test_double"]


def test_write_tools_require_idempotency_and_issue_refund_is_high_risk() -> None:
    registry = build_tool_registry()

    for name in ("create_ticket", "update_ticket", "issue_refund", "request_human_approval"):
        assert registry.get(name).idempotency_required is True

    high_risk_tools = {
        name
        for name in registry.names()
        if registry.get(name).risk is RiskClass.WRITE_HIGH_RISK
    }
    assert high_risk_tools == {"issue_refund"}


def test_tool_inputs_reject_unknown_or_coerced_fields() -> None:
    with pytest.raises(ValidationError):
        CreateTicketInput.model_validate(
            {
                "customer_id": "c64ec43c-f9e1-4da2-92d1-992d554dbe43",
                "title": "Cannot sign in",
                "description": "MFA loop",
                "priority": "HIGH",
                "approved": True,
            }
        )

    with pytest.raises(ValidationError):
        CustomerCreateInput.model_validate(
            {"external_id": "customer-1", "name": "Customer", "email": "not-an-email"}
        )

    with pytest.raises(ValidationError):
        CreateTicketInput.model_validate(
            {
                "customer_id": "c64ec43c-f9e1-4da2-92d1-992d554dbe43",
                "title": "Cannot sign in",
                "description": "MFA loop",
                "priority": 1,
            }
        )


def test_registry_rejects_unknown_tool_name() -> None:
    with pytest.raises(KeyError, match="unregistered tool"):
        build_tool_registry().get("invented_tool")
