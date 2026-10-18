<p align="center">
  <img src="https://h5-static.aoneroom.com/ssrStatic/mbOfficial/public/_nuxt/web-logo.apJjVir2.svg" alt="LOGO" width="200"/>
</p>

# MovieBox API Pro

A pure REST API wrapper around [moviebox.ph](https://moviebox.ph) with an indexed catalog, provider failover, and a self-hosted frontend.

No scraping. No HTML parsing. No headless browser. It talks the exact same private JSON API the moviebox web player talks — `h5-api.aoneroom.com/wefeed-h5api-bff` — and re-exposes it as a clean, self-hosted REST surface plus a browser UI.

Built for people who want to wire moviebox into their own apps, scripts, bots, dashboards, or media servers without reverse-engineering the network tab every time the site ships an update.

**Version 2.7.0** — module split, provider failover, frontend polish.

---

## What it does

- **Discovery** — banners, featured blocks, and every category row from the moviebox home feed
- **Indexed catalog** — SQLite-backed, 1700+ subjects across movies / TV / anime, self-refreshing on a 6h schedule
- **Search** — full-text search + typeahead suggestions
- **Metadata** — full detail tree for any subject (episodes, seasons, languages, cast, artwork)
- **Streams** — direct MP4 sources per resolution, plus HLS and DASH manifests
- **Stream proxy** — Range-aware MP4 passthrough for media servers
- **Subtitles** — full caption list per episode, SRT→VTT conversion inline
- **Provider failover** — 7 known mirrors, circuit breaker per host, automatic fallover
- **Web frontend** — hero carousel, catalog browser, player with resume / quality / subtitle pickers, bookmarks, watch history
- **Diagnostics** — tab-ID probing, payload variant testing, raw upstream passthrough, health

## What it isn't

- Not a proxy for HLS/DASH manifests (MP4 only)
- Not a downloader (it hands you URLs, you decide what to do with them)
- Not affiliated with moviebox.ph
- Not a scraping tool — it depends on the upstream API staying reachable

---

## Quickstart

```bash
git clone https://github.com/faisaljs/Moviebox-API
cd Moviebox-API
pip install -r requirements.txt
python main.py
```

Server boots on `http://localhost:8000`. Two entry points:

- `http://localhost:8000/` — HTML API dashboard, one-click tests for every endpoint
- `http://localhost:8000/app` — the actual frontend (hero carousel, catalog, player, bookmarks, history)

Dev with autoreload:

```bash
RELOAD=1 python main.py
```

Custom port:

```bash
PORT=9000 python main.py
```

Docker:

```bash
docker compose up -d
```

The first boot kicks off the catalog walk in the background. It takes 2–3 minutes to populate ~1700 rows and enrich the type fields. `/catalog/stats` shows progress.

---

## Endpoints

### Discovery & home

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/home` | Featured sections and banners |
| `GET` | `/home/all` | Flattened deduped feed |
| `GET` | `/sections` | List every home section |
| `GET` | `/browse/{section}` | Direct access to any home section |

### Catalog (indexed)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/catalog` | Indexed catalog with sort/filter/search |
| `GET` | `/catalog/stats` | Counts, walk progress, keyword telemetry |
| `POST` | `/catalog/refresh` | Re-run the walk (`?mode=enrich` for enrichment only) |
| `POST` | `/catalog/add` | Manually add a subject by slug |

### Home-fallback catalogs

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/movies` | Movies from home sections |
| `GET` | `/tv-series` | TV series from home sections |
| `GET` | `/animation` | Animation from home sections |

### Search & metadata

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/search` | Full-text search |
| `GET` | `/search/suggest` | Typeahead (`?enrich=true` for slugs) |
| `GET` | `/detail/{slug}` | Full metadata for a subject |

### Stream & subtitles

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/stream/{subject_id}` | Direct video sources |
| `GET` | `/api/stream/{subject_id}/proxy` | Range-aware MP4 passthrough |
| `GET` | `/api/stream/{subject_id}/captions` | Subtitle list |
| `GET` | `/api/stream/{subject_id}/subtitles/{lang}` | SRT → VTT inline |

### Providers

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/providers` | Live provider health with circuit breaker state |
| `POST` | `/providers/probe` | Force-probe all providers now |
| `POST` | `/providers/reset` | Clear all circuit breakers |
| `GET` | `/api/hosts` | Backwards-compat host status |

### Diagnostics

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness, cache state, catalog counts |
| `GET` | `/api/tabs` | Probe tab IDs (diagnostic) |
| `GET` | `/api/tabs/variants` | Try every known filter payload shape |
| `GET` | `/api/debug/upstream` | Raw upstream passthrough |

---

### `GET /home`

Returns the operating list from the moviebox home feed, grouped into sections. Banner sections and `SUBJECTS_*` rows are normalized into the same item shape.

```bash
curl http://localhost:8000/home | jq
```

```json
{
  "status": "success",
  "host": "moviebox.ph",
  "sections": [
    {
      "section": "Banner",
      "count": 11,
      "items": [
        {
          "name": "VisionQuest",
          "poster_url": "https://.../cover.webp",
          "slug": "visionquest-hindi-0qVaGP1YHE4",
          "subject_id": "3908280950886646112",
          "badge": "Hindi"
        }
      ]
    }
  ]
}
```

### `GET /catalog`

The real catalog. Reads from the SQLite index populated by the background walk.

**Query parameters**

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `type` | string | `all` | `all`, `movie`, `tv`, `anime` |
| `sort` | string | `recent` | `recent`, `title`, `rating` |
| `page` | int | `1` | 1-indexed |
| `per_page` | int | `24` | Max 100 |
| `q` | string | — | Filter title (substring, case-insensitive) |
| `host` | string | — | Filter by source host (all share one catalog upstream) |

```bash
curl "http://localhost:8000/catalog?type=movie&sort=rating&per_page=5" | jq
curl "http://localhost:8000/catalog?q=matrix" | jq
```

Response includes `total`, `total_pages`, and `items[]` with `name`, `poster_url`, `slug`, `subject_id`, `rating`, `year`, `badge`, `subject_type`, `genre`, `types[]`.

### `GET /catalog/stats`

Everything about the index:

```json
{
  "total": 1737,
  "movies": 163,
  "tv": 140,
  "anime": 25,
  "untyped": 1409,
  "untyped_pending_enrichment": 1497,
  "subject_types": { "1": 119, "2": 89, "6": 23 },
  "by_host": { "moviebox.ph": 1737 },
  "walk_state": { "running": false, "enriching": false },
  "keyword_stats": {
    "m": { "pages": 11, "items": 211, "hit_cap": false, "upstream_total": 211 }
  }
}
```

### `POST /catalog/refresh`

```bash
curl -X POST http://localhost:8000/catalog/refresh                          # full walk
curl -X POST "http://localhost:8000/catalog/refresh?mode=enrich"            # enrichment only
curl -X POST "http://localhost:8000/catalog/refresh?force=true"             # walk all hosts, no dedupe
```

Returns immediately; runs in background. Watch `/catalog/stats`.

### `POST /catalog/add?slug=`

Manually add a subject the walk missed. Useful for anything upstream's search ranking doesn't surface (`Matrix` was the case study).

```bash
curl -X POST "http://localhost:8000/catalog/add?slug=matrix-W1E5680Y2S5" | jq
```

Fetches the detail response, upserts, and enriches immediately.

### `GET /api/stream/{subject_id}`

```bash
curl "http://localhost:8000/api/stream/6207982430134357800?detail_path=breaking-bad-ej6Bp0MCAo7&se=1&ep=1" | jq
```

Returns `has_resource`, `sources[]` (resolution, format, codec, url, size, duration), `hls[]`, `dash[]`, `free_episodes`, `limited`.

### `GET /api/stream/{subject_id}/proxy`

Range-aware MP4 passthrough. Drop into media servers (Jellyfin, Plex, Infuse, VLC) that can't set custom headers.

```bash
# full file
curl -O "http://localhost:8000/api/stream/6207982430134357800/proxy?detail_path=breaking-bad-ej6Bp0MCAo7&res=1080p"

# ranged seek
curl -H "Range: bytes=0-1048575" "http://localhost:8000/api/stream/.../proxy?..." -o chunk.bin
```

Forwards `Range` upstream, returns `206` with `Content-Range` on partial. Full-file requests stream through a 64KB chunked iterator.

### `GET /api/stream/{subject_id}/subtitles/{lang}`

Fetches the SRT, strips index lines, converts timestamps, prepends `WEBVTT`. Drop-in for a `<track>` element.

```bash
curl "http://localhost:8000/api/stream/6207982430134357800/subtitles/en?detail_path=breaking-bad-ej6Bp0MCAo7" | head -20
```

```vtt
WEBVTT

00:00:17.833 --> 00:00:18.916
[Vision] <i>Who am I?</i>
```

SRT formatting tags (`<i>`, `<b>`, `{\an8}`) survive the conversion.

### `GET /providers`

Live provider state:

```json
{
  "default_host": "moviebox.ph",
  "provider_count": 7,
  "healthy": 7,
  "degraded": 0,
  "providers": [
    {
      "host": "moviebox.ph",
      "alive": true,
      "circuit_open": false,
      "circuit_open_for": 0,
      "consecutive_failures": 0,
      "total_success": 25,
      "total_failure": 0,
      "avg_latency_ms": 374,
      "last_success_ago": 0,
      "last_failure_ago": null,
      "last_error": null
    }
  ],
  "circuit_breaker": { "threshold": 3, "window_seconds": 60 }
}
```

### `GET /health`

```json
{
  "status": "ok",
  "version": "2.7.0",
  "default_host": "moviebox.ph",
  "providers": { "moviebox.ph": { "alive": true, "latency_ms": 374, "fails": 0, "circuit_open": false } },
  "tokens_cached": 1,
  "domain_cached": "https://mzfi.me",
  "domain_expires_in": 287,
  "catalog_indexed": 1737,
  "walk_running": false,
  "enriching": false
}
```

---

## Frontend at `/app`

Single-file vanilla JS, no build step, no dependencies. Served by the same FastAPI process.

**Views:**

- **Home** — hero carousel across the top (auto-rotating, arrows, dots, Ken Burns zoom), then horizontal scroll rows for every section
- **Catalog** — type tabs (All/Movies/TV/Anime), sort dropdown, text filter, pagination
- **Bookmarks** — everything you've hearted, sorted by save time, "Clear all" button
- **History** — every episode you've watched with progress bar and resume button
- **Detail** — poster, description, metadata, cast, dubs, Play button or season/episode grid, bookmark toggle
- **Player** — modal overlay with quality picker, subtitle picker, resume-from-history, keyboard shortcuts, episode picker on missing streams

**Persistence** (localStorage, per-browser):

- `mb_bookmarks` — saved titles
- `mb_history` — watch progress per `(slug, se, ep)`

**Player keyboard shortcuts:**

| Key | Action |
|-----|--------|
| `Space` / `k` | Play / pause |
| `←` / `→` | Seek ±5s |
| `↑` / `↓` | Volume ±10% |
| `f` | Fullscreen |
| `m` | Mute |
| `Esc` | Close player / exit fullscreen |

---

### Screenshots

![Home](screenshots/home.png)
![Catalog](screenshots/catalog.png)
![Player](screenshots/player.png)

More at [`screenshots/`](screenshots/).

---

## Architecture

### Module layout

```
config.py            env vars + headers + constants
state.py             mutable globals (client, db, tokens, caches, walk_state)
providers.py         provider health, circuit breaker, probe loop
upstream.py          token cache, bff_request, player, stream resolution
storage.py           sqlite schema, read, upsert
helpers.py           normalize, srt_to_vtt, pick_mp4_stream
catalog_worker.py    walk, enrichment, maintenance loop
dashboard.py         DASHBOARD_HTML string
routes.py            all API endpoints
main.py              FastAPI app + lifespan + middleware + / and /app
static/app.html      frontend
```

No circular imports. Import order: `config → state → providers → upstream → catalog_worker → routes → main`. `storage` and `helpers` are leaf modules.

### Guest tokens per provider

Each mirror issues its own guest JWT via the `x-user` response header. Tokens are cached per host. On 401, the token is cleared, re-acquired, and the request retried once on the same host.

### Player domain

Stream URLs live on a different host than the BFF. Fetched from `/media-player/get-domain`, cached for 300s. On stream failure, the cache is invalidated and the domain re-discovered once before surfacing an error.

### Provider failover

Every BFF request tries the primary first, then falls over to the next healthy provider on network failure, 5xx, 429 (honoring Retry-After), or non-JSON. Per-host circuit breaker opens after 3 consecutive failures for 60s. A background probe loop cycles one host per `PROVIDER_PROBE_INTERVAL` seconds, keeping latency and health fresh.

All 7 known mirrors share the same aoneroom backend — the failover is pure resilience, not multiple catalogs.

### Catalog walk

Two-tier source for the index:

1. **Home ingest** — every subject appearing in `/home` under a category section gets tagged with `is_movie` / `is_tv` / `is_anime` based on the section name
2. **Keyword walk** — 36 keywords (a-z, 0-9), paginate until `hasMore: false` or `CATALOG_MAX_PAGES_PER_KEYWORD`, upsert every subject

After walking, a background enricher hits `/detail/{slug}` for every subject whose type is unknown and reads `subjectType` (1=movie, 2=series, 6=music video) plus `genre` (contains "anime" → `is_anime`). This is what gets movies from 44 to 400+ and TV from 60 to 300+.

The walk re-runs every 6 hours. Enrichment runs once after each walk. `POST /catalog/refresh` forces both manually.

### SRT → VTT

`/subtitles/{lang}` fetches the SRT, strips numeric index lines, converts `,` timestamp separators to `.`, prepends `WEBVTT`, collapses whitespace. Italics and other SRT tags survive.

---

## Project layout

```
.
├── main.py
├── config.py
├── state.py
├── providers.py
├── upstream.py
├── storage.py
├── helpers.py
├── catalog_worker.py
├── dashboard.py
├── routes.py
├── static/
│   └── app.html
├── requirements.txt
├── verify.py
├── Dockerfile
├── docker-compose.yml
├── README.md
├── CONTRIBUTING.md
├── .env.example
└── .gitignore
```

---

## Verification

`verify.py` runs a live integration pass against a running server. 27 checks, chains real requests — pulls a slug from `/home`, feeds `/detail`, `/api/stream`, `/captions`, `/proxy`, `/subtitles`. Also exercises `/providers`, `/api/hosts`, catalog sorts and filters.

```bash
# server must be running
python verify.py

# or against a remote host
python verify.py https://your-host
```

Exits non-zero on any failure. Drops straight into CI or a pre-commit hook.

---

## Environment

See `.env.example` for the full list with defaults. Key variables:

| Variable | Default | Notes |
|----------|---------|-------|
| `PORT` | `8000` | Listen port |
| `RELOAD` | `0` | `1` enables autoreload (dev) |
| `REQUEST_TIMEOUT` | `25` | Upstream timeout, seconds |
| `CATALOG_DB` | `catalog.db` | SQLite path |
| `CATALOG_REFRESH_INTERVAL` | `21600` | Walk re-run interval (6h) |
| `CATALOG_MAX_PAGES_PER_KEYWORD` | `50` | Bump to 200 for deeper keyword coverage |
| `ENRICH_RATE_LIMIT` | `0.2` | Seconds between `/detail` calls during enrichment |
| `MOVIEBOX_HOST` | `moviebox.ph` | Primary provider |
| `MOVIEBOX_HOSTS` | 7 mirrors | Comma-separated |
| `PROVIDER_PROBE_INTERVAL` | `60` | Seconds between probe ticks |
| `CIRCUIT_BREAKER_THRESHOLD` | `3` | Failures before host opens |
| `CIRCUIT_BREAKER_SECONDS` | `60` | How long host stays open |

No config file. `set -a && source .env && set +a && python main.py` or `docker compose up`.

---

## Deployment notes

- **Behind a reverse proxy** — forward the full path. Routes assume `/` as root.
- **Healthcheck** — point your orchestrator at `/health`.
- **Long-running** — tokens are per-process, per-host. Multiple workers each acquire their own.
- **Rate limits** — upstream is unauthenticated for guests. Heavy polling will get throttled.
- **Persist `catalog.db`** — mount it as a volume or the walk restarts on every container rebuild.
- **ToS** — you're calling a third party's private API. Behave accordingly.

---

## Troubleshooting

### `/home` returns 500

Check `/health` and `/providers`. If a helper module is broken, verify.py step 1 fails on the same route. If all providers are up but `/home` fails, the issue is local — check server logs.

### Catalog shows `total: 0`

The walk either hasn't run yet or crashed. `POST /catalog/refresh` and watch `/catalog/stats` — `walk_state.running` should flip to `true` within a second.

### `?type=movie` returns fewer results than expected

Enrichment hasn't finished. Only home-ingested subjects have type tags until enrichment runs. Watch `untyped_pending_enrichment` in `/catalog/stats` — when it hits 0, everything has a type.

### Stream proxy returns 502

Upstream CDN might be blocking the referer. Check `/api/stream/{sid}` first — if sources are present, the URLs are live and the proxy should work. If empty, the episode may be gated or mid-upload.

### Search misses a known title

Upstream `/subject/search` is relevance-ranked, not exhaustive. `Matrix` isn't in the top 200 for `m`, so no walk keyword catches it. Fix: `POST /catalog/add?slug=<known-slug>` — the detail endpoint always works.

### Frontend `/app` 404

`static/app.html` missing. Confirm the file exists in the repo. `Dockerfile` copies `static/` explicitly.

---

## Changelog

**v2.7.0** — module split (10 files), provider failover with per-host circuit breakers, multi-host detection, `/providers` + `/providers/probe` + `/providers/reset` endpoints.

**v2.6.0** — multi-host probe, `?host=` on every BFF endpoint, `source_host` column in catalog.

**v2.4.1** — type enrichment from `/detail`, `keyword_stats` telemetry, `POST /catalog/add`, `?mode=enrich` refresh.

**v2.4.0** — indexed catalog (`/catalog`, `/catalog/stats`, `/catalog/refresh`), SQLite storage, background walk + enrichment.

**v2.3.3** — re-added `/` dashboard route; `/api/tabs/variants` confirms `/subject/filter` requires auth as guest.

**v2.3.2** — catalog home-fallback (when filter returns sentinel), `/sections`, `/browse/{section}`.

**v2.3.1** — `/search/suggest` honesty (text-only upstream), `total_sentinel` flag, `?tab=` override, `/api/tabs`.

**v2.3.0** — shared `httpx.AsyncClient`, async-locked token init, retry with backoff, `/home/all`, `/api/stream/{sid}/proxy`, `/api/stream/{sid}/subtitles/{lang}`, Docker setup.

---

Not affiliated with moviebox.ph. Provided as-is. You are responsible for how you use it.
