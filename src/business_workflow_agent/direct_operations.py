from uuid import UUID

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.execution import DirectOperationDefinition
from business_workflow_agent.schemas import (
    CustomerCreatedOutput,
    CustomerCreateInput,
    IssueRefundInput,
    RefundOutput,
)
from business_workflow_agent.services import BusinessService
from business_workflow_agent.tools.registry import RiskClass


def _create_customer(
    service: BusinessService,
    data: CustomerCreateInput,
    principal: Principal,
    _run_id: UUID,
) -> CustomerCreatedOutput:
    customer = service.create_customer(principal, data)
    return CustomerCreatedOutput.model_validate(customer)


def _issue_refund(
    service: BusinessService,
    data: IssueRefundInput,
    principal: Principal,
    _run_id: UUID,
) -> RefundOutput:
    return RefundOutput.model_validate(service.issue_refund(principal, data))


CREATE_CUSTOMER_OPERATION = DirectOperationDefinition(
    name="crm.create_customer",
    version="1.0.0",
    input_model=CustomerCreateInput,
    output_model=CustomerCreatedOutput,
    risk=RiskClass.WRITE_LOW_RISK,
    required_roles=frozenset({Role.ADMIN}),
    required_scope="customer:write",
    pii_fields=frozenset({"name", "email"}),
    handler=_create_customer,
)


ISSUE_REFUND_OPERATION = DirectOperationDefinition(
    name="refund_service.issue_refund",
    version="1.0.0",
    input_model=IssueRefundInput,
    output_model=RefundOutput,
    risk=RiskClass.WRITE_HIGH_RISK,
    required_roles=frozenset({Role.ADMIN}),
    required_scope="refund:issue",
    pii_fields=frozenset({"reason"}),
    handler=_issue_refund,
)

