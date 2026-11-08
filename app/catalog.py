"""The film catalogue in SQLite: IMDb's own ratings, filled in with TMDb details through Seerr.

IMDb publishes its ratings and basic title data as daily files at
datasets.imdbws.com (free for personal use). Every film with at least
MIN_VOTES votes goes in with its year, runtime, IMDb genres, rating and vote
count; the files are fetched again every IMDB_EVERY seconds.

Seerr already has TMDb access, so the poster, plot, keywords, director,
writers, cast, original language, certificate, trailer and where a film
streams in REGION come from Seerr's API: one search for the IMDb id gives the
TMDb id, then one details call. Films are filled in most-voted first and
filled in again after DETAILS_DAYS, so streaming info stays current.

Seerr's media list says which films are in Jellyfin or requested; it is read
every LIBRARY_EVERY seconds. With JELLYFIN_API_KEY and JELLYFIN_USER set,
films that user has played in Jellyfin are marked seen (without a verdict).
"""

import gzip
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("catalog")

STATE_DIR = os.environ.get("STATE_DIR", "/state")
DB_PATH = os.environ.get("DB_PATH", os.path.join(STATE_DIR, "movies.db"))
SEERR_URL = os.environ.get("SEERR_URL", "http://192.168.0.10:5055").rstrip("/")
SEERR_API_KEY = os.environ.get("SEERR_API_KEY", "")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://192.168.0.10:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "")
REGION = os.environ.get("REGION", "IN")
MIN_VOTES = int(os.environ.get("MIN_VOTES", "1000"))            # films kept in the catalogue
ENRICH_MIN_VOTES = int(os.environ.get("ENRICH_MIN_VOTES", "3000"))  # filled in ahead of time
ENRICH_MIN_RATING = float(os.environ.get("ENRICH_MIN_RATING", "5.5"))
WORKERS = int(os.environ.get("ENRICH_WORKERS", "3"))
IMDB_FILES = ("title.ratings.tsv.gz", "title.basics.tsv.gz")
IMDB_BASE = "https://datasets.imdbws.com/"
IMDB_EVERY = 7 * 86400
DETAILS_DAYS = 45
NOT_FOUND_DAYS = 30
LIBRARY_EVERY = 600
RECS_DAYS = 14
WRITING_JOBS = {"Screenplay", "Writer", "Story", "Novel", "Author", "Book", "Original Story"}

_local = threading.local()
write_lock = threading.Lock()
state = {"imdb": None, "enrich": None, "library": None, "jellyfin": None}   # last run of each job


SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
  id TEXT PRIMARY KEY,          -- IMDb id, tt...
  title TEXT NOT NULL, original TEXT, year INTEGER, runtime INTEGER,
  genres TEXT NOT NULL DEFAULT '',  -- IMDb genres, comma-separated
  rating REAL, votes INTEGER,
  tmdb INTEGER, poster TEXT, backdrop TEXT, overview TEXT, tagline TEXT, lang TEXT, cert TEXT,
  keywords TEXT, directors TEXT, writers TEXT, cast_ TEXT, countries TEXT,  -- JSON lists
  stream TEXT, rent TEXT,       -- JSON lists of service names in REGION
  watch_link TEXT, trailer TEXT,
  filled REAL NOT NULL DEFAULT 0  -- unix time of the last TMDb fill, negative = not found on TMDb
);
CREATE INDEX IF NOT EXISTS movies_votes ON movies(votes);
CREATE INDEX IF NOT EXISTS movies_tmdb ON movies(tmdb);
CREATE TABLE IF NOT EXISTS marks (
  id TEXT PRIMARY KEY,          -- IMDb id
  list TEXT NOT NULL,           -- watchlist | seen | hidden
  verdict INTEGER,              -- seen: 2 loved, 1 fine, -1 not for me, NULL no verdict
  source TEXT NOT NULL DEFAULT 'me',
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS library (
  tmdb INTEGER PRIMARY KEY, imdb TEXT, status INTEGER, url TEXT
);
CREATE TABLE IF NOT EXISTS recs (tmdb INTEGER PRIMARY KEY, ids TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    """One connection per thread (a shared one returns garbage under concurrent requests)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with write_lock:
        db().executescript(SCHEMA)
        db().commit()


def meta_get(key, default=None):
    row = db().execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def meta_set(key, value):
    with write_lock:
        db().execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (key, json.dumps(value)))
        db().commit()


# --- IMDb ---------------------------------------------------------------------

def _download(name, dest):
    req = urllib.request.Request(IMDB_BASE + name, headers={"User-Agent": "movies-homelab"})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=120) as res, open(tmp, "wb") as f:
        while chunk := res.read(1 << 20):
            f.write(chunk)
    os.replace(tmp, dest)


def import_imdb():
    """Fetch IMDb's files and upsert every film with MIN_VOTES votes or more."""
    folder = os.path.join(STATE_DIR, "imdb")
    os.makedirs(folder, exist_ok=True)
    paths = {}
    for name in IMDB_FILES:
        paths[name] = os.path.join(folder, name)
        _download(name, paths[name])
    ratings = {}
    with gzip.open(paths["title.ratings.tsv.gz"], "rt", encoding="utf-8") as f:
        next(f)
        for line in f:
            tid, avg, votes = line.rstrip("\n").split("\t")
            if int(votes) >= MIN_VOTES:
                ratings[tid] = (float(avg), int(votes))
    rows = []
    with gzip.open(paths["title.basics.tsv.gz"], "rt", encoding="utf-8") as f:
        next(f)
        for line in f:
            tid = line[:line.index("\t")]
            if tid not in ratings:
                continue
            p = line.rstrip("\n").split("\t")
            if p[1] != "movie" or p[4] == "1":      # films only, no adult titles
                continue
            num = lambda v: int(v) if v.isdigit() else None
            genres = "" if p[8] == "\\N" else p[8]
            rows.append((tid, p[2], p[3] if p[3] != p[2] else None, num(p[5]), num(p[7]), genres, *ratings[tid]))
    with write_lock:
        conn = db()
        conn.executemany("""
            INSERT INTO movies(id, title, original, year, runtime, genres, rating, votes)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, original=excluded.original,
              year=excluded.year, runtime=COALESCE(excluded.runtime, movies.runtime),
              genres=excluded.genres, rating=excluded.rating, votes=excluded.votes""", rows)
        conn.commit()
    for path in paths.values():
        os.remove(path)
    meta_set("imdb_at", time.time())
    log.info("imdb: %d films with %d+ votes", len(rows), MIN_VOTES)
    return len(rows)


# --- Seerr --------------------------------------------------------------------

def seerr(path, method="GET", body=None, **params):
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SEERR_URL}/api/v1{path}" + (f"?{qs}" if qs else ""), data=data,
                                 method=method, headers={"X-Api-Key": SEERR_API_KEY,
                                                         "Accept": "application/json",
                                                         "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.load(res)


def tmdb_id(imdb):
    for r in seerr("/search", query=f"imdb:{imdb}", page=1).get("results") or []:
        if r.get("mediaType") == "movie":
            return r["id"]
    return None


def _names(items, limit):
    out = []
    for name in items:
        if name and name not in out:
            out.append(name)
    return out[:limit]


def _service(name):
    """'Netflix basic with Ads' and 'Amazon Prime Video with Ads' are the same subscription."""
    for suffix in (" with Ads", " Basic with Ads", " basic with Ads", " Standard with Ads"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name.strip()


def details_row(d):
    credits = d.get("credits") or {}
    crew = credits.get("crew") or []
    cast = sorted(credits.get("cast") or [], key=lambda c: c.get("order", 99))
    where = next((w for w in d.get("watchProviders") or [] if w.get("iso_3166_1") == REGION), {})
    stream = _names([_service(p["name"]) for k in ("flatrate", "free", "ads") for p in where.get(k) or []], 8)
    rent = _names([_service(p["name"]) for k in ("rent", "buy") for p in where.get(k) or []], 8)
    videos = [v for v in d.get("relatedVideos") or [] if v.get("site") == "YouTube" and v.get("type") == "Trailer"]
    cert = None
    for region in (REGION, "US"):
        for rel in (d.get("releases") or {}).get("results") or []:
            if rel.get("iso_3166_1") == region:
                cert = next((x["certification"].strip() for x in rel.get("release_dates") or []
                             if (x.get("certification") or "").strip()), None)
        if cert:
            break
    return {
        "tmdb": d.get("id"), "poster": d.get("posterPath"), "backdrop": d.get("backdropPath"),
        "overview": d.get("overview") or None, "tagline": d.get("tagline") or None,
        "lang": d.get("originalLanguage"), "cert": cert,
        "keywords": json.dumps(_names([k.get("name") for k in d.get("keywords") or []], 40)),
        "directors": json.dumps(_names([c.get("name") for c in crew if c.get("job") == "Director"], 3)),
        "writers": json.dumps(_names([c.get("name") for c in crew if c.get("job") in WRITING_JOBS], 4)),
        "cast_": json.dumps(_names([c.get("name") for c in cast], 8)),
        "countries": json.dumps([c.get("iso_3166_1") for c in d.get("productionCountries") or []]),
        "stream": json.dumps(stream), "rent": json.dumps(rent),
        "watch_link": where.get("link"), "trailer": videos[0]["key"] if videos else None,
    }


def fill(imdb, tmdb=None):
    """Fetch one film's TMDb details through Seerr and store them. Returns True if found."""
    tmdb = tmdb or tmdb_id(imdb)
    if not tmdb:
        with write_lock:
            db().execute("UPDATE movies SET filled=? WHERE id=?", (-time.time(), imdb))
            db().commit()
        return False
    row = details_row(seerr(f"/movie/{tmdb}"))
    cols = ", ".join(f"{k}=:{k}" for k in row)
    with write_lock:
        db().execute(f"UPDATE movies SET {cols}, filled=:filled WHERE id=:id",
                     {**row, "filled": time.time(), "id": imdb})
        db().commit()
    return True


def due(limit):
    now = time.time()
    return [r[0] for r in db().execute("""
        SELECT id FROM movies
        WHERE votes >= ? AND rating >= ?
          AND (filled = 0 OR (filled > 0 AND filled < ?) OR (filled < 0 AND -filled < ?))
        ORDER BY (filled = 0) DESC, votes DESC LIMIT ?""",
        (ENRICH_MIN_VOTES, ENRICH_MIN_RATING, now - DETAILS_DAYS * 86400, now - NOT_FOUND_DAYS * 86400, limit))]


def counts():
    row = db().execute("""SELECT COUNT(*), SUM(filled > 0),
        SUM(votes >= ? AND rating >= ?), SUM(votes >= ? AND rating >= ? AND filled != 0)
        FROM movies""", (ENRICH_MIN_VOTES, ENRICH_MIN_RATING, ENRICH_MIN_VOTES, ENRICH_MIN_RATING)).fetchone()
    return {"films": row[0] or 0, "filled": row[1] or 0, "to_fill": row[2] or 0, "done": row[3] or 0}


def _safe_fill(imdb):
    try:
        return fill(imdb)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            with write_lock:
                db().execute("UPDATE movies SET filled=? WHERE id=?", (-time.time(), imdb))
                db().commit()
            return False
        raise


def enrich_loop(on_change):
    pool = ThreadPoolExecutor(WORKERS)
    while True:
        batch = due(60)
        if not batch:
            state["enrich"] = {"at": time.time(), "ok": True}
            time.sleep(900)
            continue
        try:
            list(pool.map(_safe_fill, batch))
            state["enrich"] = {"at": time.time(), "ok": True}
            on_change()
        except Exception as err:            # Seerr down or TMDb unreachable: try again later
            log.warning("fill failed: %s", err)
            state["enrich"] = {"at": time.time(), "ok": False, "error": str(err)[:200]}
            time.sleep(120)


# --- Library (Seerr) and plays (Jellyfin) ---------------------------------------

def sync_library():
    rows, skip = [], 0
    while True:
        page = seerr("/media", take=100, skip=skip, sort="added")
        for m in page.get("results") or []:
            if m.get("mediaType") == "movie" and m.get("tmdbId"):
                rows.append((m["tmdbId"], m.get("imdbId"), m.get("status"), m.get("mediaUrl")))
        skip += 100
        if skip >= (page.get("pageInfo") or {}).get("results", 0):
            break
    with write_lock:
        conn = db()
        conn.execute("DELETE FROM library")
        conn.executemany("INSERT OR REPLACE INTO library(tmdb, imdb, status, url) VALUES(?, ?, ?, ?)", rows)
        conn.commit()
    return len(rows)


def jellyfin(path, **params):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{JELLYFIN_URL}{path}" + (f"?{qs}" if qs else ""),
                                 headers={"X-Emby-Token": JELLYFIN_API_KEY, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.load(res)


def sync_plays():
    """Mark films JELLYFIN_USER played in Jellyfin as seen, unless already marked."""
    users = [u for u in jellyfin("/Users") if u.get("Name", "").lower() == JELLYFIN_USER.lower()]
    if not users:
        raise RuntimeError(f"no Jellyfin user {JELLYFIN_USER!r}")
    items = jellyfin(f"/Users/{users[0]['Id']}/Items", IncludeItemTypes="Movie", Recursive="true",
                     Filters="IsPlayed", Fields="ProviderIds").get("Items") or []
    ids = [(i.get("ProviderIds") or {}).get("Imdb") for i in items]
    ids = [i for i in ids if i]
    now = time.time()
    with write_lock:
        conn = db()
        conn.executemany("INSERT OR IGNORE INTO marks(id, list, verdict, source, at) VALUES(?, 'seen', NULL, 'jellyfin', ?)",
                         [(i, now) for i in ids])
        conn.commit()
    return len(ids)


def library_loop(on_change):
    while True:
        try:
            state["library"] = {"at": time.time(), "ok": True, "films": sync_library()}
            on_change()
        except Exception as err:
            log.warning("library sync failed: %s", err)
            state["library"] = {"at": time.time(), "ok": False, "error": str(err)[:200]}
        if JELLYFIN_API_KEY and JELLYFIN_USER:
            try:
                state["jellyfin"] = {"at": time.time(), "ok": True, "played": sync_plays()}
                on_change()
            except Exception as err:
                log.warning("jellyfin plays failed: %s", err)
                state["jellyfin"] = {"at": time.time(), "ok": False, "error": str(err)[:200]}
        time.sleep(LIBRARY_EVERY)


def imdb_loop(on_change):
    while True:
        last = meta_get("imdb_at", 0)
        wait = last + IMDB_EVERY - time.time()
        if wait > 0:
            time.sleep(min(wait, 3600))
            continue
        try:
            state["imdb"] = {"at": time.time(), "ok": True, "films": import_imdb()}
            on_change()
        except Exception as err:
            log.warning("imdb import failed: %s", err)
            state["imdb"] = {"at": time.time(), "ok": False, "error": str(err)[:200]}
            time.sleep(1800)


# --- TMDb recommendations for the films marked loved ----------------------------

def tmdb_recs(tmdb):
    """TMDb's 'recommended' list for a film as IMDb ids in the catalogue (TMDb ids cached RECS_DAYS)."""
    row = db().execute("SELECT ids, at FROM recs WHERE tmdb=?", (tmdb,)).fetchone()
    if row and row["at"] > time.time() - RECS_DAYS * 86400:
        found = json.loads(row["ids"])
    else:
        found = []
        for page in (1, 2):
            data = seerr(f"/movie/{tmdb}/recommendations", page=page)
            found += [r["id"] for r in data.get("results") or [] if r.get("mediaType", "movie") == "movie"]
            if page >= (data.get("totalPages") or 1):
                break
        with write_lock:
            db().execute("INSERT OR REPLACE INTO recs(tmdb, ids, at) VALUES(?, ?, ?)",
                         (tmdb, json.dumps(found), time.time()))
            db().commit()
    if not found:
        return []
    marks = ",".join("?" * len(found))
    return [r[0] for r in db().execute(f"SELECT id FROM movies WHERE tmdb IN ({marks})", found)]
