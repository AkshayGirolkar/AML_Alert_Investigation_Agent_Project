from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Reads from environment variables and the local `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_user: str = "aml"
    postgres_password: str
    postgres_db: str = "aml"
    postgres_host: str = "localhost"
    postgres_port: int = 5433

    seed: int = 42
    openai_api_key: str = ""
    data_dir: Path = Path("data")

    # F06 agent graph
    llm_plan_model: str = "gpt-4o-mini"
    llm_assess_model: str = "gpt-4o"
    max_tool_calls: int = 8
    close_risk_max: float = 0.30  # auto-close only at or below this risk score
    close_confidence_min: float = 0.80  # ...and at or above this confidence
    # Rules the agent may never auto-close (comma-separated). Batch evals showed no
    # evidence the agent can tell true from false alerts for these; a human decides.
    no_auto_close_rules: str = "fan_in,high_risk_counterparty"

    # F09 long-term memory
    memory_history_limit: int = 5  # prior cases on the same customer shown to the agent
    # Never auto-close a customer who already has an analyst-approved escalation.
    memory_block_auto_close_on_prior_sar: bool = True

    # F07 async execution (SQS via LocalStack locally; endpoint unset on real AWS)
    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    sqs_queue_name: str = "aml-investigations"
    sqs_dlq_name: str = "aml-investigations-dlq"
    sqs_max_receive_count: int = 3
    sqs_visibility_timeout: int = 300  # seconds; must exceed one investigation
    worker_concurrency: int = 4
    # F16: connections shared by the checkpointer and memory store (psycopg pool).
    # 0 = auto: half the worker concurrency, between 4 and 16 (load test: 4 was a limit
    # at concurrency 32, 16 gave +22% throughput; Postgres allows 100 connections).
    worker_db_pool_size: int = 0

    # F10 Redis (sanctions cache, LLM rate limiter, case locks, SSE progress events)
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout: float = 2.0  # seconds; an outage must fail fast
    sanctions_cache_ttl_seconds: int = 3600
    llm_rate_limit_per_second: float = (
        5.0  # shared by all workers; keep under the OpenAI limit
    )
    llm_rate_limit_burst: int = 10
    llm_rate_limit_max_wait_seconds: float = 60.0
    case_lock_ttl_seconds: int = 300  # renewed by the worker heartbeat
    events_ttl_seconds: int = 3600

    # F12 LLM gateway: timeout, retry, fallback model (same OpenAI key), cost budget
    llm_fallback_plan_model: str = "gpt-4o-mini"
    llm_fallback_assess_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2  # per model; a model is tried max_retries + 1 times
    llm_backoff_base_seconds: float = 1.0
    llm_backoff_max_seconds: float = 20.0
    llm_case_budget_usd: float = 1.0  # 0 = no cap; over it the case goes to a human

    # F16 load test: "fake" swaps OpenAI for the deterministic SyntheticLLM (no cost)
    llm_provider: Literal["openai", "fake"] = "openai"
    fake_llm_latency_ms: float = 1500.0
    fake_llm_jitter_ms: float = 0.0
    fake_llm_error_rate: float = 0.0  # share of calls that raise a transient error
    fake_llm_escalate_pct: int = 30  # share of alerts the fake verdict escalates

    # F13 observability
    log_format: str = "text"  # "json" for structured logs (docker/ECS)
    log_level: str = "INFO"
    worker_metrics_port: int = 0  # >0: the worker serves /metrics on this port

    # F10.5 console contract: restricted DB role for the separate console, and the relay
    # that turns its `console.requests` rows into investigations and decisions.
    console_db_password: str | None = None  # None = the console_app role cannot log in
    console_relay_enabled: bool = True
    console_relay_poll_seconds: float = 5.0
    console_relay_max_attempts: int = 5

    # F04 rules engine thresholds (see app/rules/config.py for meaning and validation)
    rule_velocity_window_hours: int = 1
    rule_velocity_min_txns: int = 10
    rule_fan_window_hours: int = 24
    rule_fan_min_degree: int = 8
    rule_round_trip_window_hours: int = 24
    rule_round_trip_max_hops: int = 3
    rule_structuring_limit: float = 10_000
    rule_structuring_margin: float = 0.10
    rule_structuring_min_txns: int = 3
    rule_structuring_window_hours: int = 24
    rule_high_risk_countries: str = "IR,KP,SY,MM,CU"  # comma-separated, ISO-2
    rule_high_risk_ratings: str = "high"  # comma-separated
    rule_high_risk_window_hours: int = 24
    rule_sanctions_similarity: float = 0.5

    @property
    def no_auto_close_rule_set(self) -> frozenset[str]:
        return frozenset(
            r.strip() for r in self.no_auto_close_rules.split(",") if r.strip()
        )

    @property
    def sync_database_url(self) -> str:
        """psycopg URL used by Alembic and the bulk loader."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def async_database_url(self) -> str:
        """asyncpg URL used by the FastAPI app and the worker."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def db_pool_size(self) -> int:
        if self.worker_db_pool_size > 0:
            return self.worker_db_pool_size
        return min(16, max(4, self.worker_concurrency // 2))


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
