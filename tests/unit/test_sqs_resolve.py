"""Queue resolution: create against LocalStack, look up on real AWS (F17)."""

from typing import Any

from app.config import Settings
from app.messaging.sqs import resolve_queues
from app.redis_client import make_redis


class FakeSqs:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.attrs: dict[str, Any] = {}

    async def get_queue_url(self, QueueName: str) -> dict[str, str]:
        self.calls.append(f"get:{QueueName}")
        return {"QueueUrl": f"https://sqs/{QueueName}"}

    async def create_queue(self, QueueName: str, **kw: Any) -> dict[str, str]:
        self.calls.append(f"create:{QueueName}")
        self.attrs = kw.get("Attributes") or {}
        return {"QueueUrl": f"https://sqs/{QueueName}"}

    async def get_queue_attributes(self, **kw: Any) -> dict[str, Any]:
        return {"Attributes": {"QueueArn": "arn:dlq"}}


def _settings() -> Settings:
    return Settings(postgres_password="x")  # type: ignore[call-arg]


async def test_lookup_only_when_not_creating() -> None:
    sqs = FakeSqs()
    s = _settings()
    url, dlq = await resolve_queues(sqs, s, create=False)
    assert url.endswith(s.sqs_queue_name) and dlq.endswith(s.sqs_dlq_name)
    assert all(c.startswith("get:") for c in sqs.calls)


async def test_create_sets_redrive_and_visibility() -> None:
    sqs = FakeSqs()
    s = _settings()
    url, dlq = await resolve_queues(sqs, s, create=True)
    assert sqs.calls[0].startswith("create:") and url != dlq
    assert sqs.attrs["VisibilityTimeout"] == str(s.sqs_visibility_timeout)
    assert "arn:dlq" in sqs.attrs["RedrivePolicy"]


def test_redis_accepts_tls_url() -> None:
    s = Settings(postgres_password="x", redis_url="rediss://cache.example:6379/0")  # type: ignore[call-arg]
    r = make_redis(s)
    assert r.connection_pool.connection_class.__name__ == "SSLConnection"


def test_database_urls_escape_special_password_chars() -> None:
    from sqlalchemy.engine import make_url

    pw = "p#ss%w?rd:@/x"
    s = Settings(postgres_password=pw, postgres_host="db.example")  # type: ignore[call-arg]
    for url in (s.sync_database_url, s.async_database_url):
        parsed = make_url(url)
        assert parsed.password == pw and parsed.host == "db.example"
