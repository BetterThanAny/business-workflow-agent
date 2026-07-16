from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

import jwt


class Role(StrEnum):
    SUPPORT_AGENT = "SUPPORT_AGENT"
    REFUND_MANAGER = "REFUND_MANAGER"
    AUDITOR = "AUDITOR"
    ADMIN = "ADMIN"


ROLE_SCOPES: dict[Role, frozenset[str]] = {
    Role.SUPPORT_AGENT: frozenset(
        {
            "knowledge:read",
            "customer:read",
            "ticket:read",
            "ticket:write",
            "refund:calculate",
            "approval:request",
            "workflow:create",
            "tool:schema:read",
        }
    ),
    Role.REFUND_MANAGER: frozenset(
        {
            "knowledge:read",
            "customer:read",
            "ticket:read",
            "refund:calculate",
            "refund:issue",
            "approval:request",
            "approval:read",
            "approval:decide",
            "workflow:create",
            "tool:schema:read",
        }
    ),
    Role.AUDITOR: frozenset(
        {
            "customer:read",
            "ticket:read",
            "audit:read",
            "approval:read",
            "tool:schema:read",
        }
    ),
    Role.ADMIN: frozenset(
        {
            "knowledge:read",
            "customer:read",
            "customer:write",
            "ticket:read",
            "ticket:write",
            "refund:calculate",
            "refund:issue",
            "approval:request",
            "approval:read",
            "approval:decide",
            "workflow:create",
            "tool:schema:read",
            "audit:read",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    tenant_id: UUID
    roles: frozenset[Role]
    scopes: frozenset[str]


def granted_scopes(roles: Iterable[Role]) -> frozenset[str]:
    return frozenset(scope for role in roles for scope in ROLE_SCOPES[role])


def create_access_token(
    principal: Principal,
    *,
    secret: str,
    issuer: str,
    audience: str,
    ttl_seconds: int = 3600,
) -> str:
    role_grants = granted_scopes(principal.roles)
    if not principal.scopes <= role_grants:
        raise ValueError("token scopes exceed the server-side role grants")

    now = datetime.now(UTC)
    claims = {
        "sub": str(principal.user_id),
        "tenant_id": str(principal.tenant_id),
        "roles": sorted(role.value for role in principal.roles),
        "scopes": sorted(principal.scopes),
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(  # pyright: ignore[reportUnknownMemberType]
        claims, secret, algorithm="HS256"
    )


def decode_access_token(
    token: str,
    *,
    secret: str,
    issuer: str,
    audience: str,
) -> Principal:
    claims = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
        token,
        secret,
        algorithms=["HS256"],
        issuer=issuer,
        audience=audience,
        options={"require": ["sub", "tenant_id", "roles", "scopes", "exp", "iat"]},
    )
    roles = frozenset(Role(value) for value in claims["roles"])
    scopes = frozenset(str(value) for value in claims["scopes"])
    if not scopes <= granted_scopes(roles):
        raise jwt.InvalidTokenError("token scopes exceed role grants")
    return Principal(
        user_id=UUID(claims["sub"]),
        tenant_id=UUID(claims["tenant_id"]),
        roles=roles,
        scopes=scopes,
    )
