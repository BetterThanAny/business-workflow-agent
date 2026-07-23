import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role, granted_scopes
from business_workflow_agent.domain import (
    ApprovalDecision,
    ApprovalOrigin,
    ApprovalStatus,
    OutboxStatus,
    ToolCallStatus,
    WorkflowState,
)
from business_workflow_agent.execution import redact_payload
from business_workflow_agent.models import (
    Approval,
    AuditEvent,
    SideEffectEvent,
    SideEffectOutbox,
    ToolCall,
    WorkflowEvent,
    WorkflowRun,
)
from business_workflow_agent.policy import AuthorizationDecision, authorize_tool
from business_workflow_agent.schemas import (
    ApprovalDecisionInput,
    ApprovalDecisionOutput,
    ApprovalDetailOutput,
    ApprovalTokenOutput,
)
from business_workflow_agent.services import BusinessService, ResourceNotFound
from business_workflow_agent.tools.registry import RiskClass, ToolDefinition, ToolRegistry
from business_workflow_agent.workflow.persistence import build_checkpoint
from business_workflow_agent.workflow.state_machine import validate_transition


class ApprovalPermissionError(PermissionError):
    pass


class ApprovalConflict(ValueError):
    pass


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ApprovalService:
    def __init__(
        self,
        session: Session,
        registry: ToolRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.clock = clock or (lambda: datetime.now(UTC))

    def get(self, principal: Principal, approval_id: UUID) -> ApprovalDetailOutput:
        self._require_scope(
            principal,
            "approval:read",
            frozenset({Role.REFUND_MANAGER, Role.AUDITOR, Role.ADMIN}),
        )
        approval = self._load(principal, approval_id)
        return ApprovalDetailOutput(
            id=approval.id,
            tenant_id=approval.tenant_id,
            run_id=approval.run_id,
            tool_name=approval.tool_name,
            status=ApprovalStatus(approval.status),
            origin=ApprovalOrigin(approval.origin),
            requested_by_user_id=approval.requested_by_user_id,
            tool_arguments_redacted=approval.tool_arguments_redacted,
            expires_at=approval.expires_at,
            decided_by_user_id=approval.decided_by_user_id,
        )

    def issue_decision_token(
        self,
        principal: Principal,
        approval_id: UUID,
    ) -> ApprovalTokenOutput | None:
        approval = self._load(principal, approval_id, for_update=True)
        now = self.clock()
        self._require_scope(
            principal,
            "approval:decide",
            frozenset({Role.REFUND_MANAGER, Role.ADMIN}),
        )
        self._require_pending(approval)
        self._require_independent_approver(principal, approval)
        definition = self._high_risk_definition(approval)
        call = self._tool_call(approval)
        self._require_tool_authorization(principal, definition)
        if not approval.tool_arguments_available:
            self._terminal_denial(
                approval,
                call,
                self._run(approval),
                status=ApprovalStatus.EXPIRED,
                error_code="LEGACY_APPROVAL_REPROPOSAL_REQUIRED",
                event_type="APPROVAL_MIGRATION_EXPIRED",
                decided_by_user_id=None,
                audit_user_id=principal.user_id,
            )
            return None
        if _as_utc(approval.expires_at) <= now:
            self._terminal_denial(
                approval,
                call,
                self._run(approval),
                status=ApprovalStatus.EXPIRED,
                error_code="APPROVAL_EXPIRED",
                event_type="APPROVAL_EXPIRED",
                decided_by_user_id=None,
                audit_user_id=principal.user_id,
            )
            return None

        raw_token = secrets.token_urlsafe(32)
        token_expires_at = min(_as_utc(approval.expires_at), now + timedelta(minutes=10))
        approval.decision_token_hash = _token_hash(raw_token)
        approval.decision_token_issued_to_user_id = principal.user_id
        approval.decision_token_expires_at = token_expires_at
        self._add_audit(
            approval,
            principal,
            "APPROVAL_TOKEN_ISSUED",
            {
                "approval_id": str(approval.id),
                "issued_to_user_id": str(principal.user_id),
                "expires_at": token_expires_at.isoformat(),
            },
        )
        self.session.flush()
        return ApprovalTokenOutput(
            approval_id=approval.id,
            decision_token=raw_token,
            expires_at=token_expires_at,
        )

    def decide(
        self,
        principal: Principal,
        approval_id: UUID,
        data: ApprovalDecisionInput,
    ) -> ApprovalDecisionOutput:
        approval = self._load(principal, approval_id, for_update=True)
        now = self.clock()
        self._require_scope(
            principal,
            "approval:decide",
            frozenset({Role.REFUND_MANAGER, Role.ADMIN}),
        )
        self._require_pending(approval)
        self._require_independent_approver(principal, approval)
        call = self._tool_call(approval)
        run = self._run(approval)

        if _as_utc(approval.expires_at) <= now:
            return self._terminal_denial(
                approval,
                call,
                run,
                status=ApprovalStatus.EXPIRED,
                error_code="APPROVAL_EXPIRED",
                event_type="APPROVAL_EXPIRED",
                decided_by_user_id=None,
                audit_user_id=principal.user_id,
            )

        self._validate_token(principal, approval, data.decision_token, now)
        definition = self._high_risk_definition(approval)
        self._require_tool_authorization(principal, definition)
        approval.decision_token_hash = None
        approval.decision_token_used_at = now
        approval.decided_by_user_id = principal.user_id
        approval.decided_at = now

        if data.decision is ApprovalDecision.REJECT:
            return self._terminal_denial(
                approval,
                call,
                run,
                status=ApprovalStatus.REJECTED,
                error_code="APPROVAL_REJECTED",
                event_type="APPROVAL_REJECTED",
                decided_by_user_id=principal.user_id,
                audit_user_id=principal.user_id,
            )

        outbox = SideEffectOutbox(
            tenant_id=approval.tenant_id,
            user_id=run.user_id,
            run_id=run.id,
            tool_call_id=call.id,
            tool_name=definition.name,
            idempotency_key=call.idempotency_key or f"approval:{approval.id}",
            payload=approval.tool_arguments,
            status=OutboxStatus.PENDING.value,
            attempts=1,
            available_at=now,
            lease_expires_at=now + timedelta(seconds=30),
        )
        self.session.add(outbox)
        self.session.flush()
        self._add_outbox_event(outbox, "OUTBOX_ENQUEUED", {"status": "PENDING"})
        outbox.status = OutboxStatus.IN_PROGRESS.value
        self._add_outbox_event(outbox, "OUTBOX_CLAIMED", {"attempt": 1})
        validated_input = definition.input_model.model_validate(approval.tool_arguments)
        raw_output = definition.handler(
            BusinessService(self.session), validated_input, principal, run.id
        )
        output = definition.output_model.model_validate(raw_output)
        result = output.model_dump(mode="json")
        result_redacted = redact_payload(result, definition.pii_fields)
        approval.status = ApprovalStatus.USED.value
        call.status = ToolCallStatus.SUCCEEDED.value
        call.result_redacted = result_redacted
        call.error_code = None
        call.completed_at = now
        outbox.status = OutboxStatus.SUCCEEDED.value
        outbox.result_redacted = result_redacted
        outbox.lease_expires_at = None
        self._add_outbox_event(
            outbox,
            "OUTBOX_SUCCEEDED",
            {"attempt": 1, "result": result_redacted},
        )
        self._transition_agent_run(
            run,
            target=WorkflowState.VERIFY_RESULT,
            result=result,
            error_code=None,
            approval=approval,
        )
        self._add_audit(
            approval,
            principal,
            "APPROVAL_APPROVED",
            {
                "approval_id": str(approval.id),
                "status": approval.status,
                "result": result_redacted,
            },
            call=call,
        )
        self.session.flush()
        return self._output(approval, call, run, result)

    def _terminal_denial(
        self,
        approval: Approval,
        call: ToolCall,
        run: WorkflowRun,
        *,
        status: ApprovalStatus,
        error_code: str,
        event_type: str,
        decided_by_user_id: UUID | None,
        audit_user_id: UUID,
    ) -> ApprovalDecisionOutput:
        now = self.clock()
        approval.status = status.value
        approval.decision_token_hash = None
        approval.decided_by_user_id = decided_by_user_id
        approval.decided_at = now
        call.status = ToolCallStatus.DENIED.value
        call.error_code = error_code
        call.completed_at = now
        self._transition_agent_run(
            run,
            target=WorkflowState.NON_RETRYABLE_FAILURE,
            result=None,
            error_code=error_code,
            approval=approval,
        )
        principal = Principal(
            user_id=audit_user_id,
            tenant_id=approval.tenant_id,
            roles=frozenset(),
            scopes=frozenset(),
        )
        self._add_audit(
            approval,
            principal,
            event_type,
            {
                "approval_id": str(approval.id),
                "status": approval.status,
                "error_code": error_code,
            },
            call=call,
        )
        self.session.flush()
        return self._output(approval, call, run, None)

    def _transition_agent_run(
        self,
        run: WorkflowRun,
        *,
        target: WorkflowState,
        result: dict[str, object] | None,
        error_code: str | None,
        approval: Approval,
    ) -> None:
        if WorkflowState(run.state) is not WorkflowState.AWAIT_APPROVAL:
            return
        validate_transition(WorkflowState.AWAIT_APPROVAL, target)
        run.state = target.value
        run.version += 1
        run.step_count += 1
        run.result_payload = result
        run.error_code = error_code
        run.event_sequence += 1
        self.session.add(
            WorkflowEvent(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.id,
                sequence=run.event_sequence,
                event_type="approval_decided",
                payload_redacted={
                    "approval_id": str(approval.id),
                    "status": approval.status,
                },
            )
        )
        self.session.add(build_checkpoint(run))

    def _validate_token(
        self,
        principal: Principal,
        approval: Approval,
        raw_token: str,
        now: datetime,
    ) -> None:
        if approval.decision_token_issued_to_user_id != principal.user_id:
            raise ApprovalPermissionError("decision token belongs to another approver")
        if approval.decision_token_hash is None:
            raise ApprovalConflict("approval has no active decision token")
        expires_at = approval.decision_token_expires_at
        if expires_at is None or _as_utc(expires_at) <= now:
            raise ApprovalConflict("decision token expired")
        if not compare_digest(approval.decision_token_hash, _token_hash(raw_token)):
            raise ApprovalPermissionError("invalid decision token")

    def _load(
        self,
        principal: Principal,
        approval_id: UUID,
        *,
        for_update: bool = False,
    ) -> Approval:
        statement = select(Approval).where(
            Approval.id == approval_id,
            Approval.tenant_id == principal.tenant_id,
        )
        if for_update:
            statement = statement.with_for_update()
        approval = self.session.scalar(statement)
        if approval is None:
            raise ResourceNotFound("approval not found")
        return approval

    def _tool_call(self, approval: Approval) -> ToolCall:
        call = self.session.scalar(
            select(ToolCall).where(
                ToolCall.approval_id == approval.id,
                ToolCall.tenant_id == approval.tenant_id,
            )
        )
        if call is None or call.status != ToolCallStatus.AWAITING_APPROVAL.value:
            raise ApprovalConflict("approval tool call is not awaiting a decision")
        return call

    def _run(self, approval: Approval) -> WorkflowRun:
        run = self.session.get(WorkflowRun, approval.run_id)
        if run is None or run.tenant_id != approval.tenant_id:
            raise ResourceNotFound("approval workflow run not found")
        return run

    def _high_risk_definition(self, approval: Approval) -> ToolDefinition:
        try:
            definition = self.registry.get(approval.tool_name)
        except KeyError as exc:
            raise ApprovalConflict("approval references an unregistered tool") from exc
        if definition.risk is not RiskClass.WRITE_HIGH_RISK:
            raise ApprovalConflict("approval does not reference a high-risk tool")
        return definition

    @staticmethod
    def _require_pending(approval: Approval) -> None:
        if approval.status != ApprovalStatus.PENDING.value:
            raise ApprovalConflict("approval was already decided")

    @staticmethod
    def _require_independent_approver(
        principal: Principal,
        approval: Approval,
    ) -> None:
        if principal.user_id == approval.requested_by_user_id:
            raise ApprovalPermissionError("requester cannot decide their own approval")

    @staticmethod
    def _require_tool_authorization(
        principal: Principal,
        definition: ToolDefinition,
    ) -> None:
        if authorize_tool(principal, definition) is not AuthorizationDecision.ALLOW:
            raise ApprovalPermissionError("approver is not authorized for the high-risk tool")

    @staticmethod
    def _require_scope(
        principal: Principal,
        required_scope: str,
        roles: frozenset[Role],
    ) -> None:
        if not principal.roles.intersection(roles):
            raise ApprovalPermissionError("approval role denied")
        if required_scope not in principal.scopes:
            raise ApprovalPermissionError("approval scope missing")
        if required_scope not in granted_scopes(principal.roles):
            raise ApprovalPermissionError("approval scope is not granted to the role")

    def _add_audit(
        self,
        approval: Approval,
        principal: Principal,
        event_type: str,
        payload: dict[str, object],
        *,
        call: ToolCall | None = None,
    ) -> None:
        run = self._run(approval)
        if call is None:
            call = self.session.scalar(
                select(ToolCall).where(ToolCall.approval_id == approval.id)
            )
        self.session.add(
            AuditEvent(
                tenant_id=approval.tenant_id,
                user_id=principal.user_id,
                run_id=approval.run_id,
                tool_call_id=call.id if call is not None else None,
                tool_name=approval.tool_name,
                state=run.state,
                event_type=event_type,
                payload_redacted=payload,
            )
        )

    def _add_outbox_event(
        self,
        outbox: SideEffectOutbox,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        outbox.event_sequence += 1
        self.session.add(
            SideEffectEvent(
                tenant_id=outbox.tenant_id,
                user_id=outbox.user_id,
                run_id=outbox.run_id,
                tool_call_id=outbox.tool_call_id,
                outbox_id=outbox.id,
                sequence=outbox.event_sequence,
                event_type=event_type,
                payload_redacted=payload,
            )
        )

    @staticmethod
    def _output(
        approval: Approval,
        call: ToolCall,
        run: WorkflowRun,
        result: dict[str, object] | None,
    ) -> ApprovalDecisionOutput:
        return ApprovalDecisionOutput(
            approval_id=approval.id,
            run_id=run.id,
            approval_status=ApprovalStatus(approval.status),
            origin=ApprovalOrigin(approval.origin),
            tool_call_status=ToolCallStatus(call.status),
            run_state=WorkflowState(run.state),
            result=result,
        )
