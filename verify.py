"""
Live verification for MovieBox API Pro v2.4.0.
Includes new catalog index checks.
Run against a live server: python verify.py [base_url]
"""
import sys
import time
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TIMEOUT = 90


def hr(label):
    print(f"\n{'─' * 60}\n{label}\n{'─' * 60}")


def ok(cond, msg):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {msg}")
    return cond


def sample_items(items, label):
    print(f"    {label}: {len(items)} items")
    if items:
        it = items[0]
        print(f"    sample: name={str(it.get('name'))[:50]!r}")
        print(f"            slug={it.get('slug')}")
        print(f"            sid={it.get('subject_id')}")
        print(f"            poster={'YES' if it.get('poster_url') else 'NULL'}")
    return items


results = {"pass": 0, "fail": 0}


def record(passed):
    results["pass" if passed else "fail"] += 1


# ── 1. dashboard ────────────────────────────────────────────────────────────
hr("GET /  (dashboard)")
try:
    r = httpx.get(f"{BASE}/", timeout=TIMEOUT)
    is_html = r.status_code == 200 and "<html" in r.text.lower()
    record(ok(is_html, f"status {r.status_code}, html={'YES' if is_html else 'NO'}"))
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── 2. home ────────────────────────────────────────────────────────────────
hr("GET /home")
home_slug = home_sid = None
try:
    r = httpx.get(f"{BASE}/home", timeout=TIMEOUT)
    data = r.json()
    secs = data.get("sections", [])
    record(ok(r.status_code == 200 and len(secs) > 0, f"status {r.status_code}, {len(secs)} sections"))
    for s in secs[:4]:
        items = s.get("items", [])
        print(f"    [{s['section']}] {s['count']} items")
        if items and not home_slug:
            home_slug = items[0].get("slug")
            home_sid = items[0].get("subject_id")
            print(f"    picked: slug={home_slug} sid={home_sid}")
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── 3. catalog stats ───────────────────────────────────────────────────────
hr("GET /catalog/stats")
try:
    r = httpx.get(f"{BASE}/catalog/stats", timeout=TIMEOUT)
    data = r.json()
    total = data.get("total", 0)
    walk = data.get("walk_state", {})
    record(ok(r.status_code == 200 and "walk_state" in data,
              f"status {r.status_code}, total={total}, "
              f"walk_running={walk.get('running')}, "
              f"enriching={walk.get('enriching')}"))
    print(f"    types: movies={data.get('movies')} tv={data.get('tv')} anime={data.get('anime')} "
          f"untyped={data.get('untyped')} pending_enrich={data.get('untyped_pending_enrichment')}")
    st = data.get("subject_types", {})
    if st:
        print(f"    subject_type breakdown: {st}")
    ks = data.get("keyword_stats", {})
    if ks:
        cap_kws = [k for k, v in ks.items() if v.get("hit_cap")]
        print(f"    keywords walked: {len(ks)}/36, hit_cap: {cap_kws}")
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── 4. catalog (indexed) ───────────────────────────────────────────────────
hr("GET /catalog")
try:
    r = httpx.get(f"{BASE}/catalog", params={"per_page": 12}, timeout=TIMEOUT)
    data = r.json()
    items = data.get("items", [])
    total = data.get("total", 0)
    schema_ok = "total" in data and "items" in data and "page" in data
    record(ok(r.status_code == 200 and schema_ok, f"status {r.status_code}, total={total}, items={len(items)}"))
    if items:
        sample_items(items, "catalog")
        typed = [i for i in items if i.get("types")]
        if typed:
            print(f"    typed items in sample: {len(typed)}/{len(items)}")
except Exception as e:
    record(ok(False, f"exception: {e}"))
    
# ── 5. catalog sorts ───────────────────────────────────────────────────────
hr("GET /catalog?sort=title  and  ?sort=rating")
for sort in ("title", "rating"):
    try:
        r = httpx.get(f"{BASE}/catalog", params={"sort": sort, "per_page": 3}, timeout=TIMEOUT)
        data = r.json()
        items = data.get("items", [])
        record(ok(r.status_code == 200, f"sort={sort}, status {r.status_code}, total={data.get('total')}"))
        for it in items[:2]:
            print(f"    {sort}: {str(it.get('name'))[:50]!r} rating={it.get('rating')}")
    except Exception as e:
        record(ok(False, f"exception: {e}"))

# ── 6. catalog filter by type ──────────────────────────────────────────────
hr("GET /catalog?type=movie  /  ?type=tv  /  ?type=anime")
for t in ("movie", "tv", "anime"):
    try:
        r = httpx.get(f"{BASE}/catalog", params={"type": t, "per_page": 3}, timeout=TIMEOUT)
        data = r.json()
        record(ok(r.status_code == 200, f"type={t}, status {r.status_code}, total={data.get('total')}"))
    except Exception as e:
        record(ok(False, f"exception: {e}"))

