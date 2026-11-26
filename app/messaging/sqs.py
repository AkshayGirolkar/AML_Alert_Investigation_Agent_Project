"""Async SQS wrapper (aioboto3). Works against LocalStack or real AWS.

Delivery is at-least-once: a message is only removed by `delete`, so a crash or an
error leaves it to reappear after the visibility timeout, and after
`sqs_max_receive_count` receives SQS moves it to the dead-letter queue (redrive policy).
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import aioboto3  # type: ignore[import-untyped]

from app.config import Settings


@dataclass(frozen=True)
class QueueMessage:
    receipt_handle: str
    case_id: int
    alert_id: int
    receive_count: int
    kind: str = (
        "investigate"  # or "resume" (F08: an analyst decided, continue the graph)
    )


class InvestigationQueue(Protocol):
    async def send(
        self, case_id: int, alert_id: int, kind: str = "investigate"
    ) -> None: ...

    async def receive(
        self, max_messages: int = 1, wait_seconds: int = 20
    ) -> list[QueueMessage]: ...

    async def delete(self, receipt_handle: str) -> None: ...

    async def change_visibility(self, receipt_handle: str, seconds: int) -> None: ...


def _session(settings: Settings) -> aioboto3.Session:
    return aioboto3.Session(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


@asynccontextmanager
async def sqs_client(settings: Settings) -> AsyncIterator[Any]:
    async with _session(settings).client(
        "sqs", endpoint_url=settings.aws_endpoint_url
    ) as client:
        yield client


async def ensure_queues(settings: Settings) -> tuple[str, str]:
    """Create the DLQ and the main queue with a redrive policy. Idempotent.

    Returns (queue_url, dlq_url).
    """
    async with sqs_client(settings) as sqs:
        dlq = await sqs.create_queue(QueueName=settings.sqs_dlq_name)
        dlq_url = dlq["QueueUrl"]
        attrs = await sqs.get_queue_attributes(
            QueueUrl=dlq_url, AttributeNames=["QueueArn"]
        )
        redrive = {
            "deadLetterTargetArn": attrs["Attributes"]["QueueArn"],
            "maxReceiveCount": settings.sqs_max_receive_count,
        }
        queue = await sqs.create_queue(
            QueueName=settings.sqs_queue_name,
            Attributes={
                "VisibilityTimeout": str(settings.sqs_visibility_timeout),
                "RedrivePolicy": json.dumps(redrive),
            },
        )
        return queue["QueueUrl"], dlq_url


class SqsQueue:
    """Bound to one open client; use `open_queue` to create it."""

    def __init__(self, client: Any, queue_url: str, dlq_url: str | None = None) -> None:
        self._sqs = client
        self.queue_url = queue_url
        self.dlq_url = dlq_url

    async def depths(self) -> tuple[int, int]:
        """(visible + in-flight messages on the queue, messages in the DLQ) (F13)."""

        async def count(url: str) -> int:
            resp = await self._sqs.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )
            a = resp["Attributes"]
            return int(a["ApproximateNumberOfMessages"]) + int(
                a["ApproximateNumberOfMessagesNotVisible"]
            )

        return await count(self.queue_url), (
            await count(self.dlq_url) if self.dlq_url else 0
        )

    async def send(
        self, case_id: int, alert_id: int, kind: str = "investigate"
    ) -> None:
        await self._sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(
                {"case_id": case_id, "alert_id": alert_id, "type": kind}
            ),
        )

    async def receive(
        self, max_messages: int = 1, wait_seconds: int = 20
    ) -> list[QueueMessage]:
        resp = await self._sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        )
        out: list[QueueMessage] = []
        for m in resp.get("Messages", []):
            try:
                body = json.loads(m["Body"])
                kind = body.get("type", "investigate")  # absent in pre-F08 messages
                if kind not in ("investigate", "resume"):
                    raise ValueError(kind)
                out.append(
                    QueueMessage(
                        receipt_handle=m["ReceiptHandle"],
                        case_id=int(body["case_id"]),
                        alert_id=int(body["alert_id"]),
                        receive_count=int(
                            m.get("Attributes", {}).get("ApproximateReceiveCount", 1)
                        ),
                        kind=kind,
                    )
                )
            except (ValueError, KeyError, TypeError):
                # Malformed body: leave it alone; redrive sends it to the DLQ.
                continue
        return out

    async def delete(self, receipt_handle: str) -> None:
        await self._sqs.delete_message(
            QueueUrl=self.queue_url, ReceiptHandle=receipt_handle
        )

    async def change_visibility(self, receipt_handle: str, seconds: int) -> None:
        await self._sqs.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=seconds,
        )


@asynccontextmanager
async def open_queue(settings: Settings) -> AsyncIterator[SqsQueue]:
    """Open a client and resolve the queue URL (creating the queues if missing)."""
    url, dlq_url = await ensure_queues(settings)
    async with sqs_client(settings) as client:
        yield SqsQueue(client, url, dlq_url)
