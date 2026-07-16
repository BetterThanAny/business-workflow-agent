from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from business_workflow_agent.auth import Role
from business_workflow_agent.knowledge import DeterministicKnowledgeBackend, KnowledgeBackend
from business_workflow_agent.schemas import (
    ApprovalOutput,
    CalculateRefundInput,
    CreateTicketInput,
    CustomerOutput,
    GetCustomerInput,
    IssueRefundInput,
    ListCustomerTicketsInput,
    RefundOutput,
    RefundQuoteOutput,
    RequestHumanApprovalInput,
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseOutput,
    TicketListOutput,
    TicketOutput,
    UpdateTicketInput,
)


class RiskClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    WRITE_LOW_RISK = "WRITE_LOW_RISK"
    WRITE_HIGH_RISK = "WRITE_HIGH_RISK"


ToolHandler = Callable[..., BaseModel]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk: RiskClass
    required_roles: frozenset[Role]
    required_scope: str
    timeout_seconds: float
    max_retries: int
    idempotency_required: bool
    pii_fields: frozenset[str]
    audit_event_fields: tuple[str, ...]
    handler: ToolHandler
    deterministic_test_double: str


class ToolRegistry:
    def __init__(self, definitions: list[ToolDefinition]) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if definition.name in self._definitions:
                raise ValueError(f"duplicate tool registration: {definition.name}")
            self._definitions[definition.name] = definition

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise KeyError(f"unregistered tool: {name}") from exc

    def export_schema(self, name: str) -> dict[str, Any]:
        definition = self.get(name)
        return {
            "name": definition.name,
            "version": definition.version,
            "input_schema": definition.input_model.model_json_schema(),
            "output_schema": definition.output_model.model_json_schema(),
            "risk": definition.risk.value,
            "required_roles": sorted(role.value for role in definition.required_roles),
            "required_scope": definition.required_scope,
            "timeout_seconds": definition.timeout_seconds,
            "max_retries": definition.max_retries,
            "idempotency_required": definition.idempotency_required,
            "pii_fields": sorted(definition.pii_fields),
            "audit_event_fields": list(definition.audit_event_fields),
            "deterministic_test_double": definition.deterministic_test_double,
        }

    def export_all_schemas(self) -> list[dict[str, Any]]:
        return [self.export_schema(name) for name in self.names()]


def build_tool_registry(*, knowledge_backend: KnowledgeBackend | None = None) -> ToolRegistry:
    from business_workflow_agent.tools.handlers import (
        build_search_knowledge_handler,
        calculate_refund,
        create_ticket,
        get_customer,
        issue_refund,
        list_customer_tickets,
        request_human_approval,
        update_ticket,
    )

    resolved_knowledge_backend = knowledge_backend or DeterministicKnowledgeBackend()
    search_knowledge_base = build_search_knowledge_handler(resolved_knowledge_backend)
    common_audit_fields = ("user_id", "tenant_id", "run_id", "tool_name", "result")
    read_roles = frozenset({Role.SUPPORT_AGENT, Role.REFUND_MANAGER, Role.AUDITOR, Role.ADMIN})
    return ToolRegistry(
        [
            ToolDefinition(
                "search_knowledge_base",
                "1.0.0",
                SearchKnowledgeBaseInput,
                SearchKnowledgeBaseOutput,
                RiskClass.READ_ONLY,
                frozenset({Role.SUPPORT_AGENT, Role.REFUND_MANAGER, Role.ADMIN}),
                "knowledge:read",
                3.0,
                1,
                False,
                frozenset({"query"}),
                common_audit_fields,
                search_knowledge_base,
                "in_memory_knowledge_catalog_v1",
            ),
            ToolDefinition(
                "get_customer",
                "1.0.0",
                GetCustomerInput,
                CustomerOutput,
                RiskClass.READ_ONLY,
                read_roles,
                "customer:read",
                2.0,
                1,
                False,
                frozenset({"name", "email"}),
                common_audit_fields,
                get_customer,
                "transactional_database_fixture",
            ),
            ToolDefinition(
                "list_customer_tickets",
                "1.0.0",
                ListCustomerTicketsInput,
                TicketListOutput,
                RiskClass.READ_ONLY,
                read_roles,
                "ticket:read",
                2.0,
                1,
                False,
                frozenset({"title", "description"}),
                common_audit_fields,
                list_customer_tickets,
                "transactional_database_fixture",
            ),
            ToolDefinition(
                "create_ticket",
                "1.0.0",
                CreateTicketInput,
                TicketOutput,
                RiskClass.WRITE_LOW_RISK,
                frozenset({Role.SUPPORT_AGENT, Role.ADMIN}),
                "ticket:write",
                5.0,
                0,
                True,
                frozenset({"title", "description"}),
                common_audit_fields,
                create_ticket,
                "transactional_database_fixture",
            ),
            ToolDefinition(
                "update_ticket",
                "1.0.0",
                UpdateTicketInput,
                TicketOutput,
                RiskClass.WRITE_LOW_RISK,
                frozenset({Role.SUPPORT_AGENT, Role.ADMIN}),
                "ticket:write",
                5.0,
                0,
                True,
                frozenset({"title", "description"}),
                common_audit_fields,
                update_ticket,
                "transactional_database_fixture",
            ),
            ToolDefinition(
                "calculate_refund",
                "1.0.0",
                CalculateRefundInput,
                RefundQuoteOutput,
                RiskClass.READ_ONLY,
                frozenset({Role.SUPPORT_AGENT, Role.REFUND_MANAGER, Role.ADMIN}),
                "refund:calculate",
                2.0,
                0,
                False,
                frozenset({"reason"}),
                common_audit_fields,
                calculate_refund,
                "deterministic_refund_policy_v1",
            ),
            ToolDefinition(
                "issue_refund",
                "1.0.0",
                IssueRefundInput,
                RefundOutput,
                RiskClass.WRITE_HIGH_RISK,
                frozenset({Role.REFUND_MANAGER, Role.ADMIN}),
                "refund:issue",
                10.0,
                0,
                True,
                frozenset({"reason"}),
                common_audit_fields,
                issue_refund,
                "transactional_database_fixture",
            ),
            ToolDefinition(
                "request_human_approval",
                "1.0.0",
                RequestHumanApprovalInput,
                ApprovalOutput,
                RiskClass.WRITE_LOW_RISK,
                frozenset({Role.SUPPORT_AGENT, Role.REFUND_MANAGER, Role.ADMIN}),
                "approval:request",
                3.0,
                0,
                True,
                frozenset({"tool_arguments", "reason"}),
                common_audit_fields,
                request_human_approval,
                "transactional_database_fixture",
            ),
        ]
    )
