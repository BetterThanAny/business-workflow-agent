from uuid import uuid4

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.policy import AuthorizationDecision, authorize_tool
from business_workflow_agent.tools.registry import build_tool_registry


def test_role_and_token_scope_are_both_required() -> None:
    definition = build_tool_registry().get("create_ticket")
    principal = Principal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        roles=frozenset({Role.SUPPORT_AGENT}),
        scopes=frozenset({"ticket:read"}),
    )

    decision = authorize_tool(principal, definition)

    assert decision is AuthorizationDecision.DENY_MISSING_SCOPE


def test_model_supplied_role_cannot_change_server_principal() -> None:
    definition = build_tool_registry().get("issue_refund")
    principal = Principal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        roles=frozenset({Role.SUPPORT_AGENT}),
        scopes=frozenset({"refund:issue"}),
    )

    decision = authorize_tool(principal, definition)

    assert decision is AuthorizationDecision.DENY_ROLE

