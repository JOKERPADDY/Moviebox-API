import os
import sqlite3
import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

import state
import storage
import providers
import catalog_worker
from config import (
    API_VERSION, PORT, REQUEST_TIMEOUT, CATALOG_DB, STATIC_DIR,
)
from dashboard import DASHBOARD_HTML
from routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0),
        limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
        follow_redirects=True,
    )
    state.write_conn = sqlite3.connect(CATALOG_DB, check_same_thread=False)
    state.write_conn.execute("PRAGMA journal_mode=WAL")
    state.write_conn.execute("PRAGMA synchronous=NORMAL")
    storage.init_schema()
    state.background_task = asyncio.create_task(catalog_worker.maintenance_loop())
    state.probe_task = asyncio.create_task(providers.probe_loop())
    try:
        yield
    finally:
        for t in (state.background_task, state.probe_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        if state.client is not None:
            await state.client.aclose()
            state.client = None
        if state.write_conn is not None:
            state.write_conn.close()
            state.write_conn = None


app = FastAPI(
    title="MovieBox API Pro",
    description="Full Pure REST API for moviebox.ph — Zero Scraping",
    version=API_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/app", response_class=HTMLResponse)
async def frontend_app():
    path = os.path.join(STATIC_DIR, "app.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>app.html not found in static/</h1>", status_code=404)


if __name__ == "__main__":
    import uvicorn
    reload_flag = os.environ.get("RELOAD", "0") == "1"
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=reload_flag)