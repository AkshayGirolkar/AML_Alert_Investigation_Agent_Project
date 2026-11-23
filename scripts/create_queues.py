"""Create the SQS queue and DLQ (idempotent).  python -m scripts.create_queues"""

import asyncio

from app.config import get_settings
from app.messaging.sqs import ensure_queues


def main() -> int:
    url, dlq = asyncio.run(ensure_queues(get_settings()))
    print(f"queue: {url}\ndlq:   {dlq}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
