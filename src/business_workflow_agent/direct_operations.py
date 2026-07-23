from uuid import UUID

from business_workflow_agent.auth import Principal, Role
from business_workflow_agent.execution import DirectOperationDefinition
from business_workflow_agent.schemas import (
    CustomerCreatedOutput,
    CustomerCreateInput,
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


