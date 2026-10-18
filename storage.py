import time
import sqlite3
import hashlib
import json
from config import (
    CATALOG_DB, DEFAULT_HOST,
)
import state


def init_schema():
    conn = state.write_conn
    conn.execute("""
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
            source_host TEXT DEFAULT 'moviebox.ph',
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keyword_stats (
            keyword TEXT PRIMARY KEY,
            pages_walked INTEGER DEFAULT 0,
            items_seen INTEGER DEFAULT 0,
            hit_cap INTEGER DEFAULT 0,
            total_per_upstream INTEGER,
            last_walked REAL
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(subjects)").fetchall()}
    if "subject_type" not in cols:
        conn.execute("ALTER TABLE subjects ADD COLUMN subject_type INTEGER")
    if "genre" not in cols:
        conn.execute("ALTER TABLE subjects ADD COLUMN genre TEXT")
    if "source_host" not in cols:
        conn.execute("ALTER TABLE subjects ADD COLUMN source_host TEXT DEFAULT 'moviebox.ph'")
        conn.execute("UPDATE subjects SET source_host = 'moviebox.ph' WHERE source_host IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON subjects(title)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rating ON subjects(rating)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_last_seen ON subjects(last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_movie ON subjects(is_movie)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tv ON subjects(is_tv)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_anime ON subjects(is_anime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stype ON subjects(subject_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_host ON subjects(source_host)")
    conn.commit()


def read(sql: str, params: tuple = ()) -> list:
    conn = sqlite3.connect(CATALOG_DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def read_scalar(sql: str, params: tuple = ()):
    conn = sqlite3.connect(CATALOG_DB)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def classify_section(title: str) -> tuple[int, int, int]:
    """Best-effort section → type mapping using substring keywords.
    Works across geo-shifted home feeds — 'Popular Movie' (Singapore) and
    'Cinema' (Mumbai) both → is_movie=1. Genre-named sections that don't
    match any keyword fall through to enrichment via /detail."""
    clean = title.lower().replace("🔥", "").replace("🆓", "").strip()
    is_movie = 1 if any(kw in clean for kw in (
        "movie", "cinema", "nollywood", "bollywood", "hollywood",
    )) else 0
    is_tv = 1 if any(kw in clean for kw in (
        "series", "tv", "show", "drama",
    )) else 0
    is_anime = 1 if any(kw in clean for kw in (
        "anime", "animation",
    )) else 0
    return (is_movie, is_tv, is_anime)


def upsert_subject(sub: dict, section: str | None = None, source_host: str = DEFAULT_HOST) -> bool:
    if state.write_conn is None:
        return False
    sid = str(sub.get("subjectId") or "")
    if not sid:
        return False
    now = time.time()
    is_movie = is_tv = is_anime = 0
    if section:
        is_movie, is_tv, is_anime = classify_section(section)
    try:
        state.write_conn.execute("""
            INSERT INTO subjects
                (subject_id, title, poster_url, slug, rating, year, badge,
                 is_movie, is_tv, is_anime, source_host, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            source_host,
            now, now,
        ))
        return True
    except Exception as e:
        print(f"upsert {sid} failed: {e}")
        return False


def hash_operating_list(ops: list) -> str:
    sig = [(op.get("type"), op.get("title")) for op in ops]
    return hashlib.sha1(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:16]