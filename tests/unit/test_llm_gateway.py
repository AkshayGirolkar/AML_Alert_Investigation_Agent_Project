"""F12 unit tests: retry, fallback, timeout, budget, cost, recording. No network."""

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from app.agent.gateway import (
    CostBudgetExceeded,
    GatewayLLM,
    LLMUnavailable,
    MemoryRecorder,
    Policy,
    case_scope,
    is_transient,
)
from app.agent.llm import Limiter, Message, Usage
from app.agent.pricing import cost_usd


class Out(BaseModel):
    x: int


class HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class ScriptedProvider:
    """Plays a script: an Exception is raised, a Usage returns Out(x=1) with that usage."""

    def __init__(self, model: str, *script: Exception | Usage | float) -> None:
        self.name, self.model = "fake", model
        self._script = list(script)
        self.calls = 0

    async def structured_with_usage(
        self, schema: type[Any], messages: Sequence[Message]
    ) -> tuple[Any, Usage]:
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, float):  # sleep this long, then succeed
            await asyncio.sleep(step)
            return schema(x=1), Usage(1, 1)
        return schema(x=1), step


class CountingLimiter:
    def __init__(self) -> None:
        self.n = 0

    async def acquire(self, cost: int = 1) -> None:
        self.n += 1


def gateway(
    primary: ScriptedProvider,
    fallback: ScriptedProvider | None = None,
    recorder: MemoryRecorder | None = None,
    limiter: Limiter | None = None,
    **policy: Any,
) -> tuple[GatewayLLM, list[float]]:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    p = Policy(**{"timeout_seconds": 5, "max_retries": 2, **policy})
    return (
        GatewayLLM(
            "assess", primary, fallback, p, recorder, limiter, sleep, lambda: 1.0
        ),
        sleeps,
    )


MSG: list[Message] = [("user", "hi")]


async def test_transient_error_is_retried_with_growing_backoff() -> None:
    primary = ScriptedProvider("gpt-4o", HttpError(429), HttpError(503), Usage(10, 5))
    fb = ScriptedProvider("gpt-4o-mini")
    gw, sleeps = gateway(primary, fb, backoff_base_seconds=1, backoff_max_seconds=10)
    assert (await gw.structured(Out, MSG)).x == 1
    assert primary.calls == 3 and fb.calls == 0
    assert sleeps == [1.0, 2.0]  # rng=1.0 -> full cap: base * 2**(attempt-1)


async def test_persistent_failure_falls_back_and_is_recorded() -> None:
    primary = ScriptedProvider("gpt-4o", *[HttpError(500)] * 3)
    fb = ScriptedProvider("gpt-4o-mini", Usage(100, 50))
    rec = MemoryRecorder()
    gw, _ = gateway(primary, fb, rec)
    with case_scope(7):
        await gw.structured(Out, MSG)
    assert [(r.model, r.fallback, r.error is None) for r in rec.records] == [
        ("gpt-4o", False, False),
        ("gpt-4o", False, False),
        ("gpt-4o", False, False),
        ("gpt-4o-mini", True, True),
    ]
    assert [r.call_seq for r in rec.records] == [1, 2, 3, 4]
    assert [r.attempt for r in rec.records] == [1, 2, 3, 1]


async def test_all_providers_failing_raises_unavailable() -> None:
    gw, _ = gateway(
        ScriptedProvider("a", *[HttpError(503)] * 3),
        ScriptedProvider("b", *[HttpError(503)] * 3),
    )
    with pytest.raises(LLMUnavailable):
        await gw.structured(Out, MSG)


@pytest.mark.parametrize(
    "exc", [HttpError(401), HttpError(400), ValueError("bad schema")]
)
async def test_non_transient_errors_are_not_retried_or_fallen_back(
    exc: Exception,
) -> None:
    primary, fb = ScriptedProvider("a", exc), ScriptedProvider("b", Usage())
    gw, sleeps = gateway(primary, fb)
    with pytest.raises(type(exc)):
        await gw.structured(Out, MSG)
    assert primary.calls == 1 and fb.calls == 0 and sleeps == []


async def test_timeout_counts_as_transient_and_falls_back() -> None:
    primary = ScriptedProvider("a", 1.0)  # sleeps 1s, limit is 0.01s
    fb = ScriptedProvider("b", Usage())
    gw, _ = gateway(primary, fb, timeout_seconds=0.01, max_retries=0)
    assert (await gw.structured(Out, MSG)).x == 1
    assert primary.calls == 1 and fb.calls == 1


async def test_limiter_token_taken_for_every_provider_call() -> None:
    lim = CountingLimiter()
    gw, _ = gateway(
        ScriptedProvider("a", HttpError(500), Usage()), limiter=lim, max_retries=1
    )
    await gw.structured(Out, MSG)
    assert lim.n == 2


async def test_cost_is_computed_from_price_table_and_failures_cost_nothing() -> None:
    primary = ScriptedProvider("gpt-4o", HttpError(500), Usage(1_000_000, 1_000_000))
    rec = MemoryRecorder()
    gw, _ = gateway(primary, recorder=rec)
    with case_scope(1):
        await gw.structured(Out, MSG)
    assert [r.cost_usd for r in rec.records] == [0.0, 12.5]
    assert rec.records[0].error and rec.records[1].error is None


async def test_budget_blocks_the_next_call_and_resumes_from_recorded_spend() -> None:
    rec = MemoryRecorder()
    gw, _ = gateway(
        ScriptedProvider("gpt-4o", Usage(1_000_000, 0), Usage(1, 1)),
        recorder=rec,
        case_budget_usd=2.0,
    )
    with case_scope(1):
        await gw.structured(Out, MSG)  # spends $2.50
        with pytest.raises(CostBudgetExceeded):
            await gw.structured(Out, MSG)
    # a redelivered run of the same case sees the earlier spend and continues numbering
    gw2, _ = gateway(
        ScriptedProvider("gpt-4o", Usage()), recorder=rec, case_budget_usd=2.0
    )
    with case_scope(1), pytest.raises(CostBudgetExceeded):
        await gw2.structured(Out, MSG)
    gw3, _ = gateway(ScriptedProvider("gpt-4o", Usage()), recorder=rec)
    with case_scope(1):
        await gw3.structured(Out, MSG)
    assert [r.call_seq for r in rec.records] == [1, 2]


async def test_no_scope_means_no_recording_and_no_budget() -> None:
    rec = MemoryRecorder()
    gw, _ = gateway(
        ScriptedProvider("gpt-4o", Usage(5, 5)), recorder=rec, case_budget_usd=0.0001
    )
    await gw.structured(Out, MSG)
    assert rec.records == []


async def test_recorder_failure_does_not_fail_the_call() -> None:
    class Broken(MemoryRecorder):
        async def record(self, rec: Any) -> None:
            raise RuntimeError("db down")

    gw, _ = gateway(ScriptedProvider("gpt-4o", Usage()), recorder=Broken())
    with case_scope(1):
        assert (await gw.structured(Out, MSG)).x == 1


def test_is_transient() -> None:
    assert is_transient(TimeoutError()) and is_transient(ConnectionResetError())
    assert is_transient(HttpError(429)) and is_transient(HttpError(502))
    assert not is_transient(HttpError(401)) and not is_transient(ValueError())


def test_pricing() -> None:
    assert cost_usd("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert cost_usd("gpt-4o-2024-08-06", 1_000_000, 0) == pytest.approx(2.5)
    assert cost_usd("gpt-4o-mini-2024-07-18", 1_000_000, 0) == pytest.approx(0.15)
    assert cost_usd("unknown-model", 10, 10) == 0.0
