import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from business_workflow_agent.auth import Principal, Role, granted_scopes
from business_workflow_agent.domain import (
    ApprovalStatus,
    OutboxStatus,
    ToolCallStatus,
    ToolExecutionStatus,
)
from business_workflow_agent.models import (
    Approval,
    AuditEvent,
    SideEffectEvent,
    SideEffectOutbox,
    ToolCall,
    WorkflowRun,
    utc_now,
)
from business_workflow_agent.policy import AuthorizationDecision, authorize_tool
from business_workflow_agent.schemas import ToolExecutionResponse
from business_workflow_agent.services import BusinessError, BusinessService
from business_workflow_agent.tools.registry import RiskClass, ToolDefinition, ToolRegistry


class IdempotencyConflict(BusinessError):
    code = "IDEMPOTENCY_CONFLICT"


@dataclass(frozen=True, slots=True)
class DirectOperationDefinition:
    name: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk: RiskClass
    required_roles: frozenset[Role]
    required_scope: str
    pii_fields: frozenset[str]
    handler: Callable[..., BaseModel]


def _canonical_payload(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _request_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload(data).encode()).hexdigest()


def redact_payload(value: Any, pii_fields: frozenset[str]) -> Any:
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {
            str(key): "[REDACTED]"
            if str(key) in pii_fields
            else redact_payload(item, pii_fields)
            for key, item in mapping.items()
        }
    if isinstance(value, list):
        items = cast(list[object], value)
        return [redact_payload(item, pii_fields) for item in items]
    return value


def _is_authorized_for_direct(
    principal: Principal,
    definition: DirectOperationDefinition,
) -> bool:
    return bool(principal.roles.intersection(definition.required_roles)) and (
        definition.required_scope in principal.scopes
        and definition.required_scope in granted_scopes(principal.roles)
    )


