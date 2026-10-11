import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

import core.tracker
from web import routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Boot order matters: sessions file may (re)register repos, and legacy
    # global entries with no subscribers must be pruned BEFORE the poller
    # starts scanning -- otherwise an abandoned repo gets scanned forever.
    routes.startup_sync()
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
