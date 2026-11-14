from pathlib import Path

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


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
