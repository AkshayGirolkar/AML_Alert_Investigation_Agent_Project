from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Reads from environment variables and the local `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_user: str = "aml"
    postgres_password: str
    postgres_db: str = "aml"
    postgres_host: str = "localhost"
    postgres_port: int = 5433

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
