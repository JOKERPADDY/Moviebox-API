import asyncio
import sqlite3
import httpx
from fastapi import HTTPException

client: httpx.AsyncClient | None = None
write_conn: sqlite3.Connection | None = None
bearer_tokens: dict = {}  # host -> (token, acquired_at)
bearer_lock = asyncio.Lock()
domain_cache: dict = {"value": None, "expires": 0.0}
walk_state: dict = {"running": False, "enriching": False}
background_task: asyncio.Task | None = None
probe_task: asyncio.Task | None = None


def require_client() -> httpx.AsyncClient:
    if client is None:
        raise HTTPException(status_code=503, detail="Server initializing, retry shortly")
    return client