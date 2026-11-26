"""F16 unit tests: the SyntheticLLM load-test provider. No network, no DB."""

import json

import pytest

from app.agent.gateway import (
    GatewayLLM,
    LLMUnavailable,
    MemoryRecorder,
    Policy,
    build_gateway,
    case_scope,
)
from app.agent.pricing import cost_usd
from app.agent.routing import check_grounding
from app.agent.schemas import InvestigationPlan, SarDraft, Verdict
from app.agent.synthetic_llm import MODEL, SyntheticError, SyntheticLLM
from app.config import Settings

EVIDENCE = [
    {"evidence_id": "query_transactions:aaaaaaaaaa", "tool": "x"},
    {"evidence_id": "peer_comparison:bbbbbbbbbb", "tool": "y"},
    {"evidence_id": "sanctions_screen:cccccccccc", "tool": "z"},
    {"evidence_id": "counterparty_network:dddddddddd", "tool": "w"},
]


def prompt(alert_id: int, with_evidence: bool = True) -> list[tuple[str, str]]:
    text = f'ALERT:\n{{"alert_id": {alert_id}, "account_id": 5}}\n'
    if with_evidence:
        text += "EVIDENCE:\n" + json.dumps(EVIDENCE, indent=2)
    return [("system", "sys"), ("user", text)]


async def no_sleep(_: float) -> None:
    return None


def llm(**kw: object) -> SyntheticLLM:
    return SyntheticLLM(sleep=no_sleep, **kw)  # type: ignore[arg-type]


async def test_plan_is_schema_valid_with_known_tools() -> None:
    plan, usage = await llm().structured_with_usage(
        InvestigationPlan, prompt(1, with_evidence=False)
    )
    assert {c.tool for c in plan.checks} <= {
        "query_transactions",
        "counterparty_network",
        "sanctions_screen",
        "adverse_media",
        "peer_comparison",
    }
    assert usage.input_tokens > 0 and usage.output_tokens > 0


async def test_verdict_cites_only_ids_in_prompt() -> None:
    verdict, _ = await llm().structured_with_usage(Verdict, prompt(7))
    known = {e["evidence_id"] for e in EVIDENCE}
    assert verdict.evidence_ids and set(verdict.evidence_ids) <= known
    assert check_grounding(verdict, EVIDENCE) == []


async def test_verdict_is_deterministic_per_alert_and_seed() -> None:
    a, _ = await llm(seed=1).structured_with_usage(Verdict, prompt(42))
    b, _ = await llm(seed=1).structured_with_usage(Verdict, prompt(42))
    assert a == b


async def test_escalation_share_follows_setting() -> None:
    async def share(pct: int) -> float:
        m = llm(escalate_pct=pct)
        out = [
            (await m.structured_with_usage(Verdict, prompt(i)))[0].disposition
            for i in range(1, 401)
        ]
        return out.count("escalate") / len(out)

    assert await share(0) == 0.0
    assert await share(100) == 1.0
    assert 0.2 < await share(30) < 0.4


async def test_close_has_no_typology_escalate_has() -> None:
    m = llm(escalate_pct=100)
    v, _ = await m.structured_with_usage(Verdict, prompt(3))
    assert v.disposition == "escalate" and v.typology
    c, _ = await llm(escalate_pct=0).structured_with_usage(Verdict, prompt(3))
    assert c.disposition == "close" and c.typology is None and c.risk_score <= 0.3


async def test_sar_draft_cites_evidence() -> None:
    sar, _ = await llm().structured_with_usage(SarDraft, prompt(9))
    assert sar.narrative and sar.evidence_ids


async def test_latency_and_jitter_are_slept() -> None:
    slept: list[float] = []

    async def rec(s: float) -> None:
        slept.append(s)

    m = SyntheticLLM(latency_ms=1500, jitter_ms=500, sleep=rec)
    await m.structured_with_usage(Verdict, prompt(1))
    assert 1.5 <= slept[0] <= 2.0


async def test_error_rate_extremes() -> None:
    with pytest.raises(SyntheticError):
        await llm(error_rate=1.0).structured_with_usage(Verdict, prompt(1))
    await llm(error_rate=0.0).structured_with_usage(Verdict, prompt(1))


async def test_gateway_retries_injected_errors_then_gives_up() -> None:
    async def fast(_: float) -> None:
        return None

    gw = GatewayLLM(
        "assess",
        llm(error_rate=1.0),
        policy=Policy(max_retries=1),
        sleep=fast,
    )
    with pytest.raises(LLMUnavailable):
        await gw.structured(Verdict, prompt(1))


async def test_gateway_records_synthetic_usage_with_cost() -> None:
    rec = MemoryRecorder()
    gw = GatewayLLM("assess", llm(), recorder=rec)
    with case_scope(1):
        await gw.structured(Verdict, prompt(1))
    assert rec.total_cost > 0
    assert cost_usd(MODEL, 1000, 1000) > 0


def test_build_gateway_uses_synthetic_provider_when_fake() -> None:
    s = Settings(postgres_password="x", llm_provider="fake")  # type: ignore[call-arg]
    gw = build_gateway(s, "plan", "gpt-4o-mini", "gpt-4o")
    provider, is_fallback = gw._providers[0]
    assert isinstance(provider, SyntheticLLM) and not is_fallback
    assert len(gw._providers) == 1


def test_build_gateway_default_is_openai() -> None:
    s = Settings(postgres_password="x", openai_api_key="k")  # type: ignore[call-arg]
    gw = build_gateway(s, "plan", "gpt-4o-mini", "gpt-4o")
    assert gw._providers[0][0].name == "openai"
