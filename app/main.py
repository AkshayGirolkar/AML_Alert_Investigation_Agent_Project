"""FastAPI app (F07).  uvicorn app.main:app

`create_app` accepts a store and queue so tests can run without Postgres or SQS.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.routes import router
from app.config import get_settings
from app.db.cases import CaseStore, SqlCaseStore
from app.messaging.sqs import InvestigationQueue, open_queue


def create_app(
    store: CaseStore | None = None, queue: InvestigationQueue | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if store is None or queue is None:
                settings = get_settings()
                engine = create_async_engine(settings.async_database_url)
                stack.push_async_callback(engine.dispose)
                app.state.engine = engine
                app.state.store = SqlCaseStore(engine)
                app.state.queue = await stack.enter_async_context(open_queue(settings))
            yield

    app = FastAPI(title="AML Alert Investigation Agent", lifespan=lifespan)
    if store is not None and queue is not None:  # injected (tests): no lifespan needed
        app.state.store, app.state.queue = store, queue
    app.include_router(router)
    return app


app = create_app()