# ── 7. catalog text search ─────────────────────────────────────────────────
hr("GET /catalog?q=the")
try:
    r = httpx.get(f"{BASE}/catalog", params={"q": "the", "per_page": 3}, timeout=TIMEOUT)
    data = r.json()
    items = data.get("items", [])
    record(ok(r.status_code == 200, f"status {r.status_code}, matches={data.get('total')}"))
    sample_items(items, "q=the")
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── 8. catalog refresh POST ────────────────────────────────────────────────
hr("POST /catalog/refresh")
try:
    r = httpx.post(f"{BASE}/catalog/refresh", timeout=TIMEOUT)
    data = r.json()
    record(ok(r.status_code == 200 and "status" in data,
              f"status {r.status_code}, response={data.get('status')}"))
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── 9. home fallback catalogs ──────────────────────────────────────────────
for path in ("/movies", "/tv-series", "/animation"):
    hr(f"GET {path}")
    try:
        r = httpx.get(f"{BASE}{path}", timeout=TIMEOUT)
        data = r.json()
        items = data.get("items", [])
        record(ok(r.status_code == 200 and len(items) > 0,
                  f"status {r.status_code}, source={data.get('source')}, total={data.get('total')}"))
    except Exception as e:
        record(ok(False, f"exception: {e}"))

# ── 10. search ─────────────────────────────────────────────────────────────
hr("GET /search?q=matrix")
try:
    r = httpx.get(f"{BASE}/search", params={"q": "matrix"}, timeout=TIMEOUT)
    data = r.json()
    record(ok(r.status_code == 200, f"status {r.status_code}, total={data.get('total')}"))
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── 11. detail ─────────────────────────────────────────────────────────────
hr("GET /detail/{slug}")
if home_slug:
    try:
        r = httpx.get(f"{BASE}/detail/{home_slug}", timeout=TIMEOUT)
        data = r.json()
        inner = data.get("data", {}) or {}
        record(ok(r.status_code == 200 and bool(inner), f"status {r.status_code}, keys={list(inner.keys())[:6]}"))
    except Exception as e:
        record(ok(False, f"exception: {e}"))
else:
    record(ok(False, "no slug from /home"))

# ── 12. stream ─────────────────────────────────────────────────────────────
hr("GET /api/stream/{sid}")
if home_sid and home_slug:
    try:
        r = httpx.get(
            f"{BASE}/api/stream/{home_sid}",
            params={"detail_path": home_slug},
            timeout=TIMEOUT,
        )
        data = r.json()
        record(ok(r.status_code == 200, f"status {r.status_code}, has_resource={data.get('has_resource')}, sources={len(data.get('sources', []))}"))
    except Exception as e:
        record(ok(False, f"exception: {e}"))
else:
    record(ok(False, "no sid"))

# ── 13. proxy ──────────────────────────────────────────────────────────────
hr("GET /api/stream/{sid}/proxy  (Range)")
if home_sid and home_slug:
    try:
        with httpx.stream(
            "GET",
            f"{BASE}/api/stream/{home_sid}/proxy",
            params={"detail_path": home_slug, "res": "best"},
            headers={"Range": "bytes=0-1023"},
            timeout=TIMEOUT,
        ) as r:
            first = b""
            for chunk in r.iter_bytes(1024):
                first = chunk
                break
            record(ok(r.status_code in (200, 206) and len(first) > 0,
                      f"status {r.status_code}, {len(first)} bytes"))
    except Exception as e:
        record(ok(False, f"exception: {e}"))
else:
    record(ok(False, "no sid"))

# ── 14. health ─────────────────────────────────────────────────────────────
hr("GET /health")
try:
    r = httpx.get(f"{BASE}/health", timeout=TIMEOUT)
    data = r.json()
    record(ok(r.status_code == 200 and data.get("status") == "ok",
              f"version={data.get('version')} token_cached={data.get('token_cached')} "
              f"indexed={data.get('catalog_indexed')} walk_running={data.get('walk_running')}"))
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── 15. catalog enrichment refresh ─────────────────────────────────────────
hr("POST /catalog/refresh?mode=enrich")
try:
    r = httpx.post(f"{BASE}/catalog/refresh", params={"mode": "enrich"}, timeout=TIMEOUT)
    data = r.json()
    record(ok(r.status_code == 200 and "status" in data,
              f"status {r.status_code}, response={data.get('status')}"))
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── 16. catalog manual add (Matrix) ────────────────────────────────────────
hr("POST /catalog/add?slug=matrix-W1E5680Y2S5")
try:
    r = httpx.post(f"{BASE}/catalog/add", params={"slug": "matrix-W1E5680Y2S5"}, timeout=TIMEOUT)
    data = r.json()
    record(ok(r.status_code == 200 and data.get("status") == "added",
              f"status {r.status_code}, subject_type={data.get('subject_type')} title={data.get('title')!r}"))
except Exception as e:
    record(ok(False, f"exception: {e}"))

# ── summary ────────────────────────────────────────────────────────────────
hr("SUMMARY")
print(f"  passed: {results['pass']}")
print(f"  failed: {results['fail']}")
sys.exit(0 if results["fail"] == 0 else 1)