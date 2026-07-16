import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect

from alembic import command

EXPECTED_TABLES = {
    "customer",
    "ticket",
    "ticket_event",
    "approval",
    "workflow_run",
    "tool_call",
    "audit_event",
    "refund",
    "workflow_checkpoint",
    "workflow_event",
    "side_effect_outbox",
    "side_effect_event",
}


def _alembic_config(database_url: str) -> Config:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_initial_migration_builds_complete_schema_on_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration.db'}"
    command.upgrade(_alembic_config(database_url), "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) >= EXPECTED_TABLES
        unique_constraints = inspector.get_unique_constraints("tool_call")
        assert any(
            constraint["column_names"] == ["tenant_id", "tool_name", "idempotency_key"]
            for constraint in unique_constraints
        )
        checkpoint_constraints = inspector.get_unique_constraints("workflow_checkpoint")
        assert any(
            constraint["column_names"] == ["run_id", "version"]
            for constraint in checkpoint_constraints
        )
        workflow_columns = {column["name"] for column in inspector.get_columns("workflow_run")}
        assert {
            "message",
            "context",
            "proposal",
            "step_count",
            "tokens_used",
            "retry_count",
            "retry_from_state",
            "next_retry_at",
            "cancel_requested_at",
        } <= workflow_columns
        approval_columns = {column["name"] for column in inspector.get_columns("approval")}
        assert {
            "tool_arguments",
            "tool_arguments_available",
            "decision_token_issued_to_user_id",
            "decision_token_expires_at",
            "decision_token_used_at",
            "decided_at",
        } <= approval_columns
        tool_call_indexes = inspector.get_indexes("tool_call")
        assert any(
            index["name"] == "uq_tool_call_approval_id" and index["unique"]
            for index in tool_call_indexes
        )
        outbox_constraints = inspector.get_unique_constraints("side_effect_outbox")
        assert any(
            constraint["column_names"] == ["tool_call_id"]
            for constraint in outbox_constraints
        )
        outbox_columns = {
            column["name"] for column in inspector.get_columns("side_effect_outbox")
        }
        assert "event_sequence" in outbox_columns
        event_constraints = inspector.get_unique_constraints("side_effect_event")
        assert any(
            constraint["column_names"] == ["outbox_id", "sequence"]
            for constraint in event_constraints
        )
    finally:
        engine.dispose()


def test_m3_marks_preexisting_approval_payload_unavailable(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'legacy-approval.db'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "20260713_0002")
    engine = create_engine(database_url)
    tenant_id = uuid4()
    user_id = uuid4()
    run_id = uuid4()
    approval_id = uuid4()
    now = datetime.now(UTC)
    try:
        metadata = MetaData()
        metadata.reflect(engine)
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["workflow_run"].insert(),
                {
                    "id": str(run_id),
                    "tenant_id": str(tenant_id),
                    "user_id": str(user_id),
                    "state": "AWAIT_APPROVAL",
                    "version": 6,
                    "budget": {"max_steps": 20},
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                metadata.tables["approval"].insert(),
                {
                    "id": str(approval_id),
                    "tenant_id": str(tenant_id),
                    "run_id": str(run_id),
                    "requested_by_user_id": str(user_id),
                    "tool_name": "issue_refund",
                    "tool_arguments_redacted": {
                        "order_id": "legacy-order",
                        "reason": "[REDACTED]",
                    },
                    "status": "PENDING",
                    "expires_at": now + timedelta(hours=1),
                    "created_at": now,
                    "updated_at": now,
                },
            )
        engine.dispose()

        command.upgrade(config, "head")
        migrated_engine = create_engine(database_url)
        migrated_metadata = MetaData()
        migrated_metadata.reflect(migrated_engine)
        with migrated_engine.connect() as connection:
            row = connection.execute(
                migrated_metadata.tables["approval"]
                .select()
                .where(migrated_metadata.tables["approval"].c.id == str(approval_id))
            ).mappings().one()
        assert row["tool_arguments"] == {}
        assert row["tool_arguments_available"] is False
        migrated_engine.dispose()
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_initial_migration_is_present_on_postgresql() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL integration test")
    command.upgrade(_alembic_config(database_url), "head")
    engine = create_engine(database_url)
    try:
        assert engine.dialect.name == "postgresql"
        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()
