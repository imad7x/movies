"""Movies: what to watch next, ranked by IMDb rating and by the films marked loved.

  GET  /                      the page (web/)
  GET  /api/ping              health check
  GET  /api/status            catalogue size, fill progress, last run of each job
  GET  /api/picks             films like the ones marked loved   ?min= &where= &lang= &genre= &offset= &limit= &snap=
  GET  /api/explore           the catalogue with filters         ?list= &genre= &era= &min= &lang= &where=
                                                                  &kw= &short= &all= &sort= &offset= &limit= &snap=

Both lists answer with "snap", a token for the list's order at the first load, and
"next", the offset for the next page; passing them back pages through the same list
even while the catalogue fills in or films get marked.
  GET  /api/search?q=         films by title ("dune 2021" narrows to a year)
  GET  /api/movie/<imdb id>   one film in full, with "more like this"
  GET  /api/marks?list=       the watchlist, seen or hidden films
  GET  /api/settings          the streaming services offered in the catalogue and the ones picked
  POST /api/mark              {"id", "list": watchlist|seen|hidden|none, "verdict": 2|1|-1|null}
  POST /api/match             {"text"} one title per line -> the best catalogue match for each
  POST /api/request           {"id"} request the film in Seerr (Radarr downloads it)
  POST /api/settings          {"services": [...]}

A seen verdict is 2 loved it, 1 fine, -1 not for me. Loved films steer the picks most.

POSTs must carry the header "X-Movies: 1": a browser only lets another site
send a custom header after a CORS preflight, which this server never approves.
Every request must be addressed to an IP, localhost, or a name in
ALLOWED_HOSTS (closes DNS rebinding).
"""

import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import catalog
import recommend

PORT = int(os.environ.get("PORT", "8089"))
ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
ALLOWED_HOSTS = {name.strip().lower() for name in
                 os.environ.get("ALLOWED_HOSTS", "").split(",") if name.strip()}
SEED = [s for s in os.environ.get("SEED_LOVED", "tt2084970").split(",") if s]  # loved on first start
STATIC = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
          "style.css": "text/css; charset=utf-8", "icon.svg": "image/svg+xml",
          "manifest.webmanifest": "application/manifest+json",
          "icons/icon-192.png": "image/png", "icons/icon-512.png": "image/png"}
ID_RE = re.compile(r"^tt\d{5,10}$")
LISTS = {"watchlist", "seen", "hidden"}
LIBRARY, REQUESTED = (4, 5), (2, 3)    # Seerr media status: partly/fully available, pending/processing
KEYWORD_LISTS = {
    "mind": ["twist ending", "plot twist", "nonlinear timeline", "mind bending", "psychological thriller",
             "unreliable narrator", "time loop", "time travel", "puzzle", "mindfuck", "dream"],
    "spies": ["espionage", "spy", "code breaking", "cryptography", "cold war", "mi6", "cia", "double agent",
              "secret agent", "intelligence agency", "codebreaker"],
    "heist": ["heist", "con artist", "bank robbery", "robbery", "swindle", "con man"],
    "courtroom": ["courtroom", "trial", "lawyer", "courtroom drama", "legal drama", "jury"],
    "space": ["space", "outer space", "astronaut", "space travel", "space mission", "spaceship"],
}
GENRES = ["Action", "Adventure", "Animation", "Biography", "Comedy", "Crime", "Documentary", "Drama",
          "Family", "Fantasy", "History", "Horror", "Music", "Musical", "Mystery", "Romance", "Sci-Fi",
          "Sport", "Thriller", "War", "Western"]
ERAS = {"2020s": (2020, 2100), "2010s": (2010, 2019), "2000s": (2000, 2009), "1990s": (1990, 1999),
        "1980s": (1980, 1989), "older": (0, 1979)}
