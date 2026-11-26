"""F13 unit tests: JSON logs, PII masker, tracing setup, metrics, API endpoints."""

import io
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.observability import tracing
from app.observability.logging import (
    ContextFilter,
    JsonFormatter,
    bind_case,
    bind_request,
)
from app.observability.metrics import Registry, render_gauges
from app.observability.pii import MASK, mask, mask_text
from app.observability.scrape import scrape

# ---------------------------------------------------------------- logging


def _logger() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("test.f13")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    return log, buf


def test_json_log_carries_case_and_request_ids() -> None:
    log, buf = _logger()
    with bind_request("req-1"), bind_case(7, 3):
        log.info("working on %s", "x")
    rec = json.loads(buf.getvalue())
    assert rec["msg"] == "working on x"
    assert (rec["case_id"], rec["alert_id"], rec["request_id"]) == (7, 3, "req-1")
    assert rec["level"] == "INFO" and "ts" in rec


def test_json_log_outside_case_has_no_case_id_and_binding_resets() -> None:
    log, buf = _logger()
    with bind_case(7, 3):
        pass
    log.info("after")
    rec = json.loads(buf.getvalue())
    assert "case_id" not in rec


def test_json_log_includes_exception() -> None:
    log, buf = _logger()
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed")
    assert "ValueError: boom" in json.loads(buf.getvalue())["exc"]


# -------------------------------------------------------------------- pii

SEEDED = {
    "customer": {
        "name": "Jane Roe",
        "email": "jane.roe@example.com",
        "phone": "+1 (415) 555-0132",
        "occupation": "Dentist",
    },
    "messages": [
        "Contact jane.roe@example.com or +44 20 7946 0958 about 800104D70.",
        "IBAN GB82WEST12345698765432 and SSN 123-45-6789",
    ],
    "evidence_ids": ["query_transactions:ab12cd34ef"],
    "amount": 9_500_000,
    "text_amount": "total 12345678 USD",
}


def test_mask_removes_seeded_pii() -> None:
    out = json.dumps(mask(SEEDED))
    for secret in (
        "Jane Roe",
        "jane.roe@example.com",
        "555-0132",
        "7946 0958",
        "800104D70",
        "GB82WEST12345698765432",
        "123-45-6789",
        "Dentist",
    ):
        assert secret not in out


def test_mask_keeps_evidence_ids_and_amounts() -> None:
    out = mask(SEEDED)
    assert out["evidence_ids"] == ["query_transactions:ab12cd34ef"]
    assert out["amount"] == 9_500_000
    assert "12345678" in out["text_amount"]


def test_mask_is_idempotent_and_does_not_mutate_input() -> None:
    before = json.dumps(SEEDED)
    once = mask(SEEDED)
    assert mask(once) == once
    assert json.dumps(SEEDED) == before


def test_mask_handles_odd_values() -> None:
    assert mask({"name": None, "x": [None, 1, 2.5, True]}) == {
        "name": None,
        "x": [None, 1, 2.5, True],
    }
    assert mask_text("") == ""
    assert mask({"api_key": "sk-abcdefghijklmnop"})["api_key"] == MASK
    assert "sk-abcdefghijklmnop" not in mask_text("key sk-abcdefghijklmnop")


# ---------------------------------------------------------------- tracing


def test_tracing_off_without_flag_or_key() -> None:
    assert not tracing.tracing_enabled({})
    assert not tracing.tracing_enabled({"LANGSMITH_TRACING": "true"})
    assert not tracing.tracing_enabled({"LANGSMITH_API_KEY": "k"})
    assert tracing.setup_tracing({}) is False  # no client, no network


def test_tracing_enabled_with_both() -> None:
    assert tracing.tracing_enabled(
        {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "k"}
    )


def test_tracing_client_masks_inputs_outputs_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langsmith.run_trees as rt

    monkeypatch.setattr(rt, "_CLIENT", None)
    env = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "k"}
    assert tracing.setup_tracing(env) is True
    client = rt.get_cached_client()
    payload = {"messages": ["mail jane.roe@example.com"], "customer": {"name": "Jane"}}
    for hook in (
        client._hide_inputs,
        client._hide_outputs,
        client._hide_metadata,
    ):
        masked = json.dumps(hook(payload))  # type: ignore[operator]
        assert "jane.roe" not in masked and "Jane" not in masked
    monkeypatch.setattr(rt, "_CLIENT", None)


def test_tracing_refuses_a_preexisting_unmasked_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langsmith
    import langsmith.run_trees as rt

    monkeypatch.setattr(rt, "_CLIENT", langsmith.Client(api_key="k"))
    env = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "k"}
    assert tracing.setup_tracing(env) is False


def test_case_trace_config() -> None:
    cfg = tracing.case_trace_config(5, 9, "alert-9")
    assert cfg["metadata"] == {"case_id": 5, "alert_id": 9, "thread_id": "alert-9"}
    assert "aml-investigation" in cfg["tags"]


# ---------------------------------------------------------------- metrics


def test_registry_counters_and_histograms_render() -> None:
    reg = Registry()
    reg.inc("cases_processed_total", result="completed")
    reg.inc("cases_processed_total", result="completed")
    reg.inc("llm_cost_usd_total", 0.25, model="m")
    reg.observe("case_seconds", 1.5, result="completed")
    out = reg.render()
    assert 'cases_processed_total{result="completed"} 2' in out
    assert 'llm_cost_usd_total{model="m"} 0.25' in out
    assert 'case_seconds_bucket{result="completed",le="2"} 1' in out
    assert 'case_seconds_bucket{result="completed",le="1"} 0' in out
    assert 'case_seconds_count{result="completed"} 1' in out
    assert "# TYPE case_seconds histogram" in out


def test_render_gauges() -> None:
    out = render_gauges([("queue_depth", {}, 3.0), ("cases", {"status": "x"}, 2.0)])
    assert "queue_depth 3" in out and 'cases{status="x"} 2' in out


async def test_scrape_tolerates_failing_sources() -> None:
    class BadEngine:
        def connect(self) -> Any:
            raise RuntimeError("db down")

    async def bad_depths() -> tuple[int, int]:
        raise RuntimeError("sqs down")

    assert isinstance(await scrape(BadEngine(), bad_depths), str)


async def test_scrape_includes_queue_depth() -> None:
    async def depths() -> tuple[int, int]:
        return 4, 1

    out = await scrape(None, depths)
    assert "queue_depth 4" in out and "dlq_depth 1" in out


# ------------------------------------------------------------------- API


class _Store:
    pass


class _Queue:
    async def depths(self) -> tuple[int, int]:
        return 2, 0

    async def send(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        pass


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from app.main import create_app

    app = create_app(store=_Store(), queue=_Queue())  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        yield c


async def test_metrics_endpoint_and_request_metrics(client: httpx.AsyncClient) -> None:
    await client.get("/health")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert 'http_requests_total{method="GET",path="/health",status="200"}' in r.text
    assert "queue_depth 2" in r.text


async def test_request_id_header(client: httpx.AsyncClient) -> None:
    r = await client.get("/health", headers={"x-request-id": "abc"})
    assert r.headers["x-request-id"] == "abc"
    assert (await client.get("/health")).headers["x-request-id"]


async def test_ready_ok_and_degraded(client: httpx.AsyncClient) -> None:
    r = await client.get("/ready")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    class BrokenRedis:
        async def ping(self) -> None:
            raise ConnectionError("down")

    from app.main import create_app

    app = create_app(store=_Store(), queue=_Queue(), redis=BrokenRedis())  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "degraded": ["redis"]}
