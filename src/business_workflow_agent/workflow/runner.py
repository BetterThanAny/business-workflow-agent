import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from business_workflow_agent.auth import Principal
from business_workflow_agent.domain import (
    ApprovalStatus,
    OutboxStatus,
    ToolCallStatus,
    ToolExecutionStatus,
    WorkflowState,
)
from business_workflow_agent.execution import ToolExecutor, redact_payload
from business_workflow_agent.models import (
    Approval,
    AuditEvent,
    SideEffectEvent,
    SideEffectOutbox,
    ToolCall,
    WorkflowEvent,
    WorkflowRun,
)
from business_workflow_agent.observability import WorkflowTelemetry, safe_span_attributes
from business_workflow_agent.policy import AuthorizationDecision, authorize_tool
from business_workflow_agent.schemas import (
    AgentEventOutput,
    AgentManualResumeInput,
    AgentRunCreateInput,
    AgentRunOutput,
    AgentRunResumeInput,
    WorkflowBudget,
)
from business_workflow_agent.services import ResourceNotFound, RunOwnershipError
from business_workflow_agent.tools.registry import ToolDefinition, ToolRegistry
from business_workflow_agent.workflow.persistence import build_checkpoint
from business_workflow_agent.workflow.provider import (
    INTENT_TOOL_NAMES,
    AgentIntent,
    IntentProposal,
    ModelUsage,
    ProviderError,
    ProviderMalformedOutputError,
    ProviderRequest,
    RepairRequest,
    StructuredProvider,
    SummaryRequest,
)
from business_workflow_agent.workflow.retry import RetryPolicy
from business_workflow_agent.workflow.state_machine import PAUSE_STATES, validate_transition


class WorkflowResumeError(ValueError):
    pass


class AgentRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: ToolRegistry,
        provider: StructuredProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        retry_policy: RetryPolicy | None = None,
        max_provider_retries: int = 3,
        telemetry: WorkflowTelemetry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(UTC))
        self.retry_policy = retry_policy or RetryPolicy()
        if max_provider_retries < 0:
            raise ValueError("maximum provider retries must not be negative")
        self.max_provider_retries = max_provider_retries
        self.telemetry = telemetry or WorkflowTelemetry()

    def create(self, principal: Principal, data: AgentRunCreateInput) -> AgentRunOutput:
        with self.session_factory.begin() as session:
            run = WorkflowRun(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                state=WorkflowState.RECEIVED.value,
                version=1,
                budget=data.budget.model_dump(mode="json"),
                message=data.message,
                context_data=data.context,
                pending_fields=[],
                validation_errors=[],
                step_count=0,
                tool_call_count=0,
                tokens_used=0,
                cost_cents_used=0,
                schema_repair_attempts=0,
                event_sequence=0,
                retry_count=0,
            )
            session.add(run)
            session.flush()
            session.add(build_checkpoint(run))
            session.add(
                AuditEvent(
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    run_id=run.id,
                    tool_name="agent_run.create",
                    state=run.state,
                    event_type="AGENT_RUN_CREATED",
                    payload_redacted={"state": run.state, "version": run.version},
                )
            )
            session.flush()
            output = self._output(run)
        self.telemetry.record_run_created(principal.roles)
        return output

    def get(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        with self.session_factory() as session:
            run = self._load_owned(session, principal, run_id)
            return self._output(run)

    def events(
        self,
        principal: Principal,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> list[AgentEventOutput]:
        with self.session_factory() as session:
            self._load_owned(session, principal, run_id)
            events = list(
                session.scalars(
                    select(WorkflowEvent)
                    .where(
                        WorkflowEvent.run_id == run_id,
                        WorkflowEvent.sequence > after_sequence,
                    )
                    .order_by(WorkflowEvent.sequence)
                )
            )
            return [
                AgentEventOutput(
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload=event.payload_redacted,
                )
                for event in events
            ]

    def resume(
        self,
        principal: Principal,
        run_id: UUID,
        data: AgentRunResumeInput,
    ) -> AgentRunOutput:
        current = self.get(principal, run_id)
        if current.state is WorkflowState.RETRYABLE_FAILURE:
            return self._resume_retry(principal, run_id)
        if current.state is not WorkflowState.CLARIFY:
            raise WorkflowResumeError(
                "only clarification or a due retryable workflow can resume"
            )
        with self.session_factory() as session:
            run = self._load_owned(session, principal, run_id)
            merged_context = dict(run.context_data)
            merged_context.update(data.context)
            message = data.message or run.message
        self._transition(
            principal,
            run_id,
            expected=WorkflowState.CLARIFY,
            target=WorkflowState.CLASSIFY,
            updates={
                "context_data": merged_context,
                "message": message,
                "proposal": None,
                "pending_fields": [],
                "validation_errors": [],
                "schema_repair_attempts": 0,
                "error_code": None,
            },
        )
        return self.run_to_pause(principal, run_id)

    def manual_resume(
        self,
        principal: Principal,
        run_id: UUID,
        data: AgentManualResumeInput,
    ) -> AgentRunOutput:
        current = self.get(principal, run_id)
        if current.state is WorkflowState.NON_RETRYABLE_FAILURE:
            self._transition(
                principal,
                run_id,
                expected=WorkflowState.NON_RETRYABLE_FAILURE,
                target=WorkflowState.MANUAL_REVIEW,
                count_step=False,
            )
            current = self.get(principal, run_id)
        if current.state is not WorkflowState.MANUAL_REVIEW:
            raise WorkflowResumeError("only a workflow in manual review can be resumed")
        run = self._run_model(principal, run_id)
        merged_context = dict(run.context_data)
        merged_context.update(data.context)
        updates: dict[str, object] = {
            "context_data": merged_context,
            "message": data.message or run.message,
            "proposal": None,
            "result_payload": None,
            "summary": None,
            "pending_fields": [],
            "validation_errors": [],
            "schema_repair_attempts": 0,
            "error_code": None,
            "retry_count": 0,
            "retry_from_state": None,
            "next_retry_at": None,
        }
        if data.budget is not None:
            updates["budget"] = data.budget.model_dump(mode="json")
        self._transition(
            principal,
            run_id,
            expected=WorkflowState.MANUAL_REVIEW,
            target=WorkflowState.CLASSIFY,
            updates=updates,
            events=[
                (
                    "manual_review_resumed",
                    {"reason": self._redact_text(data.reason)},
                )
            ],
            audit=(
                "agent_run.manual_resume",
                "MANUAL_REVIEW_RESUMED",
                {"reason": self._redact_text(data.reason)},
            ),
        )
        return self.run_to_pause(principal, run_id)

    def cancel(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        current = self.get(principal, run_id)
        if current.state is WorkflowState.CANCELLED:
            return current
        if current.state is WorkflowState.COMPLETE:
            raise WorkflowResumeError("a completed workflow cannot be cancelled")
        validate_transition(current.state, WorkflowState.CANCELLED)
        now = self.clock()
        with self.session_factory.begin() as session:
            self._load_owned(session, principal, run_id)
            pending_outboxes = list(
                session.scalars(
                    select(SideEffectOutbox)
                    .where(
                        SideEffectOutbox.run_id == run_id,
                        SideEffectOutbox.status.in_(
                            [OutboxStatus.PENDING.value, OutboxStatus.IN_PROGRESS.value]
                        ),
                    )
                    .with_for_update()
                )
            )
            run = self._load_owned(session, principal, run_id, for_update=True)
            if WorkflowState(run.state) is not current.state:
                raise WorkflowResumeError("workflow state changed while cancelling")
            run.cancel_requested_at = now
            run.state = WorkflowState.CANCELLED.value
            run.error_code = "WORKFLOW_CANCELLED"
            run.version += 1
            run.step_count += 1
            run.event_sequence += 1
            session.add(
                WorkflowEvent(
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    run_id=run.id,
                    sequence=run.event_sequence,
                    event_type="workflow_cancelled",
                    payload_redacted={"state": run.state},
                )
            )
            for approval in session.scalars(
                select(Approval).where(
                    Approval.run_id == run.id,
                    Approval.status == ApprovalStatus.PENDING.value,
                )
            ):
                approval.status = ApprovalStatus.EXPIRED.value
                approval.decision_token_hash = None
                approval.decided_at = now
            for call in session.scalars(
                select(ToolCall).where(
                    ToolCall.run_id == run.id,
                    ToolCall.status.in_(
                        [
                            ToolCallStatus.IN_PROGRESS.value,
                            ToolCallStatus.AWAITING_APPROVAL.value,
                        ]
                    ),
                )
            ):
                call.status = ToolCallStatus.DENIED.value
                call.error_code = "WORKFLOW_CANCELLED"
                call.completed_at = now
            for outbox in pending_outboxes:
                outbox.status = OutboxStatus.CANCELLED.value
                outbox.error_code = "WORKFLOW_CANCELLED"
                outbox.lease_expires_at = None
                outbox.event_sequence += 1
                session.add(
                    SideEffectEvent(
                        tenant_id=outbox.tenant_id,
                        user_id=outbox.user_id,
                        run_id=outbox.run_id,
                        tool_call_id=outbox.tool_call_id,
                        outbox_id=outbox.id,
                        sequence=outbox.event_sequence,
                        event_type="OUTBOX_CANCELLED",
                        payload_redacted={
                            "attempt": outbox.attempts,
                            "error_code": outbox.error_code,
                        },
                    )
                )
            session.add(build_checkpoint(run))
            session.add(
                AuditEvent(
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    run_id=run.id,
                    tool_name="agent_run.cancel",
                    state=run.state,
                    event_type="WORKFLOW_CANCELLED",
                    payload_redacted={"state": run.state},
                )
            )
            session.flush()
            return self._output(run)

    def _resume_retry(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        if run.next_retry_at is None or run.retry_from_state is None:
            raise WorkflowResumeError("retry checkpoint is incomplete")
        due_at = self._as_utc(run.next_retry_at)
        if self.clock() < due_at:
            raise WorkflowResumeError("retry backoff has not elapsed")
        self._transition(
            principal,
            run_id,
            expected=WorkflowState.RETRYABLE_FAILURE,
            target=WorkflowState.RETRY,
            updates={"next_retry_at": None},
            events=[("retry_started", {"attempt": run.retry_count})],
        )
        return self.run_to_pause(principal, run_id)

    def _advance_retry(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        if run.retry_from_state is None:
            return self._force_manual(
                principal,
                run_id,
                WorkflowState.RETRY,
                "RETRY_CHECKPOINT_INCOMPLETE",
            )
        origin = WorkflowState(run.retry_from_state)
        self._transition(
            principal,
            run_id,
            expected=WorkflowState.RETRY,
            target=origin,
            updates={"error_code": None},
        )
        return self.get(principal, run_id)

    def run_to_pause(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        started = perf_counter()
        with self.telemetry.span(
            "workflow.run",
            safe_span_attributes(run_id=str(run_id), tenant_id=str(principal.tenant_id)),
        ) as span:
            try:
                for _ in range(256):
                    current = self.get(principal, run_id)
                    if current.state in PAUSE_STATES:
                        span.set_attribute("workflow.final_state", current.state.value)
                        return current
                    with self.telemetry.span(
                        "workflow.step",
                        safe_span_attributes(
                            run_id=str(run_id), workflow_state=current.state.value
                        ),
                    ):
                        self.advance_once(principal, run_id)
                forced = self._force_manual(
                    principal,
                    run_id,
                    self.get(principal, run_id).state,
                    "RUNNER_LOOP_GUARD_EXCEEDED",
                )
                span.set_attribute("workflow.final_state", forced.state.value)
                return forced
            finally:
                self.telemetry.observe_orchestration((perf_counter() - started) * 1000)

    def continue_after_approval(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
    ) -> AgentRunOutput:
        owner = Principal(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=frozenset(),
            scopes=frozenset(),
        )
        return self.run_to_pause(owner, run_id)

    def advance_once(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        current = self.get(principal, run_id)
        if current.state in PAUSE_STATES:
            return current
        budget_error = self._budget_error(principal, run_id)
        if budget_error is not None:
            return self._force_manual(
                principal,
                run_id,
                current.state,
                budget_error,
                count_step=False,
            )

        handlers: dict[WorkflowState, Callable[[Principal, UUID], AgentRunOutput]] = {
            WorkflowState.RECEIVED: self._advance_received,
            WorkflowState.CLASSIFY: self._advance_classify,
            WorkflowState.RETRIEVE: self._advance_proposal,
            WorkflowState.PLAN_ACTION: self._advance_proposal,
            WorkflowState.VALIDATE_POLICY: self._advance_validate,
            WorkflowState.REPAIR_SCHEMA: self._advance_repair,
            WorkflowState.EXECUTE: self._advance_execute,
            WorkflowState.VERIFY_RESULT: self._advance_verify,
            WorkflowState.RETRY: self._advance_retry,
        }
        handler = handlers.get(current.state)
        if handler is None:
            return self._force_manual(
                principal, run_id, current.state, "UNHANDLED_WORKFLOW_STATE"
            )
        return handler(principal, run_id)

    def _advance_received(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        return self._transition(
            principal,
            run_id,
            expected=WorkflowState.RECEIVED,
            target=WorkflowState.CLASSIFY,
        )

    def _advance_classify(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        try:
            with self.telemetry.span(
                "llm.classify", safe_span_attributes(run_id=str(run_id))
            ):
                proposal = self.provider.classify(
                    ProviderRequest(message=run.message, context=run.context_data)
                )
            self.telemetry.record_llm("classify", "success")
        except Exception as exc:
            self.telemetry.record_llm("classify", "error")
            return self._provider_failure(
                principal, run_id, WorkflowState.CLASSIFY, exc
            )
        budget_error = self._usage_budget_error(run, proposal.usage)
        updates: dict[str, object] = {
            "proposal": proposal.model_dump(mode="json"),
            "pending_fields": proposal.missing_fields,
            "tokens_used": run.tokens_used + proposal.usage.tokens,
            "cost_cents_used": run.cost_cents_used + proposal.usage.cost_cents,
            "error_code": budget_error,
            "retry_count": 0,
            "retry_from_state": None,
            "next_retry_at": None,
        }
        event_payload = {"summary": self._redact_text(proposal.thought_summary)}
        if budget_error is not None:
            return self._transition(
                principal,
                run_id,
                expected=WorkflowState.CLASSIFY,
                target=WorkflowState.MANUAL_REVIEW,
                updates=updates,
                events=[("thought_summary", event_payload)],
            )
        if proposal.missing_fields:
            target = WorkflowState.CLARIFY
        elif proposal.intent is AgentIntent.SEARCH_KNOWLEDGE:
            target = WorkflowState.RETRIEVE
        else:
            target = WorkflowState.PLAN_ACTION
        return self._transition(
            principal,
            run_id,
            expected=WorkflowState.CLASSIFY,
            target=target,
            updates=updates,
            events=[("thought_summary", event_payload)],
        )

    def _advance_proposal(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        proposal = self._proposal(run)
        definition, error_code = self._definition_for(proposal)
        if definition is None:
            return self._force_manual(principal, run_id, WorkflowState(run.state), error_code)
        arguments = cast(
            dict[str, Any], redact_payload(proposal.arguments, definition.pii_fields)
        )
        return self._transition(
            principal,
            run_id,
            expected=WorkflowState(run.state),
            target=WorkflowState.VALIDATE_POLICY,
            events=[
                (
                    "tool_proposed",
                    {"tool_name": definition.name, "arguments": arguments},
                )
            ],
        )

    def _advance_validate(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        proposal = self._proposal(run)
        definition, error_code = self._definition_for(proposal)
        if definition is None:
            return self._force_manual(
                principal, run_id, WorkflowState.VALIDATE_POLICY, error_code
            )
        try:
            definition.input_model.model_validate(proposal.arguments)
        except ValidationError as exc:
            errors = exc.errors(include_context=False, include_url=False)
            pending_fields = sorted(
                {
                    str(error["loc"][0])
                    for error in errors
                    if error.get("loc") and error["loc"]
                }
            )
            if run.schema_repair_attempts >= 1:
                return self._transition(
                    principal,
                    run_id,
                    expected=WorkflowState.VALIDATE_POLICY,
                    target=WorkflowState.CLARIFY,
                    updates={
                        "validation_errors": errors,
                        "pending_fields": pending_fields,
                        "error_code": "SCHEMA_VALIDATION_FAILED",
                    },
                )
            return self._transition(
                principal,
                run_id,
                expected=WorkflowState.VALIDATE_POLICY,
                target=WorkflowState.REPAIR_SCHEMA,
                updates={"validation_errors": errors, "pending_fields": pending_fields},
            )
        decision = authorize_tool(principal, definition)
        if decision is not AuthorizationDecision.ALLOW:
            return self._force_manual(
                principal,
                run_id,
                WorkflowState.VALIDATE_POLICY,
                f"POLICY_{decision.value}",
            )
        return self._transition(
            principal,
            run_id,
            expected=WorkflowState.VALIDATE_POLICY,
            target=WorkflowState.EXECUTE,
            updates={"validation_errors": [], "pending_fields": [], "error_code": None},
        )

    def _advance_repair(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        proposal = self._proposal(run)
        definition, error_code = self._definition_for(proposal)
        if definition is None:
            return self._force_manual(
                principal, run_id, WorkflowState.REPAIR_SCHEMA, error_code
            )
        try:
            with self.telemetry.span(
                "llm.repair", safe_span_attributes(run_id=str(run_id))
            ):
                repaired = self.provider.repair(
                    RepairRequest(
                        message=run.message,
                        context=run.context_data,
                        proposal=proposal,
                        validation_errors=run.validation_errors,
                        input_schema=definition.input_model.model_json_schema(),
                    )
                )
            self.telemetry.record_llm("repair", "success")
        except Exception as exc:
            self.telemetry.record_llm("repair", "error")
            return self._provider_failure(
                principal, run_id, WorkflowState.REPAIR_SCHEMA, exc
            )
        budget_error = self._usage_budget_error(run, repaired.usage)
        target = (
            WorkflowState.MANUAL_REVIEW
            if budget_error is not None
            else WorkflowState.VALIDATE_POLICY
        )
        return self._transition(
            principal,
            run_id,
            expected=WorkflowState.REPAIR_SCHEMA,
            target=target,
            updates={
                "proposal": repaired.model_dump(mode="json"),
                "schema_repair_attempts": run.schema_repair_attempts + 1,
                "tokens_used": run.tokens_used + repaired.usage.tokens,
                "cost_cents_used": run.cost_cents_used + repaired.usage.cost_cents,
                "error_code": budget_error,
                "retry_count": 0,
                "retry_from_state": None,
                "next_retry_at": None,
            },
            events=[
                (
                    "thought_summary",
                    {"summary": self._redact_text(repaired.thought_summary)},
                )
            ],
        )

    def _advance_execute(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        budget = WorkflowBudget.model_validate(run.budget)
        if run.tool_call_count >= budget.max_tool_calls:
            return self._force_manual(
                principal,
                run_id,
                WorkflowState.EXECUTE,
                "MAX_TOOL_CALLS_EXCEEDED",
            )
        proposal = self._proposal(run)
        definition, error_code = self._definition_for(proposal)
        if definition is None:
            return self._force_manual(
                principal, run_id, WorkflowState.EXECUTE, error_code
            )
        idempotency_key = f"agent:{run.id}:tool:{run.tool_call_count + 1}"
        with self.telemetry.span(
            "tool.execute",
            safe_span_attributes(run_id=str(run_id), tool_name=definition.name),
        ), self.session_factory() as session:
            result = ToolExecutor(session, self.registry).execute(
                tool_name=definition.name,
                arguments=proposal.arguments,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
            )
        self.telemetry.record_tool(definition.name, result.status.value)
        updates: dict[str, object] = {
            "tool_call_count": run.tool_call_count + 1,
            "result_payload": result.result,
            "error_code": result.error,
        }
        if result.status is ToolExecutionStatus.APPROVAL_REQUIRED:
            approval_payload = {
                "tool_name": definition.name,
                "approval_id": str(result.approval_id),
                "tool_call_id": str(result.tool_call_id),
            }
            updates["result_payload"] = approval_payload
            return self._transition(
                principal,
                run_id,
                expected=WorkflowState.EXECUTE,
                target=WorkflowState.AWAIT_APPROVAL,
                updates=updates,
                events=[("approval_required", approval_payload)],
            )
        if result.status is not ToolExecutionStatus.SUCCEEDED:
            return self._transition(
                principal,
                run_id,
                expected=WorkflowState.EXECUTE,
                target=WorkflowState.MANUAL_REVIEW,
                updates=updates,
            )
        return self._transition(
            principal,
            run_id,
            expected=WorkflowState.EXECUTE,
            target=WorkflowState.VERIFY_RESULT,
            updates=updates,
        )

    def _advance_verify(self, principal: Principal, run_id: UUID) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        proposal = self._proposal(run)
        result = run.result_payload or {}
        try:
            with self.telemetry.span(
                "llm.summarize", safe_span_attributes(run_id=str(run_id))
            ):
                summary = self.provider.summarize(
                    SummaryRequest(
                        message=run.message,
                        tool_name=proposal.tool_name or "unknown",
                        result=result,
                    )
                )
            self.telemetry.record_llm("summarize", "success")
        except Exception as exc:
            self.telemetry.record_llm("summarize", "error")
            return self._provider_failure(
                principal, run_id, WorkflowState.VERIFY_RESULT, exc
            )
        budget_error = self._usage_budget_error(run, summary.usage)
        updates: dict[str, object] = {
            "tokens_used": run.tokens_used + summary.usage.tokens,
            "cost_cents_used": run.cost_cents_used + summary.usage.cost_cents,
            "summary": summary.summary,
            "error_code": budget_error,
            "retry_count": 0,
            "retry_from_state": None,
            "next_retry_at": None,
        }
        if budget_error is not None:
            return self._transition(
                principal,
                run_id,
                expected=WorkflowState.VERIFY_RESULT,
                target=WorkflowState.MANUAL_REVIEW,
                updates=updates,
            )
        return self._transition(
            principal,
            run_id,
            expected=WorkflowState.VERIFY_RESULT,
            target=WorkflowState.COMPLETE,
            updates=updates,
            events=[
                (
                    "complete",
                    {"summary": self._redact_text(summary.summary)},
                )
            ],
        )

    def _transition(
        self,
        principal: Principal,
        run_id: UUID,
        *,
        expected: WorkflowState,
        target: WorkflowState,
        updates: dict[str, object] | None = None,
        events: list[tuple[str, dict[str, Any]]] | None = None,
        audit: tuple[str, str, dict[str, Any]] | None = None,
        count_step: bool = True,
    ) -> AgentRunOutput:
        validate_transition(expected, target)
        with self.session_factory.begin() as session:
            run = self._load_owned(session, principal, run_id, for_update=True)
            if WorkflowState(run.state) is not expected:
                raise ValueError(
                    f"workflow state changed concurrently: expected {expected}, got {run.state}"
                )
            for field_name, value in (updates or {}).items():
                setattr(run, field_name, value)
            run.state = target.value
            run.version += 1
            if count_step:
                run.step_count += 1
            for event_type, payload in events or []:
                run.event_sequence += 1
                session.add(
                    WorkflowEvent(
                        tenant_id=run.tenant_id,
                        user_id=run.user_id,
                        run_id=run.id,
                        sequence=run.event_sequence,
                        event_type=event_type,
                        payload_redacted=payload,
                    )
                )
            session.add(build_checkpoint(run))
            if audit is not None:
                tool_name, event_type, payload = audit
                session.add(
                    AuditEvent(
                        tenant_id=run.tenant_id,
                        user_id=run.user_id,
                        run_id=run.id,
                        tool_name=tool_name,
                        state=run.state,
                        event_type=event_type,
                        payload_redacted=payload,
                    )
                )
            session.flush()
            output = self._output(run)
        self.telemetry.record_transition(expected.value, target.value)
        return output

    def _provider_failure(
        self,
        principal: Principal,
        run_id: UUID,
        expected: WorkflowState,
        exc: Exception,
    ) -> AgentRunOutput:
        run = self._run_model(principal, run_id)
        if isinstance(exc, ProviderError):
            retryable = exc.retryable
            error_code = exc.code
        elif isinstance(exc, TimeoutError):
            retryable = True
            error_code = "PROVIDER_TIMEOUT"
        elif isinstance(exc, ValidationError):
            retryable = False
            error_code = ProviderMalformedOutputError.code
        else:
            retryable = False
            error_code = "PROVIDER_UNEXPECTED_ERROR"

        attempt = run.retry_count + 1
        if retryable and attempt <= self.max_provider_retries:
            delay = self.retry_policy.delay_seconds(attempt)
            next_retry_at = self.clock() + timedelta(seconds=delay)
            return self._transition(
                principal,
                run_id,
                expected=expected,
                target=WorkflowState.RETRYABLE_FAILURE,
                updates={
                    "retry_count": attempt,
                    "retry_from_state": expected.value,
                    "next_retry_at": next_retry_at,
                    "error_code": error_code,
                },
                events=[
                    (
                        "retry_scheduled",
                        {
                            "attempt": attempt,
                            "error_code": error_code,
                            "next_retry_at": next_retry_at.isoformat(),
                        },
                    )
                ],
            )
        terminal_error = "PROVIDER_RETRIES_EXHAUSTED" if retryable else error_code
        return self._transition(
            principal,
            run_id,
            expected=expected,
            target=WorkflowState.NON_RETRYABLE_FAILURE,
            updates={
                "retry_count": run.retry_count,
                "retry_from_state": expected.value,
                "next_retry_at": None,
                "error_code": terminal_error,
            },
        )

    def _force_manual(
        self,
        principal: Principal,
        run_id: UUID,
        expected: WorkflowState,
        error_code: str,
        *,
        count_step: bool = True,
    ) -> AgentRunOutput:
        return self._transition(
            principal,
            run_id,
            expected=expected,
            target=WorkflowState.MANUAL_REVIEW,
            updates={"error_code": error_code},
            count_step=count_step,
        )

    def _budget_error(self, principal: Principal, run_id: UUID) -> str | None:
        run = self._run_model(principal, run_id)
        budget = WorkflowBudget.model_validate(run.budget)
        if run.step_count >= budget.max_steps:
            return "MAX_STEPS_EXCEEDED"
        created_at = run.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if (self.clock() - created_at).total_seconds() >= budget.max_elapsed_seconds:
            return "MAX_ELAPSED_TIME_EXCEEDED"
        if run.tokens_used > budget.max_tokens:
            return "TOKEN_BUDGET_EXCEEDED"
        if run.cost_cents_used > budget.max_cost_cents:
            return "COST_BUDGET_EXCEEDED"
        return None

    def _usage_budget_error(self, run: WorkflowRun, usage: ModelUsage) -> str | None:
        budget = WorkflowBudget.model_validate(run.budget)
        if run.tokens_used + usage.tokens > budget.max_tokens:
            return "TOKEN_BUDGET_EXCEEDED"
        if run.cost_cents_used + usage.cost_cents > budget.max_cost_cents:
            return "COST_BUDGET_EXCEEDED"
        return None

    def _definition_for(
        self,
        proposal: IntentProposal,
    ) -> tuple[ToolDefinition | None, str]:
        if proposal.tool_name is None:
            return None, "UNREGISTERED_TOOL"
        expected_name = INTENT_TOOL_NAMES.get(proposal.intent)
        try:
            definition = self.registry.get(proposal.tool_name)
        except KeyError:
            return None, "UNREGISTERED_TOOL"
        if expected_name != definition.name:
            return None, "TOOL_SELECTION_MISMATCH"
        return definition, ""

    def _proposal(self, run: WorkflowRun) -> IntentProposal:
        if run.proposal is None:
            raise ValueError("workflow has no structured proposal")
        return IntentProposal.model_validate(run.proposal)

    def _run_model(self, principal: Principal, run_id: UUID) -> WorkflowRun:
        with self.session_factory() as session:
            run = self._load_owned(session, principal, run_id)
            session.expunge(run)
            return run

    def _load_owned(
        self,
        session: Session,
        principal: Principal,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkflowRun:
        statement = select(WorkflowRun).where(
            WorkflowRun.id == run_id,
            WorkflowRun.tenant_id == principal.tenant_id,
        )
        if for_update:
            statement = statement.with_for_update()
        run = session.scalar(statement)
        if run is None:
            raise ResourceNotFound("workflow run not found")
        if run.user_id != principal.user_id:
            raise RunOwnershipError("workflow run belongs to a different user")
        return run

    def _output(self, run: WorkflowRun) -> AgentRunOutput:
        return AgentRunOutput(
            id=run.id,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            state=WorkflowState(run.state),
            version=run.version,
            budget=run.budget,
            step_count=run.step_count,
            tool_call_count=run.tool_call_count,
            tokens_used=run.tokens_used,
            cost_cents_used=run.cost_cents_used,
            schema_repair_attempts=run.schema_repair_attempts,
            pending_fields=run.pending_fields,
            result=run.result_payload,
            summary=run.summary,
            error_code=run.error_code,
            retry_count=run.retry_count,
            retry_from_state=(
                WorkflowState(run.retry_from_state) if run.retry_from_state else None
            ),
            next_retry_at=(self._as_utc(run.next_retry_at) if run.next_retry_at else None),
            cancel_requested_at=(
                self._as_utc(run.cancel_requested_at)
                if run.cancel_requested_at
                else None
            ),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _redact_text(value: str) -> str:
        value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED]", value)
        return re.sub(r"\b\d{11}\b", "[REDACTED]", value)
