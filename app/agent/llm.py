"""Minimal LLM interface for the graph: one structured call.

`OpenAILLM` wraps LangChain's ChatOpenAI; `FakeLLM` replays scripted answers in tests.
Timeouts, retries, fallback and cost tracking live in `app/agent/gateway.py` (F12).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Message = tuple[str, str]  # (role, content), role in {"system", "user"}


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    async def structured(self, schema: type[T], messages: Sequence[Message]) -> T: ...


class OpenAILLM:
    name = "openai"

    def __init__(self, model: str, api_key: str, temperature: float = 0.0) -> None:
        from langchain_openai import ChatOpenAI

        self.model = model
        # Retries and timeouts are the gateway's job, so the client must not hide them.
        self._chat = ChatOpenAI(
            model=model,
            api_key=api_key,  # type: ignore[arg-type]
            temperature=temperature,
            max_retries=0,
        )

    async def structured_with_usage(
        self, schema: type[T], messages: Sequence[Message]
    ) -> tuple[T, Usage]:
        # function_calling, not OpenAI's strict json_schema mode: the plan's free-form
        # `args` dict is rejected there. We validate the result with pydantic below.
        runnable = self._chat.with_structured_output(
            schema, method="function_calling", include_raw=True
        )
        out = await runnable.ainvoke(list(messages))
        if out.get("parsing_error") is not None:
            raise out["parsing_error"]
        parsed = out["parsed"]
        result = parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
        meta = getattr(out.get("raw"), "usage_metadata", None) or {}
        return result, Usage(meta.get("input_tokens", 0), meta.get("output_tokens", 0))

    async def structured(self, schema: type[T], messages: Sequence[Message]) -> T:
        result, _ = await self.structured_with_usage(schema, messages)
        return result


class Limiter(Protocol):
    async def acquire(self, cost: int = 1) -> None: ...


class RateLimitedLLM:
    """Waits for a shared (F10, Redis) rate-limit token before each call."""

    def __init__(self, inner: LLM, limiter: Limiter) -> None:
        self._inner = inner
        self._limiter = limiter

    async def structured(self, schema: type[T], messages: Sequence[Message]) -> T:
        await self._limiter.acquire()
        return await self._inner.structured(schema, messages)


class FakeLLM:
    """Returns queued answers per schema; records every call. For tests."""

    def __init__(self, *answers: BaseModel | Exception) -> None:
        self._queue: list[BaseModel | Exception] = list(answers)
        self.calls: list[tuple[str, Sequence[Message]]] = []

    async def structured(self, schema: type[T], messages: Sequence[Message]) -> T:
        self.calls.append((schema.__name__, messages))
        if not self._queue:
            raise AssertionError(f"FakeLLM has no answer left for {schema.__name__}")
        answer = self._queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if not isinstance(answer, schema):
            raise TypeError(
                f"FakeLLM expected {schema.__name__}, queued {type(answer).__name__}"
            )
        return answer
