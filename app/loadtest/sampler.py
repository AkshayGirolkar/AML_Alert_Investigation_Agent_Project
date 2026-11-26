"""Periodic resource samples during a load run (F16).

A probe is an async callable returning a flat dict. A failing probe records its error
and never stops the run. Concrete probes cover Postgres activity, SQS depth, Redis and
`docker stats` for the compose containers; each is optional.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg

from app.db.copy import conninfo

Probe = Callable[[], Awaitable[dict[str, Any]]]


class Sampler:
    def __init__(
        self,
        probes: dict[str, Probe],
        interval: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._probes = probes
        self._interval = interval
        self._clock = clock
        self.samples: list[dict[str, Any]] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def sample_once(self) -> dict[str, Any]:
        row: dict[str, Any] = {"t": self._clock()}
        for name, probe in self._probes.items():
            try:
                row[name] = await probe()
            except Exception as exc:  # noqa: BLE001  a broken probe must not end the run
                row[name] = {"error": repr(exc)}
        self.samples.append(row)
        return row

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.sample_once()
            try:
                await asyncio.wait_for(self._stop.wait(), self._interval)
            except TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> list[dict[str, Any]]:
        self._stop.set()
        if self._task is not None:
            await self._task
        return self.samples


def postgres_probe(database_url: str) -> Probe:
    sql = (
        "SELECT count(*) FILTER (WHERE state = 'active'),"
        " count(*) FILTER (WHERE state = 'idle'),"
        " count(*) FILTER (WHERE state = 'idle in transaction'),"
        " count(*) FILTER (WHERE wait_event_type = 'Lock'),"
        " count(*) FROM pg_stat_activity WHERE datname = current_database()"
    )

    def read() -> dict[str, Any]:
        with psycopg.connect(conninfo(database_url), connect_timeout=3) as conn:
            a, i, t, w, n = conn.execute(sql).fetchone() or (0, 0, 0, 0, 0)
        return {"active": a, "idle": i, "idle_in_tx": t, "lock_waits": w, "total": n}

    async def probe() -> dict[str, Any]:
        return await asyncio.to_thread(read)

    return probe


def sqs_probe(depths: Callable[[], Awaitable[tuple[int, int]]]) -> Probe:
    async def probe() -> dict[str, Any]:
        queue, dlq = await depths()
        return {"queue": queue, "dlq": dlq}

    return probe


def redis_probe(redis: Any) -> Probe:
    async def probe() -> dict[str, Any]:
        info = await redis.info()
        return {
            "ops_per_sec": info.get("instantaneous_ops_per_sec"),
            "clients": info.get("connected_clients"),
            "used_memory": info.get("used_memory"),
        }

    return probe


def _pct(s: str) -> float:
    return float(s.strip().rstrip("%") or 0)


def parse_docker_stats(output: str) -> dict[str, Any]:
    """`docker stats --no-stream --format '{{json .}}'` lines -> {container: cpu/mem}."""
    out: dict[str, Any] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out[d["Name"]] = {"cpu_pct": _pct(d["CPUPerc"]), "mem_pct": _pct(d["MemPerc"])}
    return out


def running_only(wanted: list[str], ps_output: str) -> list[str]:
    """Keep the wanted containers that `docker ps` lists (a missing one fails the stats)."""
    up = set(ps_output.split())
    return [c for c in wanted if c in up]


def docker_probe(containers: list[str]) -> Probe:
    async def probe() -> dict[str, Any]:
        ps = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "--format",
            "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        ps_out, _ = await ps.communicate()
        names = running_only(containers, ps_out.decode())
        if not names:
            return {}
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            *names,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return parse_docker_stats(stdout.decode())

    return probe
