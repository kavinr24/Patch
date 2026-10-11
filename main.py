import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

import core.tracker
from web import routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    core.tracker.on_fix_callback = routes.record_fix
    core.tracker.load_registry()
    poller_task = asyncio.create_task(core.tracker.poll_forever(30))
    try:
        yield
    finally:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Patch", lifespan=lifespan)
app.include_router(routes.router)
