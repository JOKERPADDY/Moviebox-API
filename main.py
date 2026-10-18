import os
import re
import json
import time
import sqlite3
import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

API_VERSION = "2.4.1"

PORT = int(os.environ.get("PORT", "8000"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "25"))
RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.environ.get("RETRY_BACKOFF", "0.5"))
DOMAIN_TTL = int(os.environ.get("DOMAIN_TTL", "300"))

TAB_MOVIES = int(os.environ.get("TAB_MOVIES", "2"))
TAB_TV = int(os.environ.get("TAB_TV", "5"))
TAB_ANIMATION = int(os.environ.get("TAB_ANIMATION", "8"))

CATALOG_DB = os.environ.get("CATALOG_DB", "catalog.db")
CATALOG_REFRESH_INTERVAL = int(os.environ.get("CATALOG_REFRESH_INTERVAL", "21600"))
CATALOG_KEYWORDS = list("abcdefghijklmnopqrstuvwxyz0123456789")
CATALOG_MAX_PAGES_PER_KEYWORD = int(os.environ.get("CATALOG_MAX_PAGES_PER_KEYWORD", "50"))
ENRICH_RATE_LIMIT = float(os.environ.get("ENRICH_RATE_LIMIT", "0.2"))
ENRICH_BATCH_SIZE = int(os.environ.get("ENRICH_BATCH_SIZE", "20"))

BASE_URL = "https://moviebox.ph"
API_BASE = "https://h5-api.aoneroom.com/wefeed-h5api-bff"

_client: httpx.AsyncClient | None = None
_bearer_token: str | None = None
_token_acquired_at: float = 0.0
_token_lock = asyncio.Lock()
_domain_cache: dict = {"value": None, "expires": 0.0}

_write_conn: sqlite3.Connection | None = None
_walk_state: dict = {"running": False, "enriching": False}
_background_task: asyncio.Task | None = None

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Referer": "https://moviebox.ph/",
    "Origin": "https://moviebox.ph",
    "X-Client-Info": '{"timezone":"Asia/Dhaka"}',
    "X-Request-Lang": "en",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
}

PLAYER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "X-Client-Info": '{"timezone":"Asia/Dhaka"}',
    "X-Source": "",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

CATALOG_SECTIONS = {
    "movies": ["Cinema", "Bollywood", "Hollywood", "South Indian"],
    "tv-series": ["Hot Short TV", "Indian Drama", "Western TV", "Best Asian Series"],
    "animation": ["Top Anime"],
}

MOVIE_SECTIONS = {"cinema", "bollywood", "hollywood", "south indian"}
TV_SECTIONS = {"hot short tv", "indian drama", "western tv", "best asian series"}
ANIME_SECTIONS = {"top anime"}

