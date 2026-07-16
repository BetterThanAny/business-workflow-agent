from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from business_workflow_agent.app import create_app
from business_workflow_agent.auth import Principal, Role, create_access_token, granted_scopes
from business_workflow_agent.config import Settings
from business_workflow_agent.db import Base, create_database_engine, create_session_factory

TEST_JWT_SECRET = "test-only-secret-with-more-than-thirty-two-bytes"


@pytest.fixture
def engine() -> Iterator[Engine]:
    database_engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    yield database_engine
    database_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    with factory() as database_session:
        yield database_session


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        jwt_secret=TEST_JWT_SECRET,
        jwt_issuer="test-issuer",
        jwt_audience="test-audience",
    )


@pytest.fixture
def client(engine: Engine, settings: Settings) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings, engine=engine), raise_server_exceptions=False
    ) as test_client:
        yield test_client


@pytest.fixture
def tenant_id() -> UUID:
    return uuid4()


@pytest.fixture
def principal_factory(
    tenant_id: UUID,
) -> Callable[[Role, frozenset[str] | None, UUID | None], Principal]:
    def factory(
        role: Role,
        scopes: frozenset[str] | None = None,
        user_id: UUID | None = None,
    ) -> Principal:
        roles = frozenset({role})
        return Principal(
            user_id=user_id or uuid4(),
            tenant_id=tenant_id,
            roles=roles,
            scopes=scopes if scopes is not None else granted_scopes(roles),
        )

    return factory


@pytest.fixture
def token_factory(
    settings: Settings,
) -> Callable[[Principal], str]:
    def factory(principal: Principal) -> str:
        return create_access_token(
            principal,
            secret=settings.jwt_secret.get_secret_value(),
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )

    return factory


@pytest.fixture
def auth_headers(
    token_factory: Callable[[Principal], str],
) -> Callable[[Principal], dict[str, str]]:
    def factory(principal: Principal) -> dict[str, str]:
        return {"Authorization": f"Bearer {token_factory(principal)}"}

    return factory


@pytest.fixture
def create_run(
    client: TestClient,
    auth_headers: Callable[[Principal], dict[str, str]],
) -> Callable[[Principal], UUID]:
    def factory(principal: Principal) -> UUID:
        response = client.post(
            "/api/v1/workflow-runs",
            json={},
            headers=auth_headers(principal),
        )
        assert response.status_code == 201, response.text
        return UUID(response.json()["id"])

    return factory


@pytest.fixture
def create_customer(
    client: TestClient,
    auth_headers: Callable[[Principal], dict[str, str]],
    create_run: Callable[[Principal], UUID],
) -> Callable[[Principal, str], dict[str, Any]]:
    def factory(admin: Principal, suffix: str = "default") -> dict[str, Any]:
        run_id = create_run(admin)
        response = client.post(
            "/api/v1/customers",
            json={
                "external_id": f"customer-{suffix}",
                "name": "Example Customer",
                "email": "customer@example.com",
            },
            headers={
                **auth_headers(admin),
                "X-Workflow-Run-ID": str(run_id),
                "Idempotency-Key": f"customer-create-{suffix}",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    return factory
