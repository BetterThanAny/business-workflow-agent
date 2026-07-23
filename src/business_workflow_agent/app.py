import json
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from business_workflow_agent.approvals import (
    ApprovalConflict,
    ApprovalPermissionError,
    ApprovalService,
)
from business_workflow_agent.auth import (
    Principal,
    Role,
    decode_access_token,
    granted_scopes,
)
from business_workflow_agent.config import Settings
from business_workflow_agent.db import create_database_engine, create_session_factory
from business_workflow_agent.direct_operations import CREATE_CUSTOMER_OPERATION
from business_workflow_agent.domain import ApprovalOrigin, ToolExecutionStatus
from business_workflow_agent.evaluation import (
    LLMEvalTargetRequest,
    LLMEvalTargetResponse,
    llm_eval_target,
)
from business_workflow_agent.execution import (
    DirectOperationExecutor,
    IdempotencyConflict,
    ToolExecutor,
)
from business_workflow_agent.knowledge import (
    KnowledgeBackend,
    create_knowledge_backend,
)
from business_workflow_agent.models import AuditEvent, WorkflowRun
from business_workflow_agent.observability import WorkflowTelemetry
from business_workflow_agent.schemas import (
    AgentManualResumeInput,
    AgentRunCreateInput,
    AgentRunOutput,
    AgentRunResumeInput,
    ApprovalDecisionInput,
    ApprovalDecisionOutput,
    ApprovalDetailOutput,
    ApprovalTokenOutput,
    CalculateRefundInput,
    CreateTicketInput,
    CustomerCreatedOutput,
    CustomerCreateInput,
    CustomerOutput,
    IssueRefundInput,
    RefundQuoteOutput,
    RunTrajectoryOutput,
    TicketListOutput,
    TicketOutput,
    ToolExecutionRequest,
    ToolExecutionResponse,
    ToolSchemaListOutput,
    UpdateTicketInput,
    WorkflowRunCreateInput,
    WorkflowRunOutput,
)
from business_workflow_agent.services import BusinessService, ResourceNotFound
from business_workflow_agent.tools.registry import ToolRegistry, build_tool_registry
from business_workflow_agent.trajectory import RunTrajectoryService
from business_workflow_agent.workflow.provider import DeterministicProvider, StructuredProvider
from business_workflow_agent.workflow.runner import AgentRunner, WorkflowResumeError


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    registry: ToolRegistry | None = None,
    provider: StructuredProvider | None = None,
    telemetry: WorkflowTelemetry | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()  # type: ignore[call-arg]
    resolved_engine = engine or create_database_engine(resolved_settings.database_url)
    session_factory = create_session_factory(resolved_engine)
    owned_knowledge_backend: KnowledgeBackend | None = None
    if registry is None:
        owned_knowledge_backend = create_knowledge_backend(resolved_settings)
        tool_registry = build_tool_registry(knowledge_backend=owned_knowledge_backend)
    else:
        tool_registry = registry
    structured_provider = provider or DeterministicProvider()
    workflow_telemetry = telemetry or WorkflowTelemetry()
    agent_runner = AgentRunner(
        session_factory,
        tool_registry,
        structured_provider,
        telemetry=workflow_telemetry,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        try:
            yield
        finally:
            workflow_telemetry.shutdown()
            if owned_knowledge_backend is not None:
                owned_knowledge_backend.close()

    app = FastAPI(
        title="Business Workflow Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.engine = resolved_engine
    app.state.session_factory = session_factory
    app.state.tool_registry = tool_registry
    app.state.structured_provider = structured_provider
    app.state.agent_runner = agent_runner
    app.state.telemetry = workflow_telemetry
    app.state.knowledge_backend = owned_knowledge_backend

    security = HTTPBearer(auto_error=False)

    def get_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def get_principal(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
        try:
            return decode_access_token(
                credentials.credentials,
                secret=resolved_settings.jwt_secret.get_secret_value(),
                issuer=resolved_settings.jwt_issuer,
                audience=resolved_settings.jwt_audience,
            )
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
            ) from exc

    def require_access(
        required_scope: str,
        *,
        roles: frozenset[Role] | None = None,
    ) -> Callable[[Principal], Principal]:
        def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
            if required_scope not in principal.scopes:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing scope")
            if required_scope not in granted_scopes(principal.roles):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid scope")
            if roles is not None and not principal.roles.intersection(roles):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="role denied")
            return principal

        return dependency

    @app.exception_handler(ResourceNotFound)
    async def resource_not_found_handler(
        _request: Request, exc: ResourceNotFound
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_handler(
        _request: Request, exc: IdempotencyConflict
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(WorkflowResumeError)
    async def workflow_resume_handler(
        _request: Request, exc: WorkflowResumeError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ApprovalPermissionError)
    async def approval_permission_handler(
        _request: Request, exc: ApprovalPermissionError
    ) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ApprovalConflict)
    async def approval_conflict_handler(
        _request: Request, exc: ApprovalConflict
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(
            content=workflow_telemetry.prometheus_payload(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.post("/api/v1/workflow-runs", response_model=WorkflowRunOutput, status_code=201)
    def create_workflow_run(
        data: WorkflowRunCreateInput,
        principal: Annotated[Principal, Depends(require_access("workflow:create"))],
        session: Annotated[Session, Depends(get_session)],
    ) -> WorkflowRunOutput:
        with session.begin():
            run = BusinessService(session).create_workflow_run(principal, data.budget)
            session.add(
                AuditEvent(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    run_id=run.id,
                    tool_name="workflow_run.create",
                    state=run.state,
                    event_type="WORKFLOW_RUN_CREATED",
                    payload_redacted={"state": run.state, "version": run.version},
                )
            )
            session.flush()
            return WorkflowRunOutput.model_validate(run)

    @app.post("/api/v1/agent-runs", response_model=AgentRunOutput, status_code=201)
    def create_agent_run(
        data: AgentRunCreateInput,
        principal: Annotated[Principal, Depends(require_access("workflow:create"))],
    ) -> AgentRunOutput:
        created = agent_runner.create(principal, data)
        return agent_runner.run_to_pause(principal, created.id)

    @app.get("/api/v1/agent-runs/{run_id}", response_model=AgentRunOutput)
    def get_agent_run(
        run_id: UUID,
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> AgentRunOutput:
        return agent_runner.get(principal, run_id)

    @app.post("/api/v1/agent-runs/{run_id}/resume", response_model=AgentRunOutput)
    def resume_agent_run(
        run_id: UUID,
        data: AgentRunResumeInput,
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> AgentRunOutput:
        return agent_runner.resume(principal, run_id, data)

    @app.post("/api/v1/agent-runs/{run_id}/manual-resume", response_model=AgentRunOutput)
    def manually_resume_agent_run(
        run_id: UUID,
        data: AgentManualResumeInput,
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> AgentRunOutput:
        return agent_runner.manual_resume(principal, run_id, data)

    @app.post("/api/v1/agent-runs/{run_id}/cancel", response_model=AgentRunOutput)
    def cancel_agent_run(
        run_id: UUID,
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> AgentRunOutput:
        return agent_runner.cancel(principal, run_id)

    @app.get("/api/v1/agent-runs/{run_id}/events")
    def stream_agent_events(
        run_id: UUID,
        principal: Annotated[Principal, Depends(get_principal)],
        after_sequence: int = 0,
    ) -> StreamingResponse:
        events = agent_runner.events(
            principal,
            run_id,
            after_sequence=after_sequence,
        )

        def event_stream() -> Iterator[str]:
            for event in events:
                yield (
                    f"id: {event.sequence}\n"
                    f"event: {event.event_type}\n"
                    f"data: {json.dumps(event.payload, sort_keys=True)}\n\n"
                )

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get(
        "/api/v1/agent-runs/{run_id}/trajectory",
        response_model=RunTrajectoryOutput,
    )
    def get_agent_trajectory(
        run_id: UUID,
        principal: Annotated[Principal, Depends(get_principal)],
        session: Annotated[Session, Depends(get_session)],
    ) -> RunTrajectoryOutput:
        return RunTrajectoryService(session).get(principal, run_id)

    @app.post(
        "/api/v1/evaluation/target",
        response_model=LLMEvalTargetResponse,
    )
    def evaluate_platform_target(
        data: LLMEvalTargetRequest,
        _principal: Annotated[
            Principal,
            Depends(
                require_access(
                    "workflow:create",
                    roles=frozenset({Role.ADMIN}),
                )
            ),
        ],
    ) -> LLMEvalTargetResponse:
        return llm_eval_target(data)

    @app.get("/api/v1/approvals/{approval_id}", response_model=ApprovalDetailOutput)
    def get_approval(
        approval_id: UUID,
        principal: Annotated[
            Principal,
            Depends(
                require_access(
                    "approval:read",
                    roles=frozenset({Role.REFUND_MANAGER, Role.AUDITOR, Role.ADMIN}),
                )
            ),
        ],
        session: Annotated[Session, Depends(get_session)],
    ) -> ApprovalDetailOutput:
        return ApprovalService(session, tool_registry).get(principal, approval_id)

    @app.post(
        "/api/v1/approvals/{approval_id}/decision-token",
        response_model=ApprovalTokenOutput,
        status_code=201,
    )
    def issue_approval_decision_token(
        approval_id: UUID,
        principal: Annotated[
            Principal,
            Depends(
                require_access(
                    "approval:decide",
                    roles=frozenset({Role.REFUND_MANAGER, Role.ADMIN}),
                )
            ),
        ],
        session: Annotated[Session, Depends(get_session)],
    ) -> ApprovalTokenOutput:
        with session.begin():
            result = ApprovalService(session, tool_registry).issue_decision_token(
                principal, approval_id
            )
        if result is None:
            raise HTTPException(
                status_code=409,
                detail="approval is no longer executable; create a new proposal",
            )
        return result

    @app.post(
        "/api/v1/approvals/{approval_id}/decision",
        response_model=ApprovalDecisionOutput,
    )
    def decide_approval(
        approval_id: UUID,
        data: ApprovalDecisionInput,
        principal: Annotated[
            Principal,
            Depends(
                require_access(
                    "approval:decide",
                    roles=frozenset({Role.REFUND_MANAGER, Role.ADMIN}),
                )
            ),
        ],
        session: Annotated[Session, Depends(get_session)],
    ) -> ApprovalDecisionOutput:
        with session.begin():
            result = ApprovalService(session, tool_registry).decide(
                principal, approval_id, data
            )
        if result.approval_status.value == "EXPIRED":
            raise HTTPException(status_code=409, detail="approval expired")
        if result.run_state.value == "VERIFY_RESULT":
            with session_factory() as owner_session:
                run = owner_session.get(WorkflowRun, result.run_id)
                if run is None:
                    raise HTTPException(status_code=404, detail="workflow run not found")
                tenant_id = run.tenant_id
                user_id = run.user_id
            completed = agent_runner.continue_after_approval(
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=result.run_id,
            )
            result = result.model_copy(
                update={"run_state": completed.state, "result": completed.result}
            )
        return result

    @app.post("/api/v1/customers", response_model=CustomerCreatedOutput, status_code=201)
    def create_customer(
        data: CustomerCreateInput,
        principal: Annotated[
            Principal,
            Depends(require_access("customer:write", roles=frozenset({Role.ADMIN}))),
        ],
        session: Annotated[Session, Depends(get_session)],
        run_id: Annotated[UUID, Header(alias="X-Workflow-Run-ID")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8)],
    ) -> CustomerCreatedOutput:
        result = DirectOperationExecutor(session, tool_registry).execute_direct(
            definition=CREATE_CUSTOMER_OPERATION,
            arguments=data.model_dump(mode="json"),
            principal=principal,
            run_id=run_id,
            idempotency_key=idempotency_key,
        )
        return _extract_result(result, CustomerCreatedOutput)

    @app.get("/api/v1/customers/{customer_id}", response_model=CustomerOutput)
    def get_customer(
        customer_id: UUID,
        principal: Annotated[Principal, Depends(require_access("customer:read"))],
        session: Annotated[Session, Depends(get_session)],
    ) -> CustomerOutput:
        customer = BusinessService(session).get_customer(principal, customer_id)
        return CustomerOutput.model_validate(customer)

    @app.get("/api/v1/customers/{customer_id}/tickets", response_model=TicketListOutput)
    def list_tickets(
        customer_id: UUID,
        principal: Annotated[Principal, Depends(require_access("ticket:read"))],
        session: Annotated[Session, Depends(get_session)],
    ) -> TicketListOutput:
        tickets = BusinessService(session).list_customer_tickets(
            principal, customer_id, limit=100
        )
        return TicketListOutput(tickets=[TicketOutput.model_validate(ticket) for ticket in tickets])

    @app.post("/api/v1/tickets", response_model=TicketOutput, status_code=201)
    def create_ticket(
        data: CreateTicketInput,
        principal: Annotated[Principal, Depends(get_principal)],
        session: Annotated[Session, Depends(get_session)],
        run_id: Annotated[UUID, Header(alias="X-Workflow-Run-ID")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8)],
    ) -> TicketOutput:
        result = ToolExecutor(session, tool_registry).execute(
            tool_name="create_ticket",
            arguments=data.model_dump(mode="json"),
            principal=principal,
            run_id=run_id,
            idempotency_key=idempotency_key,
        )
        return _extract_result(result, TicketOutput)

    @app.patch("/api/v1/tickets/{ticket_id}", response_model=TicketOutput)
    def update_ticket(
        ticket_id: UUID,
        data: UpdateTicketInput,
        principal: Annotated[Principal, Depends(get_principal)],
        session: Annotated[Session, Depends(get_session)],
        run_id: Annotated[UUID, Header(alias="X-Workflow-Run-ID")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8)],
    ) -> TicketOutput:
        if data.ticket_id != ticket_id:
            raise HTTPException(status_code=422, detail="path and body ticket IDs differ")
        result = ToolExecutor(session, tool_registry).execute(
            tool_name="update_ticket",
            arguments=data.model_dump(mode="json", exclude_none=True),
            principal=principal,
            run_id=run_id,
            idempotency_key=idempotency_key,
        )
        return _extract_result(result, TicketOutput)

    @app.post("/api/v1/refunds/quote", response_model=RefundQuoteOutput)
    def calculate_refund(
        data: CalculateRefundInput,
        principal: Annotated[Principal, Depends(get_principal)],
        session: Annotated[Session, Depends(get_session)],
        run_id: Annotated[UUID, Header(alias="X-Workflow-Run-ID")],
    ) -> RefundQuoteOutput:
        result = ToolExecutor(session, tool_registry).execute(
            tool_name="calculate_refund",
            arguments=data.model_dump(mode="json"),
            principal=principal,
            run_id=run_id,
            idempotency_key=None,
        )
        return _extract_result(result, RefundQuoteOutput)

    @app.post("/api/v1/refunds", response_model=ToolExecutionResponse, status_code=202)
    def issue_refund_direct(
        data: IssueRefundInput,
        principal: Annotated[
            Principal,
            Depends(require_access("refund:issue", roles=frozenset({Role.ADMIN}))),
        ],
        session: Annotated[Session, Depends(get_session)],
        run_id: Annotated[UUID, Header(alias="X-Workflow-Run-ID")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8)],
    ) -> ToolExecutionResponse:
        return ToolExecutor(session, tool_registry).execute(
            tool_name="issue_refund",
            arguments=data.model_dump(mode="json"),
            principal=principal,
            run_id=run_id,
            idempotency_key=idempotency_key,
            approval_origin=ApprovalOrigin.DIRECT_API,
        )

    @app.get("/api/v1/tools/schemas", response_model=ToolSchemaListOutput)
    def export_tool_schemas(
        _principal: Annotated[Principal, Depends(require_access("tool:schema:read"))],
    ) -> ToolSchemaListOutput:
        return ToolSchemaListOutput(tools=tool_registry.export_all_schemas())

    @app.post("/api/v1/tools/{tool_name}/execute", response_model=ToolExecutionResponse)
    def execute_tool(
        tool_name: str,
        request: ToolExecutionRequest,
        response: Response,
        principal: Annotated[Principal, Depends(get_principal)],
        session: Annotated[Session, Depends(get_session)],
    ) -> ToolExecutionResponse:
        try:
            result = ToolExecutor(session, tool_registry).execute(
                tool_name=tool_name,
                arguments=request.arguments,
                principal=principal,
                run_id=request.run_id,
                idempotency_key=request.idempotency_key,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unregistered tool") from exc
        if result.status is ToolExecutionStatus.DENIED:
            response.status_code = status.HTTP_403_FORBIDDEN
        elif result.status is ToolExecutionStatus.APPROVAL_REQUIRED:
            response.status_code = status.HTTP_202_ACCEPTED
        elif result.status is ToolExecutionStatus.FAILED:
            response.status_code = status.HTTP_409_CONFLICT
        return result

    return app


def _extract_result[T: BaseModel](
    result: ToolExecutionResponse,
    output_model: type[T],
) -> T:
    if result.status is ToolExecutionStatus.DENIED:
        raise HTTPException(status_code=403, detail=result.error)
    if result.status is ToolExecutionStatus.APPROVAL_REQUIRED:
        raise HTTPException(status_code=409, detail="approval required")
    if result.status is ToolExecutionStatus.FAILED or result.result is None:
        code = result.error or "operation failed"
        http_status = 500 if code == "INTERNAL_ERROR" else 409
        raise HTTPException(status_code=http_status, detail=code)
    return output_model.model_validate(result.result)
