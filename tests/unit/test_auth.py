from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from business_workflow_agent.auth import (
    Principal,
    Role,
    create_access_token,
    decode_access_token,
)

SECRET = "unit-test-secret-with-more-than-thirty-two-bytes"


def test_token_creation_rejects_scope_not_granted_to_role() -> None:
    principal = Principal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        roles=frozenset({Role.SUPPORT_AGENT}),
        scopes=frozenset({"refund:issue"}),
    )

    with pytest.raises(ValueError, match="exceed"):
        create_access_token(
            principal,
            secret=SECRET,
            issuer="test",
            audience="api",
        )


def test_decoder_rejects_signed_role_scope_mismatch() -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "roles": ["SUPPORT_AGENT"],
            "scopes": ["refund:issue"],
            "iss": "test",
            "aud": "api",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(jwt.InvalidTokenError, match="exceed"):
        decode_access_token(token, secret=SECRET, issuer="test", audience="api")

