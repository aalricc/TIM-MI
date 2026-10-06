from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MI_", env_file=".env", extra="forbid")

    monitor_interval_seconds: float = Field(default=5.0, ge=0.01)
    max_attempts_per_incident: int = Field(default=25, ge=1)
    max_incident_seconds: int = Field(default=1800, ge=1)
    supervisor_backoff_initial_seconds: float = Field(default=1.0, ge=0.0)
    supervisor_backoff_max_seconds: float = Field(default=30.0, ge=0.0)

    epsilon_start: float = Field(default=0.30, ge=0.0, le=1.0)
    epsilon_min: float = Field(default=0.02, ge=0.0, le=1.0)
    epsilon_decay: float = Field(default=0.985, gt=0.0, le=1.0)
    random_seed: int = 7

    app_root: Path = Path("/target/app")
    sandbox_root: Path = Path("/target")
    backups_root: Path = Path("/target/backups")
    db_file: Path = Path("/target/app/data/app.db")
    app_log: Path = Path("/target/app/app.log")
    schema_file: Path = Path("/target/app/schema.sql")

    target_service_url: str = "http://target-app:8080"
    llm_model: str = "gpt-5.3-codex"
    llm_endpoint: str = "https://api.openai.com/v1/responses"
    llm_auth_mode: Literal["bearer", "api_key"] = "bearer"
    llm_api_key_header: str = "api-key"
    llm_timeout_seconds: float = Field(default=10.0, gt=0.0)
    llm_retries: int = Field(default=2, ge=0)
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "MI_OPENAI_API_KEY"),
    )
    last_resort_url: str | None = None

    policy_store_path: Path = Path("./state/policy_store.json")
    skill_store_path: Path = Path("./state/skills.json")
    state_dir: Path = Path("./state")

    @field_validator("epsilon_min")
    @classmethod
    def _validate_epsilon_min(cls, value: float, info: object) -> float:
        settings_data = getattr(info, "data", {})
        epsilon_start = settings_data.get("epsilon_start", 1.0)
        if value > epsilon_start:
            raise ValueError("epsilon_min must be <= epsilon_start")
        return value

    @field_validator(
        "backups_root", "sandbox_root", "app_root", "db_file", "schema_file", "app_log"
    )
    @classmethod
    def _normalize_path(cls, value: Path) -> Path:
        return value.expanduser()
