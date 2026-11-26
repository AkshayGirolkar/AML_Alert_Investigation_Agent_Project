"""Locust version of the F16 load generator (interactive, with the web UI).

LOADTEST_MANIFEST=loadtest/datasets/load-1000.json \
  .venv/bin/locust -f loadtest/locustfile.py --host http://localhost:8000
For repeatable numbers use scripts/loadtest_run.py (it also resets, waits for the drain
and writes the result file). Each user takes the next alert id from the manifest, so no
alert is sent twice; when the list is used up the run stops.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path

from locust import HttpUser, constant_throughput, task

_IDS: Iterator[int] = iter(
    json.loads(
        Path(
            os.environ.get("LOADTEST_MANIFEST", "loadtest/datasets/load-1000.json")
        ).read_text()
    )["alert_ids"]
)


class Analyst(HttpUser):
    # ~2 requests/second per user; add users to raise the arrival rate
    wait_time = constant_throughput(2)

    @task
    def investigate(self) -> None:
        alert_id = next(_IDS, None)
        if alert_id is None:
            self.environment.runner.quit()  # type: ignore[union-attr]
            return
        self.client.post(
            f"/alerts/{alert_id}/investigations", name="/alerts/{id}/investigations"
        )
