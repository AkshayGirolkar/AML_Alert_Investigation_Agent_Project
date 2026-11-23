"""F07 integration: SQS wrapper, redrive to the DLQ, and the worker poll loop.

Needs LocalStack (`docker compose up -d localstack`); skipped when it is not reachable.
Uses throw-away queue names so it never touches the app's real queues.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from botocore.exceptions import EndpointConnectionError  # type: ignore[import-untyped]

from app.config import get_settings
from app.messaging.sqs import SqsQueue, ensure_queues, sqs_client
from app.worker.main import poll_loop

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def queues() -> AsyncIterator[tuple[SqsQueue, SqsQueue, str]]:
    suffix = uuid.uuid4().hex[:8]
    s = get_settings().model_copy(
        update={
            "aws_endpoint_url": get_settings().aws_endpoint_url
            or "http://localhost:4566",
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
            "sqs_queue_name": f"test-{suffix}",
            "sqs_dlq_name": f"test-{suffix}-dlq",
            "sqs_max_receive_count": 2,
            "sqs_visibility_timeout": 1,
        }
    )
    try:
        url, dlq_url = await asyncio.wait_for(ensure_queues(s), timeout=5)
    except (OSError, TimeoutError, EndpointConnectionError):
        pytest.skip("LocalStack is not reachable")
    async with sqs_client(s) as client:
        yield SqsQueue(client, url), SqsQueue(client, dlq_url), url
        for u in (url, dlq_url):
            await client.delete_queue(QueueUrl=u)


async def test_send_receive_delete_roundtrip(
    queues: tuple[SqsQueue, SqsQueue, str],
) -> None:
    q, _, _ = queues
    await q.send(case_id=5, alert_id=7)
    [msg] = await q.receive(max_messages=1, wait_seconds=2)
    assert (msg.case_id, msg.alert_id, msg.receive_count) == (5, 7, 1)
    await q.delete(msg.receipt_handle)
    assert await q.receive(max_messages=1, wait_seconds=1) == []
    assert msg.kind == "investigate"  # default; pre-F08 bodies had no type


async def test_resume_message_kind_roundtrips(
    queues: tuple[SqsQueue, SqsQueue, str],
) -> None:
    q, _, _ = queues
    await q.send(case_id=5, alert_id=7, kind="resume")
    [msg] = await q.receive(max_messages=1, wait_seconds=2)
    assert msg.kind == "resume" and (msg.case_id, msg.alert_id) == (5, 7)
    await q.delete(msg.receipt_handle)


async def test_undeleted_message_is_redelivered_then_dead_lettered(
    queues: tuple[SqsQueue, SqsQueue, str],
) -> None:
    q, dlq, _ = queues  # visibility 1s, maxReceiveCount 2
    await q.send(case_id=1, alert_id=1)
    counts = []
    for _ in range(2):
        [msg] = await q.receive(max_messages=1, wait_seconds=3)
        counts.append(msg.receive_count)  # never deleted: simulates a failing worker
        await asyncio.sleep(1.5)
    assert counts == [1, 2]
    assert await q.receive(max_messages=1, wait_seconds=2) == []  # gone from main queue
    [dead] = await dlq.receive(max_messages=1, wait_seconds=5)
    assert (dead.case_id, dead.alert_id) == (1, 1)


async def test_poll_loop_hands_messages_to_the_processor_and_stops_on_event(
    queues: tuple[SqsQueue, SqsQueue, str],
) -> None:
    q, _, _ = queues
    for i in range(3):
        await q.send(case_id=i, alert_id=i)
    seen: list[int] = []
    stop = asyncio.Event()

    class Proc:
        async def handle(self, msg) -> str:  # type: ignore[no-untyped-def]
            seen.append(msg.case_id)
            await q.delete(msg.receipt_handle)
            if len(seen) == 3:
                stop.set()
            return "completed"

    await asyncio.wait_for(poll_loop(q, Proc(), 2, stop), timeout=30)  # type: ignore[arg-type]
    assert sorted(seen) == [0, 1, 2]
