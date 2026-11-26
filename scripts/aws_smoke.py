"""Smoke test a deployed stack (F17): one alert through investigate, review and resume.

.venv/bin/python -m scripts.aws_smoke --api-url http://<alb-dns> --alert-id 123
.venv/bin/python -m scripts.aws_smoke --api-url http://<alb-dns>    # first alert of the load manifest

Run it from an IP in allowed_cidrs. Exit 0 only if the case reaches `completed`.
"""

import argparse
import asyncio
import json
import os

import httpx

from app.auth.client import api_headers
from app.aws_smoke import run_smoke
from app.loadtest.seed import LoadSet


async def run(a: argparse.Namespace) -> int:
    alert_id = a.alert_id
    if alert_id is None:
        alert_id = LoadSet.load(f"loadtest/datasets/{a.name}.json").alert_ids[0]
    analyst_id = a.analyst_id or (None if os.environ.get("API_TOKEN") else "smoke-test")
    print(f"Smoke test: alert {alert_id} on {a.api_url}")
    async with httpx.AsyncClient(
        base_url=a.api_url, timeout=30, headers=api_headers()
    ) as client:
        res = await run_smoke(client, alert_id, analyst_id, a.timeout)
    for step in res.steps:
        print(" -", step)
    if res.case:
        print(json.dumps(res.case, indent=2, default=str)[:2000])
    print("RESULT:", "ok" if res.ok else f"FAILED ({res.error})")
    return 0 if res.ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-url", required=True)
    p.add_argument("--alert-id", type=int, default=None)
    p.add_argument(
        "--name", default="load-1000", help="manifest used when no --alert-id"
    )
    p.add_argument(
        "--analyst-id",
        default=None,
        help="default: smoke-test, or the token user when API_TOKEN is set",
    )
    p.add_argument("--timeout", type=float, default=300)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