LANGS = {"en": ["en"], "hi": ["hi"], "indian": sorted(recommend.INDIAN)}
BAYES = f"((m.votes * m.rating + {recommend.PRIOR_VOTES} * {recommend.PRIOR_RATING}) / (m.votes + {recommend.PRIOR_VOTES}))"
SORTS = {"imdb": f"{BAYES} DESC", "rating": "m.rating DESC, m.votes DESC", "votes": "m.votes DESC",
         "new": "m.year DESC, m.rating DESC"}

SNAP_KEEP = 24          # lists remembered for "Show more"
SNAP_MAX = 1500         # films kept per Explore list

log = logging.getLogger("movies")
mark_lock = threading.Lock()
snap_lock = threading.Lock()
snaps = OrderedDict()


def host_allowed(header):
    host = (header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if not host:
        return False
    if host == "localhost" or host in ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


_services = []


def services():
    return _services


SELECT = """SELECT m.*, l.status AS lib_status, l.url AS lib_url, k.list AS mark, k.verdict AS verdict
            FROM movies m LEFT JOIN library l ON l.tmdb = m.tmdb LEFT JOIN marks k ON k.id = m.id"""


def card(r, why=None):
    stream = json.loads(r["stream"] or "[]")
    lib = "library" if r["lib_status"] in LIBRARY else "requested" if r["lib_status"] in REQUESTED else None
    out = {"id": r["id"], "title": r["title"], "year": r["year"], "runtime": r["runtime"],
           "genres": [g for g in (r["genres"] or "").split(",") if g], "rating": r["rating"], "votes": r["votes"],
           "poster": r["poster"], "lang": r["lang"], "cert": r["cert"], "stream": stream,
           "mine": [s for s in stream if s in services()], "lib": lib,
           "play": r["lib_url"] if lib == "library" else None,
           "mark": r["mark"], "verdict": r["verdict"], "filled": r["filled"] > 0}
    if why:
        out["why"] = why
    return out


def where_ok(c, where):
    if where == "library":
        return c["lib"] == "library"
    if where == "services":
        return bool(c["mine"])
    if where == "now":
        return c["lib"] == "library" or bool(c["mine"])
    return True


def lang_ok(lang, choice):
    if choice in ("", "any"):
        return True
    if choice == "other":
        return bool(lang) and lang != "en"
    return lang in LANGS.get(choice, [choice])


def num(q, key, default, cast=float):
    try:
        return cast(q.get(key, [default])[0])
    except (TypeError, ValueError):
        return default


def arg(q, key, default=""):
    return (q.get(key, [default])[0] or default).strip()


def snap_put(entries):
    """Keep a list's order so "Show more" pages through the same list even while the
    catalogue fills in and films get marked (offsets into a live ranking repeat films)."""
    token = secrets.token_hex(8)
    with snap_lock:
        snaps[token] = entries
        while len(snaps) > SNAP_KEEP:
            snaps.popitem(last=False)
    return token


def snap_get(token):
    with snap_lock:
        return snaps.get(token) if token else None


def rows_for(ids):
    rows = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in catalog.db().execute(f"{SELECT} WHERE m.id IN ({','.join('?' * len(chunk))})", chunk):
            rows[r["id"]] = r
    return rows


def get_picks(q):
    minimum, where, lang, genre = num(q, "min", 7.0), arg(q, "where", "any"), arg(q, "lang", "any"), arg(q, "genre")
    offset, limit = num(q, "offset", 0, int), min(num(q, "limit", 30, int), 60)
    token = arg(q, "snap")
    snap = snap_get(token)
    if snap is None:
        ranked = recommend.picks()
        films = recommend.index()["films"]
        rows = rows_for([c for _, c, _, _, _ in ranked])
        snap = []
        for score, c, taste, seed, shared in ranked:
            r = rows.get(c)
            if not r or r["mark"] or (r["rating"] or 0) < minimum or not lang_ok(r["lang"], lang):
                continue
            if genre and genre not in (r["genres"] or "").split(","):
                continue
            why = {"seed": seed, "seed_title": films[seed]["title"], "shared": shared,
                   "match": round(min(1.0, taste) * 100)}
            if where_ok(card(r), where):
                snap.append((c, why))
        token = snap_put(snap)
    page = snap[offset:offset + limit]
    rows = rows_for([c for c, _ in page])
    items = [card(rows[c], why) for c, why in page if c in rows and not rows[c]["mark"]]
    loved = [dict(id=r["id"], title=r["title"], year=r["year"], poster=r["poster"]) for r in catalog.db().execute(
        "SELECT m.id, m.title, m.year, m.poster FROM marks k JOIN movies m ON m.id = k.id "
        "WHERE k.list = 'seen' AND k.verdict = 2 ORDER BY k.at")]
    return {"total": len(snap), "items": items, "next": offset + len(page), "snap": token, "loved": loved}


def get_explore(q):
    conds, params = ["1=1"], []
    preset = arg(q, "list")
    minimum = num(q, "min", 0.0)
    votes = 1000 if arg(q, "all") == "1" else 3000
    sort = arg(q, "sort") or "imdb"
    if preset == "top":
        votes = max(votes, 25000)
    elif preset == "gems":
        conds.append("m.votes <= 40000")
        minimum, sort = max(minimum, 7.5), arg(q, "sort") or "rating"
    elif preset == "new":
        conds.append("m.year >= ?")
        params.append(date.today().year - 2)
        minimum, sort = max(minimum, 7.0), arg(q, "sort") or "rating"
    elif preset == "indian":
        conds.append(f"m.lang IN ({','.join('?' * len(recommend.INDIAN))})")
        params += sorted(recommend.INDIAN)
        votes, minimum = max(votes, 5000), max(minimum, 7.0)
    elif preset == "true":
        conds.append("m.genres LIKE '%Biography%'")
    elif preset in KEYWORD_LISTS:
        words = KEYWORD_LISTS[preset]
        conds.append("(" + " OR ".join(["lower(m.keywords) LIKE ?"] * len(words)) + ")")
        params += [f'%"{w}"%' for w in words]
    conds += ["m.votes >= ?", "m.rating >= ?"]
    params += [votes, minimum]
    for g in [g for g in arg(q, "genre").split(",") if g in GENRES]:
        conds.append("(',' || m.genres || ',') LIKE ?")
        params.append(f"%,{g},%")
    era = ERAS.get(arg(q, "era"))
    if era:
        conds.append("m.year BETWEEN ? AND ?")
        params += list(era)
    kw = arg(q, "kw").lower()
    if kw:
        conds.append("lower(m.keywords) LIKE ?")
        params.append(f'%"{kw}"%')
    lang = arg(q, "lang", "any")
    if lang == "other":
        conds.append("m.lang IS NOT NULL AND m.lang != 'en'")
    elif lang in LANGS:
        conds.append(f"m.lang IN ({','.join('?' * len(LANGS[lang]))})")
        params += LANGS[lang]
    if arg(q, "short") == "1":
        conds.append("m.runtime <= 120")
    if arg(q, "unseen", "1") == "1":
        conds.append("(k.list IS NULL OR k.list = 'watchlist')")
    where = arg(q, "where", "any")
    if where == "library":
        conds.append(f"l.status IN {LIBRARY}")
    elif where in ("services", "now"):
        mine = services()
        svc = " OR ".join(["m.stream LIKE ?"] * len(mine)) or "0"
        conds.append(f"(({svc}) OR l.status IN {LIBRARY})" if where == "now" else f"({svc})")
        params += [f'%"{s}"%' for s in mine]
    offset, limit = num(q, "offset", 0, int), min(num(q, "limit", 30, int), 60)
    token = arg(q, "snap")
    snap = snap_get(token)
    if snap is None:
        sql = (f"SELECT m.id FROM movies m LEFT JOIN library l ON l.tmdb = m.tmdb LEFT JOIN marks k ON k.id = m.id "
               f"WHERE {' AND '.join(conds)} ORDER BY {SORTS.get(sort, SORTS['imdb'])}, m.id LIMIT {SNAP_MAX}")
        snap = [r[0] for r in catalog.db().execute(sql, params)]
        token = snap_put(snap)
    page = snap[offset:offset + limit]
    rows = rows_for(page)
    unseen = arg(q, "unseen", "1") == "1"
    items = [card(rows[i]) for i in page if i in rows and not (unseen and rows[i]["mark"] in ("seen", "hidden"))]
    return {"items": items, "next": offset + len(page), "more": offset + len(page) < len(snap),
            "snap": token, "total": len(snap)}


def get_search(q):
    text = arg(q, "q").lower()
    m = re.search(r"\b(19\d\d|20\d\d)\s*$", text)
    year = int(m.group(1)) if m else None
    title = text[:m.start()].strip() if m and text[:m.start()].strip() else text
    if len(title) < 2:
        return {"items": []}
    conds = ["(lower(m.title) LIKE ? OR lower(m.original) LIKE ?)"]
    params = [f"%{title}%", f"%{title}%"]
    if year:
        conds.append("m.year = ?")
        params.append(year)
    rows = catalog.db().execute(
        f"{SELECT} WHERE {' AND '.join(conds)} ORDER BY (lower(m.title) = ?) DESC, "
        "(lower(m.title) LIKE ?) DESC, m.votes DESC LIMIT 20", params + [title, f"{title}%"]).fetchall()
    return {"items": [card(r) for r in rows]}


def get_movie(film_id):
    row = catalog.db().execute(f"{SELECT} WHERE m.id = ?", (film_id,)).fetchone()
    if not row:
        return None
    if row["filled"] == 0 or (row["filled"] > 0 and row["filled"] < time.time() - catalog.DETAILS_DAYS * 86400):
        try:
            if catalog.fill(film_id):
                recommend.changed("catalog", urgent=row["mark"] is not None)
            row = catalog.db().execute(f"{SELECT} WHERE m.id = ?", (film_id,)).fetchone()
        except Exception as err:
            log.warning("fill %s: %s", film_id, err)
    out = card(row)
    out.update(original=row["original"], overview=row["overview"], tagline=row["tagline"],
               backdrop=row["backdrop"], keywords=json.loads(row["keywords"] or "[]"),
               directors=json.loads(row["directors"] or "[]"), writers=json.loads(row["writers"] or "[]"),
               cast=json.loads(row["cast_"] or "[]"), rent=json.loads(row["rent"] or "[]"),
               watch_link=row["watch_link"], trailer=row["trailer"], tmdb=row["tmdb"],
               lib_status=row["lib_status"])
    like = recommend.like(film_id)
    if like:
        ids = [c for c, _ in like]
        rows = {r["id"]: r for r in catalog.db().execute(f"{SELECT} WHERE m.id IN ({','.join('?' * len(ids))})", ids)}
        out["like"] = [card(rows[c], {"shared": s}) for c, s in like if c in rows]
    return out


def get_marks(q):
    lst = arg(q, "list", "watchlist")
    if lst not in LISTS:
        return {"items": []}
    rows = catalog.db().execute(f"{SELECT} WHERE k.list = ? ORDER BY "
                                "CASE WHEN k.list='seen' THEN -COALESCE(k.verdict, 0) END, k.at DESC", (lst,))
    return {"items": [card(r) for r in rows]}


def get_settings():
    counts = {}
    for (stream,) in catalog.db().execute("SELECT stream FROM movies WHERE filled > 0 AND stream != '[]'"):
        for s in json.loads(stream):
            counts[s] = counts.get(s, 0) + 1
    offered = [{"name": s, "films": n} for s, n in sorted(counts.items(), key=lambda x: -x[1]) if n >= 20]
    return {"services": services(), "offered": offered, "region": catalog.REGION}


def get_status():
    c = catalog.counts()
    lib = catalog.db().execute(f"SELECT COUNT(*) FROM library WHERE status IN {LIBRARY}").fetchone()[0]
    marks = dict(catalog.db().execute("SELECT list, COUNT(*) FROM marks GROUP BY list").fetchall())
    return {**c, "library": lib, "jobs": catalog.state, "imdb_at": catalog.meta_get("imdb_at"),
            "marks": marks, "genres": GENRES, "eras": list(ERAS)}


def post_mark(body):
    film_id, lst, verdict = str(body.get("id", "")), body.get("list"), body.get("verdict")
    if not ID_RE.match(film_id) or lst not in LISTS | {"none"} or verdict not in (None, 2, 1, -1):
        return 400, {"error": "need id, list (watchlist, seen, hidden or none) and verdict (2, 1, -1 or null)"}
    if not catalog.db().execute("SELECT 1 FROM movies WHERE id = ?", (film_id,)).fetchone():
        return 404, {"error": "not in the catalogue"}
    with catalog.write_lock:
        conn = catalog.db()
        if lst == "none":
            conn.execute("DELETE FROM marks WHERE id = ?", (film_id,))
        else:
            conn.execute("INSERT INTO marks(id, list, verdict, source, at) VALUES(?, ?, ?, 'me', ?) "
                         "ON CONFLICT(id) DO UPDATE SET list=excluded.list, verdict=excluded.verdict, "
                         "source='me', at=excluded.at",
                         (film_id, lst, verdict if lst == "seen" else None, time.time()))
        conn.commit()
    recommend.changed("marks")
    if lst == "seen" and verdict == 2:      # a new loved film: fill it in now so it steers the picks
        row = catalog.db().execute("SELECT filled FROM movies WHERE id = ?", (film_id,)).fetchone()
        if row and row["filled"] <= 0:
            try:
                catalog.fill(film_id)
                recommend.changed("catalog", urgent=True)
            except Exception as err:
                log.warning("fill %s: %s", film_id, err)
        threading.Thread(target=prefetch_recs, args=(film_id,), daemon=True).start()
    return 200, {"ok": True, "id": film_id, "mark": None if lst == "none" else lst,
                 "verdict": verdict if lst == "seen" else None}


def prefetch_recs(film_id):
    """Fetch TMDb's list for a newly loved film now, so the next picks don't wait for it."""
    row = catalog.db().execute("SELECT tmdb FROM movies WHERE id = ?", (film_id,)).fetchone()
    if row and row["tmdb"]:
        try:
            catalog.tmdb_recs(row["tmdb"])
        except Exception as err:
            log.warning("recs %s: %s", film_id, err)


def post_match(body):
    lines = [l.strip() for l in str(body.get("text", "")).splitlines() if l.strip()][:60]
    out = []
    for line in lines:
        clean = re.sub(r"[\(\)\[\]]", " ", line)
        found = get_search({"q": [re.sub(r"\s+", " ", clean)]})["items"]
        out.append({"line": line, "film": found[0] if found else None})
    return 200, {"items": out}


def post_request(body):
    film_id = str(body.get("id", ""))
    row = catalog.db().execute("SELECT tmdb FROM movies WHERE id = ?", (film_id,)).fetchone() if ID_RE.match(film_id) else None
    if not row or not row["tmdb"]:
        return 404, {"error": "film not found on TMDb"}
    try:
        catalog.seerr("/request", method="POST", body={"mediaType": "movie", "mediaId": row["tmdb"]})
    except Exception as err:
        detail = getattr(err, "read", lambda: b"")()[:300].decode(errors="replace")
        log.warning("request %s: %s %s", film_id, err, detail)
        return 502, {"error": f"Seerr said no: {detail or err}"}
    try:
        catalog.sync_library()
    except Exception:
        pass
    return 200, {"ok": True}


def post_settings(body):
    picked = body.get("services")
    if not isinstance(picked, list) or len(picked) > 40 or not all(isinstance(s, str) and len(s) < 80 for s in picked):
        return 400, {"error": "need services: [names]"}
    catalog.meta_set("services", sorted(set(picked)))
    _services[:] = sorted(set(picked))
    return 200, {"services": services()}


class Handler(BaseHTTPRequestHandler):
    server_version = "movies"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _is_lan_origin(self):
        origin = self.headers.get("Origin", "")
        if not origin:
            return False
        if origin == "null":
            return host_allowed(self.headers.get("Host"))
        try:
            host = urlparse(origin).hostname or ""
            if host == "localhost":
                return True
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def send(self, code, body, ctype="application/json", cache="no-cache"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self._is_lan_origin():
            origin = self.headers.get("Origin", "")
            self.send_header("Access-Control-Allow-Origin", origin if origin and origin != "null" else "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Movies")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data: https://image.tmdb.org; object-src 'none'; "
                             "base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):
        if not host_allowed(self.headers.get("Host")):
            return self.send(421, {"error": "unknown host"})
        if self._is_lan_origin():
            self.send(204, b"", ctype="text/plain")
        else:
            self.send(403, {"error": "CORS not allowed"})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not host_allowed(self.headers.get("Host")):
            return self.send(421, {"error": "unknown host"})
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    return self.send(200, f.read(), STATIC["index.html"])
            if path.lstrip("/") in STATIC:
                name = path.lstrip("/")
                with open(os.path.join(WEB, *name.split("/")), "rb") as f:
                    return self.send(200, f.read(), STATIC[name])
            if path == "/api/ping":
                return self.send(200, {"ok": True})
            routes = {"/api/status": get_status, "/api/settings": get_settings}
            if path in routes:
                return self.send(200, routes[path]())
            routes = {"/api/picks": get_picks, "/api/explore": get_explore, "/api/search": get_search,
                      "/api/marks": get_marks}
            if path in routes:
                return self.send(200, routes[path](q))
            m = re.fullmatch(r"/api/movie/(tt\d{5,10})", path)
            if m:
                film = get_movie(m.group(1))
                return self.send(200, film) if film else self.send(404, {"error": "not in the catalogue"})
            return self.send(404, {"error": "not found"})
        except Exception:
            log.exception("GET %s", self.path)
            return self.send(500, {"error": "server error"})

    def do_POST(self):
        if not host_allowed(self.headers.get("Host")):
            return self.send(421, {"error": "unknown host"})
        if self.headers.get("X-Movies") != "1":
            return self.send(403, {"error": "missing X-Movies header"})
        routes = {"/api/mark": post_mark, "/api/match": post_match, "/api/request": post_request,
                  "/api/settings": post_settings}
        if self.path not in routes:
            return self.send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 32768:
                return self.send(400, {"error": "bad body"})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                return self.send(400, {"error": "bad body"})
        except ValueError:
            return self.send(400, {"error": "bad body"})
        try:
            with mark_lock:
                code, out = routes[self.path](body)
            return self.send(code, out)
        except Exception:
            log.exception("POST %s", self.path)
            return self.send(500, {"error": "server error"})


def first_start():
    """Mark SEED_LOVED as loved once, so the picks have something to start from."""
    if catalog.meta_get("seeded"):
        return
    with catalog.write_lock:
        conn = catalog.db()
        conn.executemany("INSERT OR IGNORE INTO marks(id, list, verdict, source, at) VALUES(?, 'seen', 2, 'me', ?)",
                         [(s, time.time()) for s in SEED if ID_RE.match(s)])
        conn.commit()
    catalog.meta_set("seeded", True)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(name)s %(message)s")
    catalog.init()
    first_start()
    _services[:] = catalog.meta_get("services", [])
    if not catalog.SEERR_API_KEY:
        log.warning("SEERR_API_KEY not set: no posters, plots or streaming info")
    on_change = lambda: recommend.changed("catalog")
    if os.environ.get("JOBS", "on") == "off":       # testing against a copy of the database
        threading.Thread(target=recommend.index, daemon=True).start()
        log.info("serving on :%d (background jobs off)", PORT)
        return ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    threading.Thread(target=catalog.imdb_loop, args=(on_change,), daemon=True).start()
    threading.Thread(target=catalog.library_loop, args=(on_change,), daemon=True).start()
    if catalog.SEERR_API_KEY:
        threading.Thread(target=catalog.enrich_loop, args=(on_change,), daemon=True).start()
    threading.Thread(target=recommend.index, daemon=True).start()
    log.info("serving on :%d", PORT)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
