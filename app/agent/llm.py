"""Minimal LLM interface for the graph: one structured call.

`OpenAILLM` wraps LangChain's ChatOpenAI; `FakeLLM` replays scripted answers in tests.
Timeouts, retries and provider fallback belong to F12.
"""

from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Message = tuple[str, str]  # (role, content), role in {"system", "user"}


class LLM(Protocol):
    async def structured(self, schema: type[T], messages: Sequence[Message]) -> T: ...


class OpenAILLM:
    def __init__(self, model: str, api_key: str, temperature: float = 0.0) -> None:
        from langchain_openai import ChatOpenAI

        self._chat = ChatOpenAI(
            model=model,
            api_key=api_key,  # type: ignore[arg-type]
            temperature=temperature,
        )

    async def structured(self, schema: type[T], messages: Sequence[Message]) -> T:
        # function_calling, not OpenAI's strict json_schema mode: the plan's free-form
        # `args` dict is rejected there. We validate the result with pydantic below.
        runnable = self._chat.with_structured_output(schema, method="function_calling")
        result = await runnable.ainvoke(list(messages))
        if isinstance(result, schema):
            return result
        return schema.model_validate(result)


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
