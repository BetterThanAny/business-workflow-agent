from enum import StrEnum

from business_workflow_agent.auth import Principal, granted_scopes
from business_workflow_agent.tools.registry import ToolDefinition


class AuthorizationDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY_ROLE = "DENY_ROLE"
    DENY_MISSING_SCOPE = "DENY_MISSING_SCOPE"
    DENY_INVALID_ROLE_SCOPE = "DENY_INVALID_ROLE_SCOPE"


def authorize_tool(
    principal: Principal,
    definition: ToolDefinition,
) -> AuthorizationDecision:
    if not principal.roles.intersection(definition.required_roles):
        return AuthorizationDecision.DENY_ROLE
    if definition.required_scope not in principal.scopes:
        return AuthorizationDecision.DENY_MISSING_SCOPE
    if definition.required_scope not in granted_scopes(principal.roles):
        return AuthorizationDecision.DENY_INVALID_ROLE_SCOPE
    return AuthorizationDecision.ALLOW

