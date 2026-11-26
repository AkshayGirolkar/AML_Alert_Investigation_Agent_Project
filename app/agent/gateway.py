"""LLM gateway (F12): timeout, retry with backoff, provider fallback, usage and cost.

`GatewayLLM` implements the same one-method `LLM` interface the graph already uses.
Per-case attribution comes from a context variable set by the worker (`case_scope`), so
no graph node signature changes. Without a scope (unit tests) nothing is recorded.
"""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.agent.llm import Limiter, Message, Usage
from app.agent.pricing import cost_usd
from app.config import Settings
from app.observability.metrics import metrics

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(Exception):
    """Every provider failed after retries. Retryable at the queue level."""


class CostBudgetExceeded(Exception):
    """The case has spent its LLM budget; the graph sends it to a human instead."""


class Provider(Protocol):
    name: str
    model: str

    async def structured_with_usage(
        self, schema: type[T], messages: Sequence[Message]
    ) -> tuple[T, Usage]: ...


@dataclass(frozen=True)
class CallRecord:
    case_id: int
    node: str
    call_seq: int
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    attempt: int
    fallback: bool
    error: str | None


class UsageRecorder(Protocol):
    async def record(self, rec: CallRecord) -> None: ...

    async def load(self, case_id: int) -> tuple[float, int]:
        """(USD already spent on the case, next free call_seq)."""
        ...


@dataclass
class MemoryRecorder:
    """In-process recorder for the CLI demo and tests."""

    records: list[CallRecord] = field(default_factory=list)

    async def record(self, rec: CallRecord) -> None:
        self.records.append(rec)

    async def load(self, case_id: int) -> tuple[float, int]:
        mine = [r for r in self.records if r.case_id == case_id]
        return sum(r.cost_usd for r in mine), max(
            (r.call_seq for r in mine), default=0
        ) + 1

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.records)


@dataclass
class CaseContext:
    case_id: int
    seq: int | None = None  # None until loaded from the recorder
    spent: float = 0.0


_current: ContextVar[CaseContext | None] = ContextVar("llm_case", default=None)


@contextlib.contextmanager
def case_scope(case_id: int) -> Iterator[CaseContext]:
    ctx = CaseContext(case_id)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@dataclass(frozen=True)
class Policy:
    timeout_seconds: float = 60.0
    max_retries: int = 2  # retries per provider, so up to max_retries + 1 attempts
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 20.0
    case_budget_usd: float = 0.0  # 0 = no cap


def is_transient(exc: BaseException) -> bool:
    """Worth retrying: timeouts, connection problems, 408/409/429 and 5xx.

    Matched on shape (status_code / class name) so any provider's exceptions work.
    Auth errors (401/403), bad requests and schema-validation errors are not transient.
    """
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 409, 429) or status >= 500
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}


