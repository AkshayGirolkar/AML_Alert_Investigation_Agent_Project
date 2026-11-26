"""FastAPI app (F07).  uvicorn app.main:app

`create_app` accepts a store and queue so tests can run without Postgres or SQS.
"""

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.routes import router
from app.config import get_settings
from app.db.cases import CaseStore, SqlCaseStore
from app.messaging.sqs import InvestigationQueue, open_queue
from app.observability.logging import bind_request, configure_logging
from app.observability.metrics import metrics
from app.redis_client import make_redis


def create_app(
    store: CaseStore | None = None,
    queue: InvestigationQueue | None = None,
    redis: Any = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if redis is None:
                settings = get_settings()
                client = make_redis(settings)
                stack.push_async_callback(client.aclose)
                app.state.redis = client
            if store is None or queue is None:
                settings = get_settings()
                engine = create_async_engine(settings.async_database_url)
                stack.push_async_callback(engine.dispose)
                app.state.engine = engine
                app.state.store = SqlCaseStore(engine)
                app.state.queue = await stack.enter_async_context(open_queue(settings))
            yield

    app = FastAPI(title="AML Alert Investigation Agent", lifespan=lifespan)

    @app.middleware("http")
    async def observe_request(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.monotonic()
        with bind_request(request_id):
            response: Response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")  # template, not the raw id
        metrics.inc(
            "http_requests_total",
            method=request.method,
            path=path,
            status=str(response.status_code),
        )
        metrics.observe("http_request_seconds", time.monotonic() - started, path=path)
        response.headers["x-request-id"] = request_id
        return response

    app.state.redis = redis
    if store is not None and queue is not None:  # injected (tests): no lifespan needed
        app.state.store, app.state.queue = store, queue
    app.include_router(router)
    return app


def _init_logging() -> None:
    settings = get_settings()
    configure_logging(settings.log_format, settings.log_level)


_init_logging()
app = create_app()
