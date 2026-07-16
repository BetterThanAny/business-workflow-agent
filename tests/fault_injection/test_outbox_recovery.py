from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select

from business_workflow_agent.auth import Principal, Role, granted_scopes
from business_workflow_agent.db import Base, create_database_engine, create_session_factory
from business_workflow_agent.execution import ToolExecutor
from business_workflow_agent.models import (
    Customer,
    SideEffectEvent,
    SideEffectOutbox,
    Ticket,
    ToolCall,
    WorkflowRun,
)
from business_workflow_agent.tools.registry import build_tool_registry


class SimulatedProcessCrash(BaseException):
    pass


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 16, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _fixture(tmp_path: Path) -> tuple[object, object, Principal, UUID, UUID]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'outbox.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    roles = frozenset({Role.SUPPORT_AGENT})
    principal = Principal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        roles=roles,
        scopes=granted_scopes(roles),
    )
    customer_id = uuid4()
    run_id = uuid4()
    with factory.begin() as session:
        session.add(
            Customer(
                id=customer_id,
                tenant_id=principal.tenant_id,
                external_id="outbox-customer",
                name="Example Customer",
                email="customer@example.com",
            )
        )
        session.add(
            WorkflowRun(
                id=run_id,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                state="EXECUTE",
                version=1,
                budget={"max_steps": 20},
            )
        )
    return engine, factory, principal, run_id, customer_id


def _arguments(customer_id: UUID) -> dict[str, object]:
    return {
        "customer_id": str(customer_id),
        "title": "Crash-safe ticket",
        "description": "The outbox must recover exactly once.",
        "priority": "HIGH",
    }


def test_claimed_outbox_recovers_after_process_crash_and_ten_replays(
    tmp_path: Path,
) -> None:
    engine, factory, principal, run_id, customer_id = _fixture(tmp_path)
    clock = MutableClock()

    def crash_after_claim() -> None:
        raise SimulatedProcessCrash()

    with factory() as session:
        executor = ToolExecutor(
            session,
            build_tool_registry(),
            clock=clock,
            after_outbox_claim=crash_after_claim,
        )
        try:
            executor.execute(
                tool_name="create_ticket",
                arguments=_arguments(customer_id),
                principal=principal,
                run_id=run_id,
                idempotency_key="outbox-crash-replay",
            )
        except SimulatedProcessCrash:
            pass
        else:
            raise AssertionError("the injected process crash did not occur")

    with factory() as session:
        outbox = session.scalar(select(SideEffectOutbox))
        assert outbox is not None and outbox.status == "IN_PROGRESS"
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0
        assert list(
            session.scalars(
                select(SideEffectEvent.event_type).order_by(SideEffectEvent.sequence)
            )
        ) == ["OUTBOX_ENQUEUED", "OUTBOX_CLAIMED"]

    clock.now += timedelta(seconds=31)
    responses = []
    for _ in range(10):
        with factory() as session:
            responses.append(
                ToolExecutor(session, build_tool_registry(), clock=clock).execute(
                    tool_name="create_ticket",
                    arguments=_arguments(customer_id),
                    principal=principal,
                    run_id=run_id,
                    idempotency_key="outbox-crash-replay",
                )
            )

    assert all(response.status == "SUCCEEDED" for response in responses)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 1
        assert session.scalar(select(func.count()).select_from(SideEffectOutbox)) == 1
        outbox = session.scalar(select(SideEffectOutbox))
        assert outbox is not None and outbox.status == "SUCCEEDED"
        assert outbox.attempts == 2
        assert list(
            session.scalars(
                select(SideEffectEvent.event_type).order_by(SideEffectEvent.sequence)
            )
        ) == [
            "OUTBOX_ENQUEUED",
            "OUTBOX_CLAIMED",
            "OUTBOX_CLAIMED",
            "OUTBOX_SUCCEEDED",
        ]
    engine.dispose()


def test_cancellation_after_outbox_claim_prevents_write_from_starting(
    tmp_path: Path,
) -> None:
    engine, factory, principal, run_id, customer_id = _fixture(tmp_path)
    clock = MutableClock()

    def cancel_after_claim() -> None:
        with factory.begin() as cancellation_session:
            run = cancellation_session.get(WorkflowRun, run_id)
            assert run is not None
            run.cancel_requested_at = clock.now
            run.state = "CANCELLED"
            run.error_code = "WORKFLOW_CANCELLED"

    with factory() as session:
        response = ToolExecutor(
            session,
            build_tool_registry(),
            clock=clock,
            after_outbox_claim=cancel_after_claim,
        ).execute(
            tool_name="create_ticket",
            arguments=_arguments(customer_id),
            principal=principal,
            run_id=run_id,
            idempotency_key="outbox-cancelled",
        )

    assert response.status == "DENIED"
    assert response.error == "WORKFLOW_CANCELLED"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0
        outbox = session.scalar(select(SideEffectOutbox))
        call = session.scalar(select(ToolCall))
        assert outbox is not None and outbox.status == "CANCELLED"
        assert call is not None and call.status == "DENIED"
    engine.dispose()
