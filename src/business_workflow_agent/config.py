from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _empty_knowledge_base_ids() -> dict[UUID, UUID]:
    return {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://workflow@127.0.0.1:54329/workflow"
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_issuer: str = "business-workflow-agent"
    jwt_audience: str = "business-workflow-api"
    jwt_ttl_seconds: int = 3600
    knowledge_backend: Literal["deterministic_stub", "enterprise_rag"] = (
        "deterministic_stub"
    )
    enterprise_rag_base_url: str = "http://127.0.0.1:8010"
    enterprise_rag_bearer_token: SecretStr | None = None
    enterprise_rag_knowledge_base_ids: dict[UUID, UUID] = Field(
        default_factory=_empty_knowledge_base_ids
    )
    enterprise_rag_timeout_seconds: float = Field(default=3, gt=0, le=30)
    enterprise_rag_max_attempts: int = Field(default=2, ge=1, le=5)
    redis_url: str = "redis://127.0.0.1:56379/0"
    knowledge_cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    knowledge_lock_ttl_seconds: int = Field(default=5, ge=1, le=60)