class ToolExecutor:
    def __init__(
        self,
        session: Session,
        registry: ToolRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        after_outbox_claim: Callable[[], None] | None = None,
        outbox_lease_seconds: int = 30,
    ) -> None:
        self.session = session
        self.registry = registry
        self.clock = clock or (lambda: datetime.now(UTC))
        self.after_outbox_claim = after_outbox_claim
        self.outbox_lease_seconds = outbox_lease_seconds

    def execute(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: Principal,
        run_id: UUID,
        idempotency_key: str | None,
    ) -> ToolExecutionResponse:
        definition = self.registry.get(tool_name)
        if definition.risk is RiskClass.WRITE_LOW_RISK:
            return self._execute_outboxed(
                definition=definition,
                arguments=arguments,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
            )
        try:
            return self._execute_transaction(
                tool_name=tool_name,
                arguments=arguments,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
            )
        except IdempotencyConflict:
            raise
        except Exception as exc:
            self.session.rollback()
            definition = self.registry.get(tool_name)
            error_code = exc.code if isinstance(exc, BusinessError) else "INTERNAL_ERROR"
            return self._persist_rolled_back_failure(
                definition=definition,
                arguments=arguments,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
                error_code=error_code,
            )

    def _execute_outboxed(
        self,
        *,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        principal: Principal,
        run_id: UUID,
        idempotency_key: str | None,
    ) -> ToolExecutionResponse:
        try:
            with self.session.begin():
                run = BusinessService(self.session).require_run(principal, run_id)
                if run.cancel_requested_at is not None or run.state == "CANCELLED":
                    return self._record_denial(
                        definition,
                        principal,
                        run_id,
                        arguments,
                        "WORKFLOW_CANCELLED",
                    )
                decision = authorize_tool(principal, definition)
                if decision is not AuthorizationDecision.ALLOW:
                    return self._record_denial(
                        definition,
                        principal,
                        run_id,
                        arguments,
                        f"AUTHORIZATION_{decision.value}",
                    )
                try:
                    validated_input = definition.input_model.model_validate(arguments)
                except ValidationError:
                    return self._record_denial(
                        definition,
                        principal,
                        run_id,
                        arguments,
                        "SCHEMA_VALIDATION_FAILED",
                    )
                if idempotency_key is None:
                    return self._record_denial(
                        definition,
                        principal,
                        run_id,
                        arguments,
                        "IDEMPOTENCY_KEY_REQUIRED",
                    )
                payload = validated_input.model_dump(mode="json")
                payload_hash = _request_hash(payload)
                call = self._existing_call(
                    principal.tenant_id, definition.name, idempotency_key
                )
                if call is not None:
                    if call.request_hash != payload_hash:
                        raise IdempotencyConflict(
                            "idempotency key was already used for another request"
                        )
                    if call.status != ToolCallStatus.IN_PROGRESS.value:
                        return self._replay(call, payload_hash)
                    outbox = self.session.scalar(
                        select(SideEffectOutbox).where(
                            SideEffectOutbox.tool_call_id == call.id
                        )
                    )
                    if outbox is None:
                        raise RuntimeError("in-progress write has no outbox record")
                else:
                    call = self._new_call(
                        definition=definition,
                        principal=principal,
                        run_id=run_id,
                        idempotency_key=idempotency_key,
                        request_hash=payload_hash,
                        arguments=payload,
                    )
                    outbox = SideEffectOutbox(
                        tenant_id=principal.tenant_id,
                        user_id=principal.user_id,
                        run_id=run_id,
                        tool_call_id=call.id,
                        tool_name=definition.name,
                        idempotency_key=idempotency_key,
                        payload=payload,
                        status=OutboxStatus.PENDING.value,
                        attempts=0,
                        available_at=self.clock(),
                    )
                    self.session.add(outbox)
                    self.session.flush()
                    self._add_outbox_event(
                        outbox,
                        "OUTBOX_ENQUEUED",
                        {"status": outbox.status},
                    )
                outbox_id = outbox.id
            return self._dispatch_outbox(
                outbox_id=outbox_id,
                definition=definition,
                principal=principal,
                run_id=run_id,
                payload_hash=payload_hash,
            )
        except IdempotencyConflict:
            raise
        except Exception as exc:
            self.session.rollback()
            return self._fail_existing_outbox(
                definition=definition,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
                error_code=(
                    exc.code if isinstance(exc, BusinessError) else "INTERNAL_ERROR"
                ),
            )

    def _dispatch_outbox(
        self,
        *,
        outbox_id: UUID,
        definition: ToolDefinition,
        principal: Principal,
        run_id: UUID,
        payload_hash: str,
    ) -> ToolExecutionResponse:
        now = self.clock()
        with self.session.begin():
            outbox = self.session.scalar(
                select(SideEffectOutbox)
                .where(SideEffectOutbox.id == outbox_id)
                .with_for_update()
            )
            if outbox is None:
                raise RuntimeError("outbox record disappeared")
            call = self.session.get(ToolCall, outbox.tool_call_id)
            if call is None:
                raise RuntimeError("outbox tool call disappeared")
            if outbox.status in {
                OutboxStatus.SUCCEEDED.value,
                OutboxStatus.FAILED.value,
                OutboxStatus.CANCELLED.value,
            }:
                return self._replay(call, payload_hash)
            if (
                outbox.status == OutboxStatus.IN_PROGRESS.value
                and outbox.lease_expires_at is not None
                and self._as_utc(outbox.lease_expires_at) > now
            ):
                return ToolExecutionResponse(
                    status=ToolExecutionStatus.FAILED,
                    tool_call_id=call.id,
                    error="OUTBOX_IN_PROGRESS",
                    replayed=True,
                )
            outbox.status = OutboxStatus.IN_PROGRESS.value
            outbox.attempts += 1
            outbox.lease_expires_at = now + timedelta(seconds=self.outbox_lease_seconds)
            self._add_outbox_event(
                outbox,
                "OUTBOX_CLAIMED",
                {"attempt": outbox.attempts},
            )
            self.session.flush()

        if self.after_outbox_claim is not None:
            self.after_outbox_claim()

        try:
            with self.session.begin():
                outbox = self.session.scalar(
                    select(SideEffectOutbox)
                    .where(SideEffectOutbox.id == outbox_id)
                    .with_for_update()
                )
                if outbox is None:
                    raise RuntimeError("outbox record disappeared")
                call = self.session.get(ToolCall, outbox.tool_call_id)
                run = self.session.scalar(
                    select(WorkflowRun)
                    .where(WorkflowRun.id == run_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                if call is None or run is None:
                    raise RuntimeError("outbox execution context disappeared")
                if run.cancel_requested_at is not None or run.state == "CANCELLED":
                    return self._cancel_claimed_outbox(outbox, call, principal, run_id)
                validated_input = definition.input_model.model_validate(outbox.payload)
                raw_output = definition.handler(
                    BusinessService(self.session), validated_input, principal, run_id
                )
                output = definition.output_model.model_validate(raw_output)
                result = output.model_dump(mode="json")
                outbox.status = OutboxStatus.SUCCEEDED.value
                outbox.result_redacted = redact_payload(result, definition.pii_fields)
                outbox.lease_expires_at = None
                self._add_outbox_event(
                    outbox,
                    "OUTBOX_SUCCEEDED",
                    {"attempt": outbox.attempts, "result": outbox.result_redacted},
                )
                call.status = ToolCallStatus.SUCCEEDED.value
                call.result_redacted = outbox.result_redacted
                call.completed_at = self.clock()
                self._add_audit(
                    call,
                    principal,
                    run_id,
                    "TOOL_CALL_SUCCEEDED",
                    {"status": call.status, "result": call.result_redacted},
                )
                self.session.flush()
                return ToolExecutionResponse(
                    status=ToolExecutionStatus.SUCCEEDED,
                    tool_call_id=call.id,
                    result=result,
                )
        except Exception as exc:
            self.session.rollback()
            return self._fail_existing_outbox(
                definition=definition,
                principal=principal,
                run_id=run_id,
                idempotency_key=None,
                outbox_id=outbox_id,
                error_code=(
                    exc.code if isinstance(exc, BusinessError) else "INTERNAL_ERROR"
                ),
            )

    def _cancel_claimed_outbox(
        self,
        outbox: SideEffectOutbox,
        call: ToolCall,
        principal: Principal,
        run_id: UUID,
    ) -> ToolExecutionResponse:
        outbox.status = OutboxStatus.CANCELLED.value
        outbox.error_code = "WORKFLOW_CANCELLED"
        outbox.lease_expires_at = None
        self._add_outbox_event(
            outbox,
            "OUTBOX_CANCELLED",
            {"attempt": outbox.attempts, "error_code": outbox.error_code},
        )
        call.status = ToolCallStatus.DENIED.value
        call.error_code = "WORKFLOW_CANCELLED"
        call.completed_at = self.clock()
        self._add_audit(
            call,
            principal,
            run_id,
            "TOOL_CALL_DENIED",
            {"status": call.status, "error_code": call.error_code},
        )
        self.session.flush()
        return ToolExecutionResponse(
            status=ToolExecutionStatus.DENIED,
            tool_call_id=call.id,
            error="WORKFLOW_CANCELLED",
        )

    def _fail_existing_outbox(
        self,
        *,
        definition: ToolDefinition,
        principal: Principal,
        run_id: UUID,
        idempotency_key: str | None,
        error_code: str,
        outbox_id: UUID | None = None,
    ) -> ToolExecutionResponse:
        missing_outbox = False
        with self.session.begin():
            outbox = (
                self.session.get(SideEffectOutbox, outbox_id)
                if outbox_id is not None
                else self.session.scalar(
                    select(SideEffectOutbox).where(
                        SideEffectOutbox.tenant_id == principal.tenant_id,
                        SideEffectOutbox.tool_name == definition.name,
                        SideEffectOutbox.idempotency_key == idempotency_key,
                    )
                )
            )
            if outbox is None:
                missing_outbox = True
            else:
                call = self.session.get(ToolCall, outbox.tool_call_id)
                if call is None:
                    raise RuntimeError("outbox tool call disappeared")
                outbox.status = OutboxStatus.FAILED.value
                outbox.error_code = error_code
                outbox.lease_expires_at = None
                self._add_outbox_event(
                    outbox,
                    "OUTBOX_FAILED",
                    {"attempt": outbox.attempts, "error_code": error_code},
                )
                call.status = ToolCallStatus.FAILED.value
                call.error_code = error_code
                call.completed_at = self.clock()
                self._add_audit(
                    call,
                    principal,
                    run_id,
                    "TOOL_CALL_FAILED",
                    {"status": call.status, "error_code": error_code},
                )
                self.session.flush()
                return ToolExecutionResponse(
                    status=ToolExecutionStatus.FAILED,
                    tool_call_id=call.id,
                    error=error_code,
                )
        if missing_outbox:
            return self._persist_rolled_back_failure(
                definition=definition,
                arguments={},
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
                error_code=error_code,
            )
        raise RuntimeError("outbox failure was not persisted")

    def _add_outbox_event(
        self,
        outbox: SideEffectOutbox,
        event_type: str,
        payload: dict[str, Any],
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
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _execute_transaction(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: Principal,
        run_id: UUID,
        idempotency_key: str | None,
    ) -> ToolExecutionResponse:
        definition = self.registry.get(tool_name)
        service = BusinessService(self.session)
        with self.session.begin():
            service.require_run(principal, run_id)
            decision = authorize_tool(principal, definition)
            if decision is not AuthorizationDecision.ALLOW:
                return self._record_denial(
                    definition,
                    principal,
                    run_id,
                    arguments,
                    f"AUTHORIZATION_{decision.value}",
                )
            try:
                validated_input = definition.input_model.model_validate(arguments)
            except ValidationError:
                return self._record_denial(
                    definition,
                    principal,
                    run_id,
                    arguments,
                    "SCHEMA_VALIDATION_FAILED",
                )
            if definition.idempotency_required and idempotency_key is None:
                return self._record_denial(
                    definition,
                    principal,
                    run_id,
                    arguments,
                    "IDEMPOTENCY_KEY_REQUIRED",
                )

            payload = validated_input.model_dump(mode="json")
            payload_hash = _request_hash(payload)
            existing = self._existing_call(
                principal.tenant_id,
                definition.name,
                idempotency_key,
            )
            if existing is not None:
                return self._replay(existing, payload_hash)

            call = self._new_call(
                definition=definition,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
                request_hash=payload_hash,
                arguments=payload,
            )
            if definition.risk is RiskClass.WRITE_HIGH_RISK:
                return self._persist_approval_required(
                    call,
                    definition,
                    principal,
                    run_id,
                    payload,
                )

            raw_output = definition.handler(service, validated_input, principal, run_id)
            output = definition.output_model.model_validate(raw_output)

            result = output.model_dump(mode="json")
            call.status = ToolCallStatus.SUCCEEDED.value
            call.result_redacted = redact_payload(result, definition.pii_fields)
            call.completed_at = utc_now()
            self._add_audit(
                call,
                principal,
                run_id,
                "TOOL_CALL_SUCCEEDED",
                {"status": call.status, "result": call.result_redacted},
            )
            self.session.flush()
            return ToolExecutionResponse(
                status=ToolExecutionStatus.SUCCEEDED,
                tool_call_id=call.id,
                result=result,
            )

    def _existing_call(
        self,
        tenant_id: UUID,
        tool_name: str,
        idempotency_key: str | None,
    ) -> ToolCall | None:
        if idempotency_key is None:
            return None
        return self.session.scalar(
            select(ToolCall).where(
                ToolCall.tenant_id == tenant_id,
                ToolCall.tool_name == tool_name,
                ToolCall.idempotency_key == idempotency_key,
            )
        )

    def _new_call(
        self,
        *,
        definition: ToolDefinition | DirectOperationDefinition,
        principal: Principal,
        run_id: UUID,
        idempotency_key: str | None,
        request_hash: str,
        arguments: dict[str, Any],
    ) -> ToolCall:
        call = ToolCall(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            tool_name=definition.name,
            tool_version=definition.version,
            risk_class=definition.risk.value,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            arguments_redacted=redact_payload(arguments, definition.pii_fields),
            status=ToolCallStatus.IN_PROGRESS.value,
        )
        self.session.add(call)
        self.session.flush()
        return call

    def _record_denial(
        self,
        definition: ToolDefinition,
        principal: Principal,
        run_id: UUID,
        arguments: dict[str, Any],
        error_code: str,
    ) -> ToolExecutionResponse:
        call = self._new_call(
            definition=definition,
            principal=principal,
            run_id=run_id,
            idempotency_key=None,
            request_hash=_request_hash(arguments),
            arguments=arguments,
        )
        call.status = ToolCallStatus.DENIED.value
        call.error_code = error_code
        call.completed_at = utc_now()
        self._add_audit(
            call,
            principal,
            run_id,
            "TOOL_CALL_DENIED",
            {"status": call.status, "error_code": error_code},
        )
        self.session.flush()
        return ToolExecutionResponse(
            status=ToolExecutionStatus.DENIED,
            tool_call_id=call.id,
            error=error_code,
        )

    def _persist_rolled_back_failure(
        self,
        *,
        definition: ToolDefinition | DirectOperationDefinition,
        arguments: dict[str, Any],
        principal: Principal,
        run_id: UUID,
        idempotency_key: str | None,
        error_code: str,
    ) -> ToolExecutionResponse:
        with self.session.begin():
            BusinessService(self.session).require_run(principal, run_id)
            call = self._new_call(
                definition=definition,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
                request_hash=_request_hash(arguments),
                arguments=arguments,
            )
            call.status = ToolCallStatus.FAILED.value
            call.error_code = error_code
            call.completed_at = utc_now()
            self._add_audit(
                call,
                principal,
                run_id,
                "TOOL_CALL_FAILED",
                {"status": call.status, "error_code": error_code},
            )
            self.session.flush()
            return ToolExecutionResponse(
                status=ToolExecutionStatus.FAILED,
                tool_call_id=call.id,
                error=error_code,
            )

    def _persist_approval_required(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        principal: Principal,
        run_id: UUID,
        payload: dict[str, Any],
    ) -> ToolExecutionResponse:
        approval = Approval(
            tenant_id=principal.tenant_id,
            run_id=run_id,
            requested_by_user_id=principal.user_id,
            tool_name=definition.name,
            tool_arguments=payload,
            tool_arguments_available=True,
            tool_arguments_redacted=redact_payload(payload, definition.pii_fields),
            status=ApprovalStatus.PENDING.value,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.session.add(approval)
        self.session.flush()
        call.status = ToolCallStatus.AWAITING_APPROVAL.value
        call.approval_id = approval.id
        call.completed_at = utc_now()
        self._add_audit(
            call,
            principal,
            run_id,
            "APPROVAL_REQUIRED",
            {"status": call.status, "approval_id": str(approval.id)},
        )
        self.session.flush()
        return ToolExecutionResponse(
            status=ToolExecutionStatus.APPROVAL_REQUIRED,
            tool_call_id=call.id,
            approval_id=approval.id,
        )

    def _replay(self, call: ToolCall, payload_hash: str) -> ToolExecutionResponse:
        if call.request_hash != payload_hash:
            raise IdempotencyConflict("idempotency key was already used for another request")
        status_map = {
            ToolCallStatus.SUCCEEDED.value: ToolExecutionStatus.SUCCEEDED,
            ToolCallStatus.DENIED.value: ToolExecutionStatus.DENIED,
            ToolCallStatus.AWAITING_APPROVAL.value: ToolExecutionStatus.APPROVAL_REQUIRED,
            ToolCallStatus.FAILED.value: ToolExecutionStatus.FAILED,
        }
        return ToolExecutionResponse(
            status=status_map.get(call.status, ToolExecutionStatus.FAILED),
            tool_call_id=call.id,
            result=call.result_redacted,
            error=call.error_code,
            approval_id=call.approval_id,
            replayed=True,
        )

    def _add_audit(
        self,
        call: ToolCall,
        principal: Principal,
        run_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        run = self.session.get(WorkflowRun, run_id)
        self.session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                run_id=run_id,
                tool_call_id=call.id,
                tool_name=call.tool_name,
                state=run.state if run is not None else "UNKNOWN",
                event_type=event_type,
                payload_redacted=payload,
            )
        )


class DirectOperationExecutor(ToolExecutor):
    def execute_direct(
        self,
        *,
        definition: DirectOperationDefinition,
        arguments: dict[str, Any],
        principal: Principal,
        run_id: UUID,
        idempotency_key: str,
    ) -> ToolExecutionResponse:
        try:
            return self._execute_direct_transaction(
                definition=definition,
                arguments=arguments,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
            )
        except (IdempotencyConflict, PermissionError):
            raise
        except Exception as exc:
            self.session.rollback()
            error_code = exc.code if isinstance(exc, BusinessError) else "INTERNAL_ERROR"
            return self._persist_rolled_back_failure(
                definition=definition,
                arguments=arguments,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
                error_code=error_code,
            )

    def _execute_direct_transaction(
        self,
        *,
        definition: DirectOperationDefinition,
        arguments: dict[str, Any],
        principal: Principal,
        run_id: UUID,
        idempotency_key: str,
    ) -> ToolExecutionResponse:
        service = BusinessService(self.session)
        with self.session.begin():
            service.require_run(principal, run_id)
            if not _is_authorized_for_direct(principal, definition):
                raise PermissionError("direct operation not authorized")
            validated_input = definition.input_model.model_validate(arguments)
            payload = validated_input.model_dump(mode="json")
            payload_hash = _request_hash(payload)
            existing = self._existing_call(
                principal.tenant_id,
                definition.name,
                idempotency_key,
            )
            if existing is not None:
                return self._replay(existing, payload_hash)
            call = self._new_call(
                definition=definition,
                principal=principal,
                run_id=run_id,
                idempotency_key=idempotency_key,
                request_hash=payload_hash,
                arguments=payload,
            )
            raw_output = definition.handler(service, validated_input, principal, run_id)
            output = definition.output_model.model_validate(raw_output)
            result = output.model_dump(mode="json")
            call.status = ToolCallStatus.SUCCEEDED.value
            call.result_redacted = redact_payload(result, definition.pii_fields)
            call.completed_at = utc_now()
            self._add_audit(
                call,
                principal,
                run_id,
                "DIRECT_OPERATION_SUCCEEDED",
                {"status": call.status, "result": call.result_redacted},
            )
            self.session.flush()
            return ToolExecutionResponse(
                status=ToolExecutionStatus.SUCCEEDED,
                tool_call_id=call.id,
                result=result,
            )
