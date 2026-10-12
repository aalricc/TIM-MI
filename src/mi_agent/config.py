from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MI_", env_file=".env", extra="forbid")

    monitor_interval_seconds: float = Field(default=5.0, ge=0.01)
    max_attempts_per_incident: int = Field(default=25, ge=0)
    max_attempts_per_hour: int = Field(default=250, ge=0)
    max_incident_seconds: int = Field(default=1800, ge=0)
    supervisor_backoff_initial_seconds: float = Field(default=1.0, ge=0.0)
    supervisor_backoff_max_seconds: float = Field(default=30.0, ge=0.0)
    verifier_delayed_reverify_seconds: float = Field(default=0.05, ge=0.0, le=10.0)

    epsilon_start: float = Field(default=0.30, ge=0.0, le=1.0)
    epsilon_min: float = Field(default=0.02, ge=0.0, le=1.0)
    epsilon_decay: float = Field(default=0.985, gt=0.0, le=1.0)
    policy_recency_alpha: float = Field(default=0.3, gt=0.0, le=1.0)
    failed_action_ttl_seconds: float = Field(default=900.0, ge=1.0)
    random_seed: int = 7

    app_root: Path = Path("/target/app")
    sandbox_root: Path = Path("/target")
    backups_root: Path = Path("/target/backups")
    db_file: Path = Path("/target/app/data/app.db")
    app_log: Path = Path("/target/app/app.log")
    schema_file: Path = Path("/target/app/schema.sql")

    target_service_url: str = "http://target-app:8080"
    llm_model: str = "gpt-5.3-codex"
    llm_small_model: str | None = None
    llm_large_model: str | None = None
    llm_escalate_on_validation_failure: bool = True
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
    skill_store_path: Path = Path("./state/skills.md")
    decision_log_path: Path = Path("./state/decision_log.jsonl")
    event_log_path: Path = Path("./state/events.jsonl")
    failure_taxonomy_path: Path = Path("./state/failure_taxonomy.json")
    state_dir: Path = Path("./state")
    policy_version: str = "v1-tabular"
    taxonomy_merge_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    taxonomy_review_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    skill_similarity_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    skill_candidate_to_canary_successes: int = Field(default=2, ge=1)
    skill_canary_to_trusted_successes: int = Field(default=5, ge=1)
    skill_trusted_regression_failures: int = Field(default=3, ge=1)
    candidate_rejection_summary_enabled: bool = True
    candidate_rejection_summary_sample_limit: int = Field(default=5, ge=1, le=25)
    candidate_rejection_log_decision_enabled: bool = True
    readonly_replan_enabled: bool = False
    max_readonly_rounds_per_incident: int = Field(default=2, ge=0, le=10)
    max_readonly_steps_per_round: int = Field(default=5, ge=1, le=20)
    readonly_replan_max_incident_seconds: float = Field(default=120.0, ge=1.0, le=3600.0)
    readonly_replan_recent_rejection_limit: int = Field(default=5, ge=1, le=25)
    readonly_replan_max_chars_per_read: int = Field(default=1000, ge=200, le=10000)
    readonly_replan_max_chars_per_round: int = Field(default=3000, ge=500, le=20000)

    @field_validator("epsilon_min")
    @classmethod
    def _validate_epsilon_min(cls, value: float, info: object) -> float:
        settings_data = getattr(info, "data", {})
        epsilon_start = settings_data.get("epsilon_start", 1.0)
        if value > epsilon_start:
            raise ValueError("epsilon_min must be <= epsilon_start")
        return value

    @field_validator(
        "backups_root",
        "sandbox_root",
        "app_root",
        "db_file",
        "schema_file",
        "app_log",
        "policy_store_path",
        "skill_store_path",
        "decision_log_path",
        "event_log_path",
        "failure_taxonomy_path",
        "state_dir",
    )
    @classmethod
    def _normalize_path(cls, value: Path) -> Path:
        return value.expanduser()
