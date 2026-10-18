import re
import json
import time
import asyncio
import httpx
from fastapi import HTTPException
from config import (
    API_BASE, DEFAULT_HOST, DEFAULT_HEADERS, PLAYER_HEADERS,
    DOMAIN_TTL, RETRY_BACKOFF, PROVIDER_PROBE_TIMEOUT,
)
import state
import providers


def _add_host_param(path: str, host: str) -> str:
    if "?" in path:
        base, query = path.split("?", 1)
        params = [p for p in query.split("&") if p and not p.startswith("host=")]
        if params:
            return f"{base}?{'&'.join(params)}&host={host}"
        return f"{base}?host={host}"
    return f"{path}?host={host}"


async def get_bearer_token(host: str) -> str:
    cached = state.bearer_tokens.get(host)
    if cached:
        return cached[0]
    async with state.bearer_lock:
        cached = state.bearer_tokens.get(host)
        if cached:
            return cached[0]
        client = state.require_client()
        token = None
        try:
            resp = await client.get(
                f"{API_BASE}/home?host={host}",
                headers=DEFAULT_HEADERS,
                timeout=PROVIDER_PROBE_TIMEOUT,
            )
            x_user = resp.headers.get("x-user")
            if x_user:
                try:
                    token = json.loads(x_user).get("token")
                except json.JSONDecodeError:
                    token = None
            if not token:
                cookie = resp.headers.get("set-cookie", "")
                m = re.search(r"token=([^;]+)", cookie)
                if m:
                    token = m.group(1)
            if token:
                state.bearer_tokens[host] = (token, time.time())
        except Exception:
            pass
        return token or ""


async def bff_request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    preferred_host: str | None = None,
    strict_host: bool = False,
) -> dict:
    candidates = providers.ordered_candidates(preferred_host, strict_host)
    if not candidates:
        raise HTTPException(503, detail="No providers available")
    client = state.require_client()
    last_error: str | None = None

    for host in candidates:
        token = await get_bearer_token(host)
        headers = {**DEFAULT_HEADERS}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{API_BASE}{_add_host_param(path, host)}"
        start = time.time()

        try:
            if method == "POST":
                resp = await client.post(url, headers=headers, json=payload)
            else:
                resp = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            providers.mark_failure(host, str(e))
            last_error = f"{host}: {type(e).__name__}"
            if strict_host and host == preferred_host:
                break
            continue

        elapsed_ms = (time.time() - start) * 1000

        x_user = resp.headers.get("x-user")
        if x_user:
            try:
                new_token = json.loads(x_user).get("token")
                if new_token:
                    state.bearer_tokens[host] = (new_token, time.time())
                    headers["Authorization"] = f"Bearer {new_token}"
            except json.JSONDecodeError:
                pass

        if resp.status_code == 401:
            state.bearer_tokens.pop(host, None)
            new_token = await get_bearer_token(host)
            if new_token:
                headers["Authorization"] = f"Bearer {new_token}"
                try:
                    if method == "POST":
                        resp = await client.post(url, headers=headers, json=payload)
                    else:
                        resp = await client.get(url, headers=headers)
                    elapsed_ms = (time.time() - start) * 1000
                except Exception as e:
                    providers.mark_failure(host, f"401 retry: {e}")
                    last_error = f"{host}: 401 retry failed"
                    if strict_host and host == preferred_host:
                        break
                    continue

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else RETRY_BACKOFF
            await asyncio.sleep(min(delay, 5))
            try:
                if method == "POST":
                    resp = await client.post(url, headers=headers, json=payload)
                else:
                    resp = await client.get(url, headers=headers)
                elapsed_ms = (time.time() - start) * 1000
            except Exception as e:
                providers.mark_failure(host, f"429 retry: {e}")
                last_error = f"{host}: 429 retry failed"
                if strict_host and host == preferred_host:
                    break
                continue

        if resp.status_code != 200:
            providers.mark_failure(host, f"HTTP {resp.status_code}")
            last_error = f"{host}: HTTP {resp.status_code}"
            if strict_host and host == preferred_host:
                break
            continue

        try:
            data = resp.json()
        except json.JSONDecodeError:
            providers.mark_failure(host, "non-JSON response")
            last_error = f"{host}: non-JSON"
            if strict_host and host == preferred_host:
                break
            continue

        providers.mark_success(host, elapsed_ms)
        return data

    raise HTTPException(
        status_code=502,
        detail=f"All {len(candidates)} providers failed. Last: {last_error}",
    )


async def get_player_domain(force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh and state.domain_cache["value"] and now < state.domain_cache["expires"]:
        return state.domain_cache["value"]
    data = await bff_request("/media-player/get-domain")
    domain = (data.get("data") or "https://netfilm.world").rstrip("/")
    state.domain_cache["value"] = domain
    state.domain_cache["expires"] = now + DOMAIN_TTL
    return domain


def build_player_referer(domain: str, detail_path: str, subject_id: str, se: int, ep: int) -> str:
    return (
        f"{domain}/spa/videoPlayPage/movies/{detail_path}"
        f"?id={subject_id}&type=/movie/detail&detailSe={se}&detailEp={ep}&lang=en"
    )


async def resolve_stream(subject_id: str, detail_path: str, se: int, ep: int) -> dict:
    client = state.require_client()
    last_error = "unknown"
    for attempt in range(2):
        try:
            domain = await get_player_domain(force_refresh=(attempt > 0))
            referer = build_player_referer(domain, detail_path, subject_id, se, ep)
            play_url = (
                f"{domain}/wefeed-h5api-bff/subject/play"
                f"?subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}"
            )
            resp = await client.get(play_url, headers={**PLAYER_HEADERS, "Referer": referer})
            if resp.status_code != 200:
                last_error = f"player HTTP {resp.status_code}"
                if attempt == 0:
                    continue
                raise HTTPException(502, last_error)
            try:
                data = resp.json().get("data", {}) or {}
            except Exception:
                last_error = "player non-JSON"
                if attempt == 0:
                    continue
                raise HTTPException(502, last_error)
            return {"domain": domain, "referer": referer, "data": data}
        except HTTPException:
            raise
        except Exception as e:
            last_error = str(e)
            if attempt == 0:
                continue
    raise HTTPException(502, f"Stream resolution failed: {last_error}")


async def resolve_captions(subject_id: str, detail_path: str, se: int, ep: int) -> list:
    stream = await resolve_stream(subject_id, detail_path, se, ep)
    data = stream["data"]
    streams = data.get("streams") or []
    dash = data.get("dash") or []

    stream_id = None
    stream_format = None
    if streams:
        stream_id = streams[0].get("id")
        stream_format = streams[0].get("format", "MP4")
    elif dash:
        stream_id = dash[0].get("id")
        stream_format = dash[0].get("format", "DASH")

    if not stream_id:
        return []

    cap_path = (
        f"/subject/caption?format={stream_format}&id={stream_id}"
        f"&subjectId={subject_id}&detailPath={detail_path}"
    )
    data = await bff_request(cap_path)
    inner = data.get("data", {}) or {}
    return inner.get("captions", []) if isinstance(inner, dict) else inner