TAB_PAYLOAD_VARIANTS = {
    "baseline":       {"filter": {"sort": "RECOMMEND", "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}},
    "no_filter":      None,
    "empty_filter":   {"filter": {}},
    "sort_only":      {"filter": {"sort": "RECOMMEND"}},
    "sort_newest":    {"filter": {"sort": "NEWEST", "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}},
    "sort_hot":       {"filter": {"sort": "HOT", "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}},
    "sort_rating":    {"filter": {"sort": "RATING", "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}},
    "type_movie":     {"filter": {"sort": "RECOMMEND", "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL", "type": "MOVIE"}},
    "type_series":    {"filter": {"sort": "RECOMMEND", "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL", "type": "TV_SERIES"}},
}

# ─── CATALOG STORAGE ──────────────────────────────────────────────────────────

def _init_schema():
    _write_conn.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            subject_id TEXT PRIMARY KEY,
            title TEXT,
            poster_url TEXT,
            slug TEXT,
            rating TEXT,
            year TEXT,
            badge TEXT,
            is_movie INTEGER DEFAULT 0,
            is_tv INTEGER DEFAULT 0,
            is_anime INTEGER DEFAULT 0,
            subject_type INTEGER,
            genre TEXT,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        )
    """)
    _write_conn.execute("""
        CREATE TABLE IF NOT EXISTS keyword_stats (
            keyword TEXT PRIMARY KEY,
            pages_walked INTEGER DEFAULT 0,
            items_seen INTEGER DEFAULT 0,
            hit_cap INTEGER DEFAULT 0,
            total_per_upstream INTEGER,
            last_walked REAL
        )
    """)
    # migration for pre-2.4.1 DBs
    cols = {row[1] for row in _write_conn.execute("PRAGMA table_info(subjects)").fetchall()}
    if "subject_type" not in cols:
        _write_conn.execute("ALTER TABLE subjects ADD COLUMN subject_type INTEGER")
    if "genre" not in cols:
        _write_conn.execute("ALTER TABLE subjects ADD COLUMN genre TEXT")
    _write_conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON subjects(title)")
    _write_conn.execute("CREATE INDEX IF NOT EXISTS idx_rating ON subjects(rating)")
    _write_conn.execute("CREATE INDEX IF NOT EXISTS idx_last_seen ON subjects(last_seen)")
    _write_conn.execute("CREATE INDEX IF NOT EXISTS idx_movie ON subjects(is_movie)")
    _write_conn.execute("CREATE INDEX IF NOT EXISTS idx_tv ON subjects(is_tv)")
    _write_conn.execute("CREATE INDEX IF NOT EXISTS idx_anime ON subjects(is_anime)")
    _write_conn.execute("CREATE INDEX IF NOT EXISTS idx_stype ON subjects(subject_type)")
    _write_conn.commit()

def _read(sql: str, params: tuple = ()) -> list:
    conn = sqlite3.connect(CATALOG_DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()

def _read_scalar(sql: str, params: tuple = ()):
    conn = sqlite3.connect(CATALOG_DB)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def _classify_section(title: str) -> tuple[int, int, int]:
    clean = title.lower().replace("🔥", "").replace("🆓", "").strip()
    return (
        1 if clean in MOVIE_SECTIONS else 0,
        1 if clean in TV_SECTIONS else 0,
        1 if clean in ANIME_SECTIONS else 0,
    )

def _upsert_subject(sub: dict, section: str | None = None) -> bool:
    if _write_conn is None:
        return False
    sid = str(sub.get("subjectId") or "")
    if not sid:
        return False
    now = time.time()
    is_movie = is_tv = is_anime = 0
    if section:
        is_movie, is_tv, is_anime = _classify_section(section)
    try:
        _write_conn.execute("""
            INSERT INTO subjects
                (subject_id, title, poster_url, slug, rating, year, badge,
                 is_movie, is_tv, is_anime, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id) DO UPDATE SET
                title      = COALESCE(excluded.title, subjects.title),
                poster_url = COALESCE(excluded.poster_url, subjects.poster_url),
                slug       = COALESCE(excluded.slug, subjects.slug),
                rating     = COALESCE(excluded.rating, subjects.rating),
                year       = COALESCE(excluded.year, subjects.year),
                badge      = COALESCE(excluded.badge, subjects.badge),
                is_movie   = MAX(subjects.is_movie, excluded.is_movie),
                is_tv      = MAX(subjects.is_tv, excluded.is_tv),
                is_anime   = MAX(subjects.is_anime, excluded.is_anime),
                last_seen  = excluded.last_seen
        """, (
            sid,
            sub.get("title"),
            (sub.get("cover") or {}).get("url"),
            sub.get("detailPath"),
            sub.get("imdbRatingValue"),
            (sub.get("releaseDate") or "")[:4] or None,
            sub.get("corner"),
            is_movie, is_tv, is_anime,
            now, now,
        ))
        return True
    except Exception as e:
        print(f"upsert {sid} failed: {e}")
        return False

async def _ingest_home_sections() -> int:
    try:
        ops = await _get_home_operating_list()
    except Exception:
        return 0
    count = 0
    for op in ops:
        op_type = op.get("type")
        section_title = op.get("title", "")
        if op_type not in ("SUBJECTS_MOVIE", "SUBJECTS_TV", "SUBJECTS_ANIMATION"):
            continue
        for raw in op.get("subjects", []) or []:
            if _upsert_subject(raw, section=section_title):
                count += 1
    if _write_conn is not None:
        _write_conn.commit()
    return count

async def _walk_keyword(kw: str) -> int:
    seen = 0
    pages = 0
    hit_cap = 0
    total_upstream = None
    for page in range(1, CATALOG_MAX_PAGES_PER_KEYWORD + 1):
        try:
            data = await _make_request(
                f"{API_BASE}/subject/search",
                method="POST",
                payload={"keyword": kw, "page": page, "perPage": 20},
            )
        except HTTPException:
            break
        inner = data.get("data", {}) or {}
        items = inner.get("items") or inner.get("list") or []
        if not items:
            break
        if _write_conn is None:
            break
        for sub in items:
            if _upsert_subject(sub):
                seen += 1
        _write_conn.commit()
        pages = page
        pager = inner.get("pager") or {}
        if total_upstream is None:
            total_upstream = pager.get("totalCount")
        if not pager.get("hasMore"):
            break
        if page == CATALOG_MAX_PAGES_PER_KEYWORD:
            hit_cap = 1
    # record stats
    try:
        _write_conn.execute("""
            INSERT INTO keyword_stats (keyword, pages_walked, items_seen, hit_cap, total_per_upstream, last_walked)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(keyword) DO UPDATE SET
                pages_walked = excluded.pages_walked,
                items_seen = excluded.items_seen,
                hit_cap = excluded.hit_cap,
                total_per_upstream = excluded.total_per_upstream,
                last_walked = excluded.last_walked
        """, (kw, pages, seen, hit_cap, total_upstream, time.time()))
        _write_conn.commit()
    except Exception as e:
        print(f"keyword_stats {kw} failed: {e}")
    return seen

async def _run_full_walk():
    if _walk_state.get("running"):
        return
    _walk_state.update({
        "running": True,
        "started_at": time.time(),
        "keywords_done": 0,
        "keywords_total": len(CATALOG_KEYWORDS),
        "items_seen": 0,
    })
    try:
        await _ingest_home_sections()
        for kw in CATALOG_KEYWORDS:
            try:
                count = await _walk_keyword(kw)
                _walk_state["keywords_done"] += 1
                _walk_state["items_seen"] += count
            except Exception as e:
                print(f"walk {kw!r} failed: {e}")
                _walk_state["keywords_done"] += 1
        _walk_state["finished_at"] = time.time()
        _walk_state["duration_seconds"] = round(_walk_state["finished_at"] - _walk_state["started_at"], 1)
        _walk_state["total_indexed"] = _read_scalar("SELECT COUNT(*) FROM subjects") or 0
    finally:
        _walk_state["running"] = False

# ─── ENRICHMENT ───────────────────────────────────────────────────────────────

def _classify_from_detail(subject_type, genre: str | None) -> tuple[int, int, int] | None:
    """Return (is_movie, is_tv, is_anime) or None if unknown type."""
    if subject_type == 1:
        return (1, 0, 0)
    if subject_type == 2:
        if genre and "anime" in genre.lower():
            return (0, 0, 1)
        return (0, 1, 0)
    if subject_type == 6:
        return (0, 0, 0)  # music video — no category, filter out
    return None

async def _enrich_subject(row) -> bool:
    sid = row["subject_id"]
    slug = row["slug"]
    if not slug:
        return False
    try:
        data = await _make_request(f"{API_BASE}/detail?detailPath={slug}")
    except HTTPException:
        return False
    sub = (data.get("data") or {}).get("subject") or {}
    stype = sub.get("subjectType")
    genre = sub.get("genre") or ""
    classification = _classify_from_detail(stype, genre)
    if classification is None:
        # unknown type — mark as processed so we don't retry forever
        # use sentinel value 0 for subject_type so it never re-enters queue
        try:
            _write_conn.execute(
                "UPDATE subjects SET subject_type=0, genre=? WHERE subject_id=?",
                (genre, sid),
            )
            return True
        except Exception:
            return False
    is_movie, is_tv, is_anime = classification
    try:
        _write_conn.execute(
            "UPDATE subjects SET subject_type=?, genre=?, is_movie=?, is_tv=?, is_anime=? WHERE subject_id=?",
            (stype, genre, is_movie, is_tv, is_anime, sid),
        )
        return True
    except Exception as e:
        print(f"enrich {sid} failed: {e}")
        return False

async def _run_enrichment():
    if _walk_state.get("enriching"):
        return
    _walk_state["enriching"] = True
    enriched = 0
    failed = 0
    try:
        while True:
            rows = _read("""
                SELECT subject_id, slug FROM subjects
                WHERE subject_type IS NULL
                  AND slug IS NOT NULL
                  AND is_movie = 0 AND is_tv = 0 AND is_anime = 0
                LIMIT ?
            """, (ENRICH_BATCH_SIZE,))
            if not rows:
                break
            for row in rows:
                ok = await _enrich_subject(row)
                if ok:
                    enriched += 1
                else:
                    failed += 1
                if _write_conn is not None:
                    _write_conn.commit()
                await asyncio.sleep(ENRICH_RATE_LIMIT)
            _walk_state["enrich_progress"] = {
                "enriched": enriched,
                "failed": failed,
                "remaining": _read_scalar(
                    "SELECT COUNT(*) FROM subjects WHERE subject_type IS NULL AND slug IS NOT NULL"
                ) or 0,
            }
        _walk_state["enrich_progress"] = {
            "enriched": enriched,
            "failed": failed,
            "remaining": 0,
            "finished_at": time.time(),
        }
    finally:
        _walk_state["enriching"] = False

async def _catalog_maintenance_loop():
    await asyncio.sleep(2)
    try:
        if (_read_scalar("SELECT COUNT(*) FROM subjects") or 0) == 0:
            await _run_full_walk()
        await _run_enrichment()
    except Exception as e:
        print(f"initial catalog setup failed: {e}")
    while True:
        try:
            await asyncio.sleep(CATALOG_REFRESH_INTERVAL)
            await _run_full_walk()
            await _run_enrichment()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"catalog refresh failed: {e}")

# ─── LIFESPAN ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _write_conn, _background_task
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0),
        limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
        follow_redirects=True,
    )
    _write_conn = sqlite3.connect(CATALOG_DB, check_same_thread=False)
    _write_conn.execute("PRAGMA journal_mode=WAL")
    _write_conn.execute("PRAGMA synchronous=NORMAL")
    _init_schema()
    _background_task = asyncio.create_task(_catalog_maintenance_loop())
    try:
        yield
    finally:
        if _background_task is not None:
            _background_task.cancel()
            try:
                await _background_task
            except asyncio.CancelledError:
                pass
        await _client.aclose()
        _client = None
        if _write_conn is not None:
            _write_conn.close()
            _write_conn = None

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

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _require_client() -> httpx.AsyncClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="Server initializing, retry shortly")
    return _client

async def _get_bearer_token() -> str:
    global _bearer_token, _token_acquired_at
    if _bearer_token:
        return _bearer_token
    async with _token_lock:
        if _bearer_token:
            return _bearer_token
        client = _require_client()
        try:
            resp = await client.get(f"{API_BASE}/home?host=moviebox.ph", headers=DEFAULT_HEADERS)
            x_user = resp.headers.get("x-user")
            if x_user:
                try:
                    _bearer_token = json.loads(x_user).get("token")
                except json.JSONDecodeError:
                    _bearer_token = None
            if not _bearer_token:
                cookie = resp.headers.get("set-cookie", "")
                m = re.search(r"token=([^;]+)", cookie)
                if m:
                    _bearer_token = m.group(1)
            if _bearer_token:
                _token_acquired_at = time.time()
        except Exception:
            _bearer_token = None
    return _bearer_token or ""

async def _make_request(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    custom_headers: dict | None = None,
) -> dict:
    global _bearer_token, _token_acquired_at
    client = _require_client()
    token = await _get_bearer_token()
    headers = {**DEFAULT_HEADERS, **(custom_headers or {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_exc: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            if method == "POST":
                resp = await client.post(url, headers=headers, json=payload)
            else:
                resp = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            last_exc = e
            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            raise HTTPException(status_code=502, detail=f"Request failed after {RETRY_ATTEMPTS} attempts: {e}")

        x_user = resp.headers.get("x-user")
        if x_user:
            try:
                new_token = json.loads(x_user).get("token")
                if new_token and new_token != _bearer_token:
                    _bearer_token = new_token
                    _token_acquired_at = time.time()
                    headers["Authorization"] = f"Bearer {new_token}"
            except json.JSONDecodeError:
                pass

        if resp.status_code == 401 and attempt == 0:
            _bearer_token = None
            _token_acquired_at = 0.0
            token = await _get_bearer_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            continue

        if resp.status_code == 429 and attempt < RETRY_ATTEMPTS - 1:
            retry_after = resp.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else RETRY_BACKOFF * (2 ** attempt)
            await asyncio.sleep(delay)
            continue

        if resp.status_code in (500, 502, 503, 504) and attempt < RETRY_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
            continue

        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Upstream API error: {resp.status_code}")

        try:
            return resp.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    raise HTTPException(status_code=502, detail=f"Request failed: {last_exc}")

async def _get_player_domain() -> str:
    now = time.time()
    if _domain_cache["value"] and now < _domain_cache["expires"]:
        return _domain_cache["value"]
    data = await _make_request(f"{API_BASE}/media-player/get-domain")
    domain = (data.get("data") or "https://netfilm.world").rstrip("/")
    _domain_cache["value"] = domain
    _domain_cache["expires"] = now + DOMAIN_TTL
    return domain

def _build_player_referer(domain: str, detail_path: str, subject_id: str, se: int, ep: int) -> str:
    return (
        f"{domain}/spa/videoPlayPage/movies/{detail_path}"
        f"?id={subject_id}&type=/movie/detail&detailSe={se}&detailEp={ep}&lang=en"
    )

async def _resolve_stream(subject_id: str, detail_path: str, se: int, ep: int) -> dict:
    domain = await _get_player_domain()
    referer = _build_player_referer(domain, detail_path, subject_id, se, ep)
    play_url = (
        f"{domain}/wefeed-h5api-bff/subject/play"
        f"?subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}"
    )
    client = _require_client()
    resp = await client.get(play_url, headers={**PLAYER_HEADERS, "Referer": referer})
    try:
        data = resp.json().get("data", {}) or {}
    except Exception:
        raise HTTPException(status_code=502, detail="Player returned non-JSON")
    return {"domain": domain, "referer": referer, "data": data}

async def _resolve_captions(subject_id: str, detail_path: str, se: int, ep: int) -> list:
    stream = await _resolve_stream(subject_id, detail_path, se, ep)
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

    cap_url = (
        f"{API_BASE}/subject/caption"
        f"?format={stream_format}&id={stream_id}"
        f"&subjectId={subject_id}&detailPath={detail_path}"
    )
    data = await _make_request(cap_url)
    inner = data.get("data", {}) or {}
    return inner.get("captions", []) if isinstance(inner, dict) else inner

def _normalize_banner_item(item: dict) -> dict | None:
    subject = item.get("subject") or {}
    title = item.get("title") or subject.get("title")
    if not title or "Communities" in title:
        return None
    return {
        "name": title,
        "poster_url": (item.get("image") or {}).get("url") or (subject.get("cover") or {}).get("url"),
        "slug": item.get("detailPath") or subject.get("detailPath"),
        "subject_id": subject.get("subjectId"),
        "badge": subject.get("corner"),
    }

def _normalize_subject_item(sub: dict) -> dict | None:
    return {
        "name": sub.get("title"),
        "poster_url": (sub.get("cover") or {}).get("url"),
        "slug": sub.get("detailPath"),
        "subject_id": sub.get("subjectId"),
        "badge": sub.get("corner"),
        "rating": sub.get("imdbRatingValue"),
    }

_TS_RE = re.compile(r"^(\d{1,2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}),(\d{3})(.*)$")
_INDEX_RE = re.compile(r"^\d+$")

def srt_to_vtt(srt: str) -> str:
    text = srt.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = ["WEBVTT", ""]
    for line in text.split("\n"):
        stripped = line.strip()
        if _INDEX_RE.match(stripped):
            continue
        m = _TS_RE.match(stripped)
        if m:
            out.append(f"{m.group(1)}.{m.group(2)} --> {m.group(3)}.{m.group(4)}{m.group(5)}")
            continue
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"

def _pick_mp4_stream(streams: list, res: str) -> dict | None:
    if not streams:
        return None
    if res == "best" or not res:
        return max(streams, key=lambda s: s.get("resolutions") or 0)
    target = res.rstrip("p")
    for s in streams:
        if str(s.get("resolutions")) == target:
            return s
    return None

async def _get_home_operating_list() -> list:
    data = await _make_request(f"{API_BASE}/home?host=moviebox.ph")
    return data.get("data", {}).get("operatingList", []) or []

async def _catalog_from_home(keywords: list[str], page: int, per_page: int = 24) -> dict | None:
    ops = await _get_home_operating_list()
    matched_items: list[dict] = []
    matched_sections: set[str] = set()
    for op in ops:
        op_type = op.get("type")
        title = op.get("title", "")
        if not any(kw.lower() in title.lower() for kw in keywords):
            continue
        if op_type not in ("SUBJECTS_MOVIE", "SUBJECTS_TV", "SUBJECTS_ANIMATION"):
            continue
        matched_sections.add(title)
        for raw in op.get("subjects", []) or []:
            item = _normalize_subject_item(raw)
            if item and item.get("subject_id"):
                matched_items.append(item)
    if not matched_items:
        return None
    start = (page - 1) * per_page
    paged = matched_items[start:start + per_page]
    return {
        "page": page,
        "per_page": per_page,
        "total": len(matched_items),
        "total_sentinel": False,
        "source": "home_fallback",
        "sections": sorted(matched_sections),
        "items": paged,
    }

# ─── CATALOG ROUTES ───────────────────────────────────────────────────────────

def _row_to_item(r) -> dict:
    return {
        "name": r["title"],
        "poster_url": r["poster_url"],
        "slug": r["slug"],
        "subject_id": r["subject_id"],
        "rating": r["rating"],
        "year": r["year"],
        "badge": r["badge"],
        "subject_type": r["subject_type"] if "subject_type" in r.keys() else None,
        "genre": r["genre"] if "genre" in r.keys() else None,
        "types": [
            t for t, f in (("movie", r["is_movie"]), ("tv", r["is_tv"]), ("anime", r["is_anime"])) if f
        ],
    }

@app.get("/catalog")
async def catalog(
    type: str = "all",
    sort: str = "recent",
    page: int = 1,
    per_page: int = 24,
    q: str | None = None,
):
    where = []
    params: list = []
    if type == "movie":
        where.append("is_movie = 1")
    elif type == "tv":
        where.append("is_tv = 1")
    elif type == "anime":
        where.append("is_anime = 1")
    elif type not in ("all", ""):
        raise HTTPException(status_code=400, detail=f"Unknown type: {type} (all|movie|tv|anime)")
    if q:
        where.append("title LIKE ?")
        params.append(f"%{q}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if sort == "title":
        order = "title COLLATE NOCASE ASC"
    elif sort == "rating":
        order = "CAST(rating AS REAL) DESC NULLS LAST"
    elif sort == "recent":
        order = "last_seen DESC"
    else:
        raise HTTPException(status_code=400, detail=f"Unknown sort: {sort} (recent|title|rating)")

    per_page = max(1, min(per_page, 100))
    offset = (max(1, page) - 1) * per_page
    total = _read_scalar(f"SELECT COUNT(*) FROM subjects {where_sql}", tuple(params)) or 0
    rows = _read(
        f"""
        SELECT subject_id, title, poster_url, slug, rating, year, badge,
               is_movie, is_tv, is_anime, subject_type, genre
        FROM subjects {where_sql}
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        tuple(params + [per_page, offset]),
    )
    return {
        "type": type,
        "sort": sort,
        "query": q,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page if per_page else 0,
        "items": [_row_to_item(r) for r in rows],
    }

@app.get("/catalog/stats")
async def catalog_stats():
    total = _read_scalar("SELECT COUNT(*) FROM subjects") or 0
    movies = _read_scalar("SELECT COUNT(*) FROM subjects WHERE is_movie = 1") or 0
    tv = _read_scalar("SELECT COUNT(*) FROM subjects WHERE is_tv = 1") or 0
    anime = _read_scalar("SELECT COUNT(*) FROM subjects WHERE is_anime = 1") or 0
    untyped = _read_scalar(
        "SELECT COUNT(*) FROM subjects WHERE is_movie = 0 AND is_tv = 0 AND is_anime = 0"
    ) or 0
    untyped_pending = _read_scalar(
        "SELECT COUNT(*) FROM subjects WHERE subject_type IS NULL AND slug IS NOT NULL"
    ) or 0
    subject_types = {
        str(r[0]): r[1] for r in _read(
            "SELECT subject_type, COUNT(*) FROM subjects WHERE subject_type IS NOT NULL GROUP BY subject_type"
        )
    }
    kw_rows = _read(
        "SELECT keyword, pages_walked, items_seen, hit_cap, total_per_upstream FROM keyword_stats ORDER BY keyword"
    )
    keyword_stats = {
        r["keyword"]: {
            "pages": r["pages_walked"],
            "items": r["items_seen"],
            "hit_cap": bool(r["hit_cap"]),
            "upstream_total": r["total_per_upstream"],
        }
        for r in kw_rows
    }
    return {
        "total": total,
        "movies": movies,
        "tv": tv,
        "anime": anime,
        "untyped": untyped,
        "untyped_pending_enrichment": untyped_pending,
        "subject_types": subject_types,
        "db_path": CATALOG_DB,
        "refresh_interval_seconds": CATALOG_REFRESH_INTERVAL,
        "keywords_total": len(CATALOG_KEYWORDS),
        "walk_state": _walk_state,
        "keyword_stats": keyword_stats,
    }

@app.post("/catalog/refresh")
async def catalog_refresh(mode: str = "full"):
    """mode=full re-walks keywords + enriches. mode=enrich only enriches untyped."""
    if mode == "enrich":
        if _walk_state.get("enriching"):
            return {"status": "enrich_already_running", "walk_state": _walk_state}
        asyncio.create_task(_run_enrichment())
        return {"status": "enrich_queued"}
    if _walk_state.get("running"):
        return {"status": "walk_already_running", "walk_state": _walk_state}
    async def _full():
        await _run_full_walk()
        await _run_enrichment()
    asyncio.create_task(_full())
    return {"status": "full_queued"}

@app.post("/catalog/add")
async def catalog_add(slug: str):
    """Manually fetch a subject by slug and add it to the catalog. For anything
    the walk missed (e.g. 'Matrix' if upstream ranking doesn't surface it)."""
    data = await _make_request(f"{API_BASE}/detail?detailPath={slug}")
    sub = (data.get("data") or {}).get("subject") or {}
    if not sub.get("subjectId"):
        raise HTTPException(status_code=502, detail="Detail response has no subject")
    _upsert_subject(sub)
    if _write_conn is not None:
        _write_conn.commit()
    # run enrichment immediately for this one
    row = _read("SELECT subject_id, slug FROM subjects WHERE subject_id=?", (str(sub["subjectId"]),))
    if row:
        await _enrich_subject(row[0])
        if _write_conn is not None:
            _write_conn.commit()
    return {
        "status": "added",
        "subject_id": str(sub["subjectId"]),
        "title": sub.get("title"),
        "subject_type": sub.get("subjectType"),
        "genre": sub.get("genre"),
    }

# ─── CATALOG (home fallback) ──────────────────────────────────────────────────

async def _get_category_data(
    tab_id: int,
    page: int = 1,
    per_page: int = 24,
    sort: str = "RECOMMEND",
) -> dict:
    url = f"{API_BASE}/subject/filter"
    payload = {
        "tabId": tab_id,
        "filter": {
            "sort": sort,
            "genre": "ALL",
            "country": "ALL",
            "year": "ALL",
            "language": "ALL",
        },
        "page": page,
        "perPage": per_page,
    }
    data = await _make_request(url, method="POST", payload=payload)
    inner = data.get("data", {}) or {}
    raw_items = inner.get("items") or inner.get("subjects") or []
    items = [
        {
            "name": sub.get("title"),
            "poster_url": (sub.get("cover") or {}).get("url"),
            "slug": sub.get("detailPath"),
            "subject_id": sub.get("subjectId"),
            "badge": sub.get("corner"),
            "rating": sub.get("imdbRatingValue"),
            "year": (sub.get("releaseDate") or "")[:4] or None,
        }
        for sub in raw_items
    ]
    pager = inner.get("pager", {}) or {}
    total = pager.get("totalCount") or inner.get("total") or len(items)
    sentinel = bool(total and total >= 1000000)
    return {
        "tab_id": tab_id,
        "page": page,
        "per_page": per_page,
        "total": None if sentinel else total,
        "total_sentinel": sentinel,
        "source": "filter",
        "items": items,
    }

async def _catalog(
    kind: str,
    tab_id: int,
    page: int,
    sort: str,
    source: str,
) -> dict:
    if source in ("auto", "filter"):
        result = await _get_category_data(tab_id, page=page, sort=sort)
        if not result["total_sentinel"] or source == "filter":
            return result
    fallback = await _catalog_from_home(CATALOG_SECTIONS[kind], page=page)
    if fallback:
        return fallback
    result = await _get_category_data(tab_id, page=page, sort=sort)
    result["source"] = "filter_sentinel_no_fallback"
    result["note"] = (
        f"No home sections matched for '{kind}'. "
        f"Try /sections to list available sections, /browse/{{section}}, or /catalog."
    )
    return result

@app.get("/movies")
async def get_movies(
    page: int = 1,
    sort: str = "RECOMMEND",
    tab: int | None = None,
    source: str = "auto",
):
    return await _catalog(
        "movies",
        tab_id=tab if tab is not None else TAB_MOVIES,
        page=page, sort=sort, source=source,
    )

@app.get("/tv-series")
async def get_tv_series(
    page: int = 1,
    sort: str = "RECOMMEND",
    tab: int | None = None,
    source: str = "auto",
):
    return await _catalog(
        "tv-series",
        tab_id=tab if tab is not None else TAB_TV,
        page=page, sort=sort, source=source,
    )

@app.get("/animation")
async def get_animation(
    page: int = 1,
    sort: str = "RECOMMEND",
    tab: int | None = None,
    source: str = "auto",
):
    return await _catalog(
        "animation",
        tab_id=tab if tab is not None else TAB_ANIMATION,
        page=page, sort=sort, source=source,
    )

@app.get("/sections")
async def list_sections():
    ops = await _get_home_operating_list()
    out = []
    for op in ops:
        op_type = op.get("type")
        title = op.get("title", "Featured")
        if op_type == "BANNER":
            count = len(op.get("banner", {}).get("items", []) or [])
        elif op_type in ("SUBJECTS_MOVIE", "SUBJECTS_TV", "SUBJECTS_ANIMATION"):
            count = len(op.get("subjects", []) or [])
        else:
            count = 0
        out.append({"section": title, "type": op_type, "count": count})
    return {"sections": out}

@app.get("/browse/{section}")
async def browse_section(section: str, page: int = 1, per_page: int = 24):
    ops = await _get_home_operating_list()
    matched: list[dict] = []
    matched_titles: set[str] = set()
    for op in ops:
        op_type = op.get("type")
        title = op.get("title", "")
        if section.lower() not in title.lower():
            continue
        if op_type not in ("SUBJECTS_MOVIE", "SUBJECTS_TV", "SUBJECTS_ANIMATION"):
            continue
        matched_titles.add(title)
        for raw in op.get("subjects", []) or []:
            item = _normalize_subject_item(raw)
            if item and item.get("subject_id"):
                matched.append(item)
    if not matched:
        raise HTTPException(
            status_code=404,
            detail=f"No section matching: {section!r}. Try /sections to list available.",
        )
    start = (page - 1) * per_page
    paged = matched[start:start + per_page]
    return {
        "section": section,
        "matched_titles": sorted(matched_titles),
        "page": page,
        "per_page": per_page,
        "total": len(matched),
        "items": paged,
    }

# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)

# ─── HOME ─────────────────────────────────────────────────────────────────────

@app.get("/home")
async def get_home():
    ops = await _get_home_operating_list()
    sections = []
    for op in ops:
        op_type = op.get("type")
        title = op.get("title", "Featured")
        if op_type == "BANNER":
            items = [
                n for n in (
                    _normalize_banner_item(i)
                    for i in op.get("banner", {}).get("items", []) or []
                ) if n
            ]
            sections.append({"section": "Banner", "count": len(items), "items": items})
        elif op_type in ("SUBJECTS_MOVIE", "SUBJECTS_TV", "SUBJECTS_ANIMATION"):
            items = [
                n for n in (
                    _normalize_subject_item(s)
                    for s in op.get("subjects", []) or []
                ) if n
            ]
            sections.append({"section": title, "count": len(items), "items": items})
    return {"status": "success", "sections": sections}

@app.get("/home/all")
async def get_home_aggregate(dedupe: bool = True):
    ops = await _get_home_operating_list()
    items: list[dict] = []
    seen: set[str] = set()
    for op in ops:
        op_type = op.get("type")
        if op_type == "BANNER":
            for raw in op.get("banner", {}).get("items", []) or []:
                norm = _normalize_banner_item(raw)
                if not norm or not norm.get("subject_id"):
                    continue
                if dedupe and norm["subject_id"] in seen:
                    continue
                seen.add(norm["subject_id"])
                items.append(norm)
        elif op_type in ("SUBJECTS_MOVIE", "SUBJECTS_TV", "SUBJECTS_ANIMATION"):
            for raw in op.get("subjects", []) or []:
                norm = _normalize_subject_item(raw)
                if not norm or not norm.get("subject_id"):
                    continue
                if dedupe and norm["subject_id"] in seen:
                    continue
                seen.add(norm["subject_id"])
                items.append(norm)
    return {"count": len(items), "items": items}

# ─── SEARCH ───────────────────────────────────────────────────────────────────

@app.get("/search/suggest")
async def get_search_suggestions(
    q: str = Query(..., min_length=1),
    enrich: bool = False,
):
    url = f"{API_BASE}/subject/search-suggest"
    data = await _make_request(url, method="POST", payload={"keyword": q, "perPage": 10})
    inner = data.get("data", {}) or {}
    raw = inner.get("items") or inner.get("list") or []
    titles: list[str] = []
    for item in raw:
        title = (
            (item.get("subject") or {}).get("title")
            or item.get("word")
            or item.get("title")
            or item.get("keyword")
        )
        if title and title not in titles:
            titles.append(title)

    if enrich:
        suggestions = await asyncio.gather(*(_enrich_suggestion(t) for t in titles))
    else:
        suggestions = [{"title": t} for t in titles]

    return {"query": q, "count": len(suggestions), "suggestions": suggestions}

async def _enrich_suggestion(title: str) -> dict:
    try:
        data = await _make_request(
            f"{API_BASE}/subject/search",
            method="POST",
            payload={"keyword": title, "page": 1, "perPage": 1},
        )
        inner = data.get("data", {}) or {}
        raw = inner.get("items") or inner.get("list") or []
        if raw:
            sub = raw[0]
            return {
                "title": sub.get("title") or title,
                "slug": sub.get("detailPath"),
                "subject_id": sub.get("subjectId"),
            }
    except Exception:
        pass
    return {"title": title, "slug": None, "subject_id": None}

@app.get("/search")
async def search(q: str = Query(..., min_length=1), page: int = 1):
    url = f"{API_BASE}/subject/search"
    data = await _make_request(url, method="POST", payload={"keyword": q, "page": page, "perPage": 20})
    inner = data.get("data", {}) or {}
    raw = inner.get("items") or inner.get("list") or []
    items = [
        {
            "name": sub.get("title"),
            "poster_url": (sub.get("cover") or {}).get("url"),
            "slug": sub.get("detailPath"),
            "subject_id": sub.get("subjectId"),
        }
        for sub in raw
    ]
    pager = inner.get("pager", {}) or {}
    total = pager.get("totalCount") or inner.get("total") or len(items)
    return {"query": q, "page": page, "total": total, "items": items}

@app.get("/detail/{slug}")
async def get_movie_detail(slug: str):
    return await _make_request(f"{API_BASE}/detail?detailPath={slug}")

# ─── STREAM ───────────────────────────────────────────────────────────────────

@app.get("/api/stream/{subject_id}")
async def get_stream_sources(
    subject_id: str,
    detail_path: str,
    se: int = 1,
    ep: int = 1,
):
    stream = await _resolve_stream(subject_id, detail_path, se, ep)
    data = stream["data"]
    has_resource = data.get("hasResource", False)
    streams = [
        {
            "resolution": f"{s.get('resolutions')}p" if s.get("resolutions") else None,
            "format": s.get("format"),
            "url": s.get("url"),
            "size": s.get("size"),
            "duration": s.get("duration"),
            "codec": s.get("codecName"),
        }
        for s in data.get("streams", []) or []
    ]
    return {
        "subject_id": subject_id,
        "se": se,
        "ep": ep,
        "has_resource": has_resource,
        "sources": streams,
        "hls": data.get("hls", []) or [],
        "dash": data.get("dash", []) or [],
        "free_episodes": data.get("freeNum"),
        "limited": data.get("limited", False),
        "note": None if has_resource else "No stream found for this episode.",
    }

@app.get("/api/stream/{subject_id}/captions")
async def get_captions(
    subject_id: str,
    detail_path: str,
    se: int = 1,
    ep: int = 1,
):
    captions = await _resolve_captions(subject_id, detail_path, se, ep)
    return {
        "subject_id": subject_id,
        "se": se,
        "ep": ep,
        "count": len(captions),
        "captions": captions,
    }

@app.get("/api/stream/{subject_id}/proxy")
async def proxy_stream(
    subject_id: str,
    request: Request,
    detail_path: str,
    se: int = 1,
    ep: int = 1,
    res: str = "best",
):
    stream = await _resolve_stream(subject_id, detail_path, se, ep)
    data = stream["data"]
    stream_list = data.get("streams") or []
    picked = _pick_mp4_stream(stream_list, res)
    if not picked:
        raise HTTPException(
            status_code=502,
            detail=f"No MP4 source for res={res}. Try /api/stream for HLS/DASH manifests.",
        )

    stream_url = picked.get("url")
    if not stream_url:
        raise HTTPException(status_code=502, detail="Selected source has no URL")

    req_headers = {**PLAYER_HEADERS, "Referer": stream["referer"]}
    range_header = request.headers.get("range")
    if range_header:
        req_headers["Range"] = range_header

    client = _require_client()
    upstream_req = client.build_request("GET", stream_url, headers=req_headers)
    upstream = await client.send(upstream_req, stream=True)

    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream stream error: {upstream.status_code}")

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()

    fwd_headers = {}
    for h in ("content-length", "content-type", "accept-ranges", "content-range", "etag", "last-modified"):
        if h in upstream.headers:
            fwd_headers[h] = upstream.headers[h]

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=fwd_headers,
        media_type=upstream.headers.get("content-type", "video/mp4"),
    )

@app.get("/api/stream/{subject_id}/subtitles/{lang}")
async def get_subtitles_vtt(
    subject_id: str,
    lang: str,
    detail_path: str,
    se: int = 1,
    ep: int = 1,
):
    captions = await _resolve_captions(subject_id, detail_path, se, ep)
    match = next((c for c in captions if (c.get("lan") or "").lower() == lang.lower()), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"No caption for language: {lang}")

    srt_url = match.get("url")
    if not srt_url or not srt_url.startswith("https://"):
        raise HTTPException(status_code=502, detail="Invalid caption URL")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(srt_url)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Caption fetch failed: {r.status_code}")

    vtt = srt_to_vtt(r.text)
    return Response(
        content=vtt,
        media_type="text/vtt",
        headers={"Content-Disposition": f'inline; filename="{lang}.vtt"'},
    )

# ─── DIAGNOSTICS ──────────────────────────────────────────────────────────────

def _tab_probe(tab_id: int, per_page: int, variant_body: dict | None) -> dict:
    payload = {"tabId": tab_id, "page": 1, "perPage": per_page}
    if variant_body is not None:
        payload.update(variant_body)
    return payload

@app.get("/api/tabs")
async def discover_tabs(min_id: int = 1, max_id: int = 20, per_page: int = 3):
    results = []
    for tab_id in range(min_id, max_id + 1):
        try:
            payload = _tab_probe(tab_id, per_page, TAB_PAYLOAD_VARIANTS["baseline"])
            data = await _make_request(f"{API_BASE}/subject/filter", method="POST", payload=payload)
            inner = data.get("data", {}) or {}
            raw = inner.get("items") or inner.get("subjects") or []
            pager = inner.get("pager") or {}
            results.append({
                "tab_id": tab_id,
                "count": len(raw),
                "pager_total": pager.get("totalCount"),
                "sentinel": bool((pager.get("totalCount") or 0) >= 1000000),
                "sample_titles": [s.get("title") for s in raw if s.get("title")][:3],
            })
        except Exception as e:
            results.append({"tab_id": tab_id, "error": str(e)})
    return {"variant": "baseline", "results": results}

@app.get("/api/tabs/variants")
async def discover_tab_variants(tab_id: int = 2, per_page: int = 3):
    out = []
    for name, body in TAB_PAYLOAD_VARIANTS.items():
        try:
            payload = _tab_probe(tab_id, per_page, body)
            data = await _make_request(f"{API_BASE}/subject/filter", method="POST", payload=payload)
            inner = data.get("data", {}) or {}
            raw = inner.get("items") or inner.get("subjects") or []
            pager = inner.get("pager") or {}
            total = pager.get("totalCount")
            out.append({
                "variant": name,
                "payload": body,
                "count": len(raw),
                "pager_total": total,
                "sentinel": bool((total or 0) >= 1000000),
                "sample_titles": [s.get("title") for s in raw if s.get("title")][:3],
            })
        except Exception as e:
            out.append({"variant": name, "error": str(e)})
    breaking = [v for v in out if not v.get("sentinel") and not v.get("error")]
    return {
        "tab_id": tab_id,
        "variants": out,
        "non_sentinel_variants": [v["variant"] for v in breaking],
        "hint": (
            "If non_sentinel_variants is non-empty, that payload shape works."
        ) if breaking else (
            "All variants sentinel. /subject/filter requires auth we don't have as a guest. "
            "Use /catalog for a real indexed catalog."
        ),
    }

@app.get("/api/debug/upstream")
async def debug_upstream(
    path: str,
    method: str = "GET",
    payload: str | None = None,
):
    if not path.startswith("/"):
        path = "/" + path
    body = json.loads(payload) if payload else None
    return await _make_request(f"{API_BASE}{path}", method=method, payload=body)

@app.get("/health")
async def health():
    token_age = int(time.time() - _token_acquired_at) if _token_acquired_at else None
    pool = _client._transport._pool if _client is not None else None
    return {
        "status": "ok",
        "version": API_VERSION,
        "token_cached": bool(_bearer_token),
        "token_age_seconds": token_age,
        "domain_cached": _domain_cache["value"],
        "domain_expires_in": max(0, int(_domain_cache["expires"] - time.time())),
        "client_pool_connections": len(pool.connections) if pool is not None else None,
        "tabs": {"movies": TAB_MOVIES, "tv": TAB_TV, "animation": TAB_ANIMATION},
        "catalog_db": CATALOG_DB,
        "catalog_indexed": _read_scalar("SELECT COUNT(*) FROM subjects") or 0,
        "walk_running": _walk_state.get("running", False),
        "enriching": _walk_state.get("enriching", False),
    }

# ─── DASHBOARD HTML ───────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MovieBox Pure API | Pro Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{--primary:#ff3d71;--secondary:#3366ff;--accent:#00f2ff;--bg:#07080c;--card-bg:rgba(255,255,255,0.03);--glass:rgba(255,255,255,0.06);--text:#fff}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Outfit',sans-serif;background:var(--bg);color:var(--text);overflow-x:hidden;min-height:100vh;background-image:radial-gradient(circle at 10% 10%,rgba(255,61,113,0.12) 0%,transparent 40%),radial-gradient(circle at 90% 90%,rgba(51,102,255,0.12) 0%,transparent 40%)}
.container{max-width:1300px;margin:0 auto;padding:60px 24px}
header{text-align:center;margin-bottom:80px;animation:fadeInDown 1s ease-out}
@keyframes fadeInDown{from{opacity:0;transform:translateY(-30px)}to{opacity:1;transform:translateY(0)}}
h1{font-size:clamp(2.5rem,8vw,4rem);font-weight:800;background:linear-gradient(135deg,#fff 0%,#aaa 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:15px;letter-spacing:-2px}
.badge{background:linear-gradient(90deg,var(--primary),var(--secondary));padding:8px 18px;border-radius:40px;font-size:.85rem;font-weight:700;display:inline-block;margin-bottom:25px;text-transform:uppercase;letter-spacing:1px;box-shadow:0 10px 30px rgba(255,61,113,0.3)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:30px;margin-top:20px}
.card{background:var(--card-bg);border:1px solid var(--glass);border-radius:28px;padding:35px;transition:all .4s cubic-bezier(.175,.885,.32,1.275);backdrop-filter:blur(12px);display:flex;flex-direction:column}
@media(hover:hover){.card:hover{transform:translateY(-12px) scale(1.02);border-color:rgba(255,255,255,.2);box-shadow:0 30px 60px rgba(0,0,0,.5)}}
.card-title{font-size:1.5rem;font-weight:700;margin-bottom:18px;display:flex;align-items:center;gap:12px}
.card-title i{width:32px;height:32px;background:rgba(255,255,255,0.05);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:1rem;color:var(--accent);font-style:normal}
.card-desc{color:#9ea3ac;font-size:1rem;line-height:1.6;margin-bottom:25px;flex-grow:1}
.endpoint{font-family:'JetBrains Mono',monospace;background:rgba(0,0,0,.4);padding:14px;border-radius:14px;font-size:.8rem;color:var(--accent);border:1px solid rgba(0,242,255,.15);margin-bottom:25px;word-break:break-all;position:relative}
.endpoint::after{content:'GET';position:absolute;right:14px;top:14px;font-size:.65rem;font-weight:800;color:rgba(255,255,255,.3)}
.btn{display:flex;align-items:center;justify-content:center;padding:16px;background:#fff;color:#000;text-decoration:none;border-radius:16px;font-weight:700;font-size:.95rem;transition:all .3s}
.btn:hover{background:var(--primary);color:#fff;transform:translateY(-2px);box-shadow:0 10px 25px rgba(255,61,113,.4)}
footer{text-align:center;padding:80px 0 40px;animation:fadeIn 2s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.dev-tag{font-weight:800;color:#666;letter-spacing:3px;text-transform:uppercase;font-size:.75rem;border:1px solid #222;padding:12px 30px;border-radius:50px;display:inline-block;background:rgba(255,255,255,.01);transition:all .3s}
.dev-tag:hover{color:var(--text);border-color:var(--primary);letter-spacing:5px}
.tag-new{background:var(--accent);color:#000;font-size:.6rem;font-weight:800;padding:3px 8px;border-radius:6px;letter-spacing:1px;margin-left:6px;vertical-align:middle}
@media(max-width:480px){.container{padding:40px 16px}.card{padding:25px}h1{margin-bottom:10px}}
</style>
</head>
<body>
<div class="container">
<header>
<div class="badge">v2.4.1 &middot; Enriched Catalog</div>
<h1>MovieBox Pro</h1>
<p style="color:#667;font-size:1.25rem;font-weight:300">State-of-the-Art Pure API Architecture</p>
</header>
<div class="grid">
<div class="card"><div class="card-title"><i>📇</i> Catalog</div><p class="card-desc">Real catalog from the indexed library. Sort, filter, text-search. Now with type enrichment from <code>/detail</code>.</p><div class="endpoint">/catalog?type=movie&sort=rating</div><a href="/catalog" target="_blank" class="btn">Open</a></div>
<div class="card"><div class="card-title"><i>📊</i> Stats</div><p class="card-desc">Indexed counts, walk progress, enrichment progress, per-keyword walk data.</p><div class="endpoint">/catalog/stats</div><a href="/catalog/stats" target="_blank" class="btn">View</a></div>
<div class="card"><div class="card-title"><i>🧬</i> Enrich<span class="tag-new">v2.4.1</span></div><p class="card-desc">Re-run type classification for untyped subjects without re-walking keywords. POST endpoint.</p><div class="endpoint">POST /catalog/refresh?mode=enrich</div><a href="/catalog/stats" target="_blank" class="btn">Trigger</a></div>
<div class="card"><div class="card-title"><i>➕</i> Manual Add<span class="tag-new">v2.4.1</span></div><p class="card-desc">Insert a specific subject by slug. For anything the walk misses (e.g. Matrix if upstream ranking hides it).</p><div class="endpoint">POST /catalog/add?slug=</div><a href="/catalog/stats" target="_blank" class="btn">Check</a></div>
<div class="card"><div class="card-title"><i>🏠</i> Home</div><p class="card-desc">Headlines, recommended content, trending blocks.</p><div class="endpoint">/home</div><a href="/home" target="_blank" class="btn">Open</a></div>
<div class="card"><div class="card-title"><i>📚</i> Aggregate</div><p class="card-desc">Every section flattened into one deduped feed.</p><div class="endpoint">/home/all</div><a href="/home/all" target="_blank" class="btn">Open</a></div>
<div class="card"><div class="card-title"><i>🧩</i> Sections</div><p class="card-desc">List home sections with type + count.</p><div class="endpoint">/sections</div><a href="/sections" target="_blank" class="btn">List</a></div>
<div class="card"><div class="card-title"><i>🗂️</i> Browse</div><p class="card-desc">Access any home section by name.</p><div class="endpoint">/browse/Cinema</div><a href="/browse/Cinema" target="_blank" class="btn">Open</a></div>
<div class="card"><div class="card-title"><i>🎬</i> Movies</div><p class="card-desc">Home fallback — independent of /catalog.</p><div class="endpoint">/movies</div><a href="/movies" target="_blank" class="btn">Open</a></div>
<div class="card"><div class="card-title"><i>📺</i> TV</div><p class="card-desc">Home fallback.</p><div class="endpoint">/tv-series</div><a href="/tv-series" target="_blank" class="btn">Open</a></div>
<div class="card"><div class="card-title"><i>🌸</i> Animation</div><p class="card-desc">Home fallback.</p><div class="endpoint">/animation</div><a href="/animation" target="_blank" class="btn">Open</a></div>
<div class="card"><div class="card-title"><i>🔍</i> Search</div><p class="card-desc">Full-text search via upstream.</p><div class="endpoint">/search?q=matrix</div><a href="/search?q=matrix" target="_blank" class="btn">Search</a></div>
<div class="card"><div class="card-title"><i>💡</i> Suggest</div><p class="card-desc">Typeahead. <code>?enrich=true</code> for slugs.</p><div class="endpoint">/search/suggest?q=break</div><a href="/search/suggest?q=break" target="_blank" class="btn">Try</a></div>
<div class="card"><div class="card-title"><i>🆔</i> Detail</div><p class="card-desc">Full metadata for a subject.</p><div class="endpoint">/detail/{slug}</div><a href="/detail/attack-on-titan-hindi-kGWQOIx0d4" target="_blank" class="btn">Fetch</a></div>
<div class="card"><div class="card-title"><i>🎥</i> Stream</div><p class="card-desc">MP4 sources + HLS/DASH manifests.</p><div class="endpoint">/api/stream/{sid}</div><a href="/api/stream/56988683026712168?detail_path=attack-on-titan-hindi-kGWQOIx0d4" target="_blank" class="btn">Get</a></div>
<div class="card"><div class="card-title"><i>📥</i> Proxy</div><p class="card-desc">Range-aware MP4 passthrough.</p><div class="endpoint">/api/stream/{sid}/proxy</div><a href="/api/stream/56988683026712168/proxy?detail_path=attack-on-titan-hindi-kGWQOIx0d4&res=best" target="_blank" class="btn">Stream</a></div>
<div class="card"><div class="card-title"><i>📄</i> VTT</div><p class="card-desc">SRT → WebVTT inline.</p><div class="endpoint">/api/stream/{sid}/subtitles/{lang}</div><a href="/api/stream/6207982430134357800/subtitles/en?detail_path=breaking-bad-ej6Bp0MCAo7" target="_blank" class="btn">Get</a></div>
<div class="card"><div class="card-title"><i>❤️</i> Health</div><p class="card-desc">Liveness + catalog state.</p><div class="endpoint">/health</div><a href="/health" target="_blank" class="btn">Check</a></div>
</div>
<footer><div class="dev-tag">Developer: Walter</div></footer>
</div>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    reload_flag = os.environ.get("RELOAD", "0") == "1"
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=reload_flag)