class GatewayLLM:
    def __init__(
        self,
        node: str,
        primary: Provider,
        fallback: Provider | None = None,
        policy: Policy | None = None,
        recorder: UsageRecorder | None = None,
        limiter: Limiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._node = node
        self._providers: list[tuple[Provider, bool]] = [(primary, False)]
        if fallback is not None:
            self._providers.append((fallback, True))
        self._policy = policy or Policy()
        self._recorder = recorder
        self._limiter = limiter
        self._sleep = sleep
        self._rng = rng

    async def structured(self, schema: type[T], messages: Sequence[Message]) -> T:
        ctx = _current.get()
        if ctx is not None:
            await self._load(ctx)
            budget = self._policy.case_budget_usd
            if budget > 0 and ctx.spent >= budget:
                raise CostBudgetExceeded(
                    f"case {ctx.case_id} spent ${ctx.spent:.4f} of ${budget:.4f}"
                )
        last: Exception | None = None
        for provider, is_fallback in self._providers:
            for attempt in range(1, self._policy.max_retries + 2):
                if self._limiter is not None:
                    await self._limiter.acquire()  # one token per real provider call
                started = time.monotonic()
                try:
                    result, usage = await asyncio.wait_for(
                        provider.structured_with_usage(schema, messages),
                        self._policy.timeout_seconds,
                    )
                except Exception as exc:
                    await self._record(
                        ctx, provider, Usage(), started, attempt, is_fallback, repr(exc)
                    )
                    if not is_transient(exc):
                        raise
                    last = exc
                    log.warning(
                        "%s %s attempt %d failed: %r",
                        provider.name,
                        self._node,
                        attempt,
                        exc,
                    )
                    if attempt <= self._policy.max_retries:
                        await self._sleep(self._backoff(attempt))
                    continue
                await self._record(
                    ctx, provider, usage, started, attempt, is_fallback, None
                )
                return result
        raise LLMUnavailable(
            f"all providers failed for {self._node}: {last!r}"
        ) from last

    def _backoff(self, attempt: int) -> float:
        cap = min(
            self._policy.backoff_max_seconds,
            self._policy.backoff_base_seconds * 2 ** (attempt - 1),
        )
        return cap / 2 + self._rng() * cap / 2  # jittered, never zero

    async def _load(self, ctx: CaseContext) -> None:
        if ctx.seq is not None:
            return
        ctx.seq = 1
        if self._recorder is not None:
            try:
                ctx.spent, ctx.seq = await self._recorder.load(ctx.case_id)
            except Exception:
                log.exception("could not load LLM usage for case %s", ctx.case_id)

    async def _record(
        self,
        ctx: CaseContext | None,
        provider: Provider,
        usage: Usage,
        started: float,
        attempt: int,
        fallback: bool,
        error: str | None,
    ) -> None:
        if ctx is None:
            return
        cost = cost_usd(provider.model, usage.input_tokens, usage.output_tokens)
        lbl = {"node": self._node, "model": provider.model}
        metrics.observe("llm_call_seconds", time.monotonic() - started, **lbl)
        metrics.inc("llm_calls_total", error="yes" if error else "no", **lbl)
        metrics.inc("llm_cost_usd_total", cost, **lbl)
        metrics.inc("llm_tokens_total", usage.input_tokens, kind="input", **lbl)
        metrics.inc("llm_tokens_total", usage.output_tokens, kind="output", **lbl)
        ctx.spent += cost
        seq = ctx.seq or 1
        ctx.seq = seq + 1
        if self._recorder is None:
            return
        rec = CallRecord(
            ctx.case_id,
            self._node,
            seq,
            provider.name,
            provider.model,
            usage.input_tokens,
            usage.output_tokens,
            cost,
            int((time.monotonic() - started) * 1000),
            attempt,
            fallback,
            error,
        )
        try:
            await self._recorder.record(rec)
        except Exception:  # accounting must never fail an investigation
            log.exception("could not record LLM usage for case %s", ctx.case_id)


def build_gateway(
    settings: Settings,
    node: str,
    model: str,
    fallback_model: str,
    recorder: UsageRecorder | None = None,
    limiter: Limiter | None = None,
) -> GatewayLLM:
    """OpenAI primary; a second OpenAI model (same key) as fallback when it differs.

    `llm_provider=fake` (F16 load tests) uses the SyntheticLLM instead, no fallback.
    """
    from app.agent.llm import OpenAILLM

    primary: Provider
    fallback: Provider | None
    if settings.llm_provider == "fake":
        from app.agent.synthetic_llm import SyntheticLLM

        primary = SyntheticLLM(
            settings.fake_llm_latency_ms,
            settings.fake_llm_jitter_ms,
            settings.fake_llm_error_rate,
            settings.fake_llm_escalate_pct,
            settings.seed,
        )
        fallback = None
    else:
        key = settings.openai_api_key
        primary = OpenAILLM(model, key)
        fallback = OpenAILLM(fallback_model, key) if fallback_model != model else None
    return GatewayLLM(
        node,
        primary,
        fallback,
        Policy(
            settings.llm_timeout_seconds,
            settings.llm_max_retries,
            settings.llm_backoff_base_seconds,
            settings.llm_backoff_max_seconds,
            settings.llm_case_budget_usd,
        ),
        recorder,
        limiter,
    )
