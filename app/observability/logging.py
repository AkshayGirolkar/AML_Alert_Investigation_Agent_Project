"""Structured logging (F13): JSON lines carrying the case being worked on.

`bind_case` / `bind_request` set context variables; every record emitted inside the
block (including from asyncio tasks started inside it) gets those ids.
"""

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

_case_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "log_case_id", default=None
)
_alert_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "log_alert_id", default=None
)
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "log_request_id", default=None
)


@contextmanager
def bind_case(case_id: int, alert_id: int | None = None) -> Iterator[None]:
    t1, t2 = _case_id.set(case_id), _alert_id.set(alert_id)
    try:
        yield
    finally:
        _case_id.reset(t1)
        _alert_id.reset(t2)


@contextmanager
def bind_request(request_id: str) -> Iterator[None]:
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.case_id = _case_id.get()
        record.alert_id = _alert_id.get()
        record.request_id = _request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("case_id", "alert_id", "request_id"):
            if (value := getattr(record, key, None)) is not None:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(fmt: str = "text", level: str = "INFO") -> None:
    """Replace the root handlers. fmt is 'json' or 'text'."""
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT)
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
