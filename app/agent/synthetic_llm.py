"""Deterministic stand-in LLM for load tests (F16).

Plugs into the F12 gateway as a Provider, so the rate limiter, cost recording, retries
and budget all run exactly as for real calls; only the network round-trip to OpenAI is
replaced by a sleep. Answers are schema-valid and seeded by alert id: a share of alerts
escalate, the rest close, and every cited evidence id is read from the prompt, so the
grounding check passes. Not the scripted `FakeLLM` in `app/agent/llm.py` (unit tests).
"""

import asyncio
import json
import random
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.agent.llm import Message, T, Usage
from app.agent.schemas import (
    InvestigationPlan,
    PlannedCheck,
    SarDraft,
    Typology,
    Verdict,
)

MODEL = "synthetic-llm"
_ALERT_ID = re.compile(r'"alert_id":\s*(\d+)')
_EVIDENCE_ID = re.compile(r'"evidence_id":\s*"([a-z_]+:[0-9a-f]{10})"')
_PLAN_TOOLS = (
    "query_transactions",
    "counterparty_network",
    "sanctions_screen",
    "peer_comparison",
)
_TYPOLOGIES: tuple[Typology, ...] = (
    "FAN-OUT",
    "FAN-IN",
    "CYCLE",
    "SCATTER-GATHER",
    "STACK",
)


class SyntheticError(ConnectionError):
    """Injected transient failure (the gateway retries these)."""


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class SyntheticLLM:
    name = "synthetic"
    model = MODEL

    def __init__(
        self,
        latency_ms: float = 1500.0,
        jitter_ms: float = 0.0,
        error_rate: float = 0.0,
        escalate_pct: int = 30,
        seed: int = 42,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._latency = latency_ms / 1000
        self._jitter = jitter_ms / 1000
        self._error_rate = error_rate
        self._escalate_pct = escalate_pct
        self._seed = seed
        self._sleep = sleep
        self._calls: dict[tuple[int, str], int] = defaultdict(int)

    async def structured_with_usage(
        self, schema: type[T], messages: Sequence[Message]
    ) -> tuple[T, Usage]:
        text = "\n".join(content for _, content in messages)
        alert_id = self._alert_id(text)
        n = self._calls[(alert_id, schema.__name__)]
        self._calls[(alert_id, schema.__name__)] = n + 1
        rng = random.Random(f"{self._seed}:{alert_id}:{schema.__name__}:{n}")
        delay = self._latency + (rng.random() * self._jitter if self._jitter else 0)
        if delay > 0:
            await self._sleep(delay)
        if self._error_rate > 0 and rng.random() < self._error_rate:
            raise SyntheticError("injected transient LLM failure")
        answer = self._answer(schema, alert_id, text)
        out = json.dumps(answer.model_dump(mode="json"))
        return answer, Usage(_tokens(text), _tokens(out))  # type: ignore[return-value]

    @staticmethod
    def _alert_id(text: str) -> int:
        m = _ALERT_ID.search(text)
        return int(m.group(1)) if m else 0

    def _escalates(self, alert_id: int) -> bool:
        return random.Random(f"{self._seed}:{alert_id}:disp").randrange(100) < (
            self._escalate_pct
        )

    def _answer(self, schema: type[Any], alert_id: int, text: str) -> Any:
        if schema is InvestigationPlan:
            return InvestigationPlan(
                checks=[
                    PlannedCheck(tool=t, reason="synthetic load-test check")
                    for t in _PLAN_TOOLS
                ]
            )
        ids = list(dict.fromkeys(_EVIDENCE_ID.findall(text)))
        if schema is Verdict:
            esc = self._escalates(alert_id)
            return Verdict(
                disposition="escalate" if esc else "close",
                typology=(_TYPOLOGIES[alert_id % len(_TYPOLOGIES)] if esc else None),
                risk_score=0.85 if esc else 0.1,
                confidence=0.9,
                evidence_ids=ids[:3],
                rationale="synthetic verdict (load test)",
            )
        if schema is SarDraft:
            return SarDraft(
                narrative=f"Synthetic SAR narrative for alert {alert_id}.",
                evidence_ids=ids[:3],
            )
        raise TypeError(f"SyntheticLLM cannot answer {schema.__name__}")
