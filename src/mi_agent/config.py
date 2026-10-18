from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MI_", env_file=".env", extra="ignore")

    monitor_interval_seconds: float = Field(default=1.0, gt=0.0, le=60.0)
    target_service_url: str = "http://target-app:8080"
    monitor_health_path: str = "/health/deep"
    logs_endpoint_path: str = "/ops/logs"
    db_status_endpoint_path: str = "/ops/db/status"
    request_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)

    app_root: Path = Path("/target/app")
    sandbox_root: Path = Path("/target")
    backups_root: Path = Path("/target/backups")
    db_file: Path = Path("/target/app/data/app.db")
    app_log: Path = Path("/target/app/app.log")
    schema_file: Path = Path("/target/app/schema.sql")

    state_dir: Path = Path("./state")
    event_log_path: Path = Path("./state/events.jsonl")
    policy_store_path: Path = Path("./state/policy_store.json")
    tool_catalog_path: Path = Path("./state/tool_catalog.json")

    llm_model: str = "gpt-5.3-codex"
    llm_endpoint: str = "https://api.openai.com/v1/responses"
    llm_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    llm_auth_mode: Literal["bearer", "api_key"] = "bearer"
    llm_api_key_header: str = "api-key"
    openai_api_key: str = Field(
        min_length=1,
        validation_alias=AliasChoices("OPENAI_API_KEY", "MI_OPENAI_API_KEY"),
    )

    jev_enabled: bool = True
    jev_model: str = "jev-latest"
    jev_base_url: str | None = None
    jev_timeout_seconds: float = Field(default=8.0, gt=0.0, le=120.0)
    jev_usable_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    jev_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("JEV_API_KEY", "MI_JEV_API_KEY"),
    )

    rag_enabled: bool = True
    rag_top_k: int = Field(default=5, ge=1, le=10)
    pinecone_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PINECONE_API_KEY", "MI_PINECONE_API_KEY"),
    )
    pinecone_index_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PINECONE_INDEX_HOST", "MI_PINECONE_INDEX_HOST"),
    )
    pinecone_namespace: str = Field(
        default="sqlite-docs",
        validation_alias=AliasChoices("PINECONE_NAMESPACE", "MI_PINECONE_NAMESPACE"),
    )

    @model_validator(mode="after")
    def _validate_runtime_gate(self) -> MISettings:
        if not self.rag_enabled:
            raise ValueError("MI_RAG_ENABLED must remain true")
        if self.pinecone_api_key is None or self.pinecone_api_key.strip() == "":
            raise ValueError("rag_enabled=true requires PINECONE_API_KEY")
        if self.pinecone_index_host is None or self.pinecone_index_host.strip() == "":
            raise ValueError("rag_enabled=true requires PINECONE_INDEX_HOST")
        if self.openai_api_key.strip() == "":
            raise ValueError("OPENAI_API_KEY or MI_OPENAI_API_KEY must be non-empty")
        if not self.jev_enabled:
            raise ValueError("MI_JEV_ENABLED must remain true")
        if self.jev_api_key is None or self.jev_api_key.strip() == "":
            raise ValueError("jev_enabled=true requires JEV_API_KEY")
        return self
