"""Picks: films like the ones marked loved, ranked with their IMDb rating.

Each filled-in film is described by its TMDb keywords, IMDb genres, director,
writers, top-billed cast, original language and decade. The similarity of a
candidate to a marked film is

    0.50 keywords   tf-idf cosine (a keyword few films share counts for more)
    0.22 genres     weighted overlap of the IMDb genres
    0.12 people     same director, writer or lead actors
    0.08 language   same original language
    0.08 era        release years close together

and is scaled so each marked film's closest match scores 1. A marked film
weighs 1.0 if loved, 0.5 if on the watchlist, 0.4 if seen and fine and 0.2 if
only seen; "not for me" pulls look-alikes down. TMDb's own "recommended" list
for a loved film adds a small bonus. The final score is

    0.6 x taste + 0.4 x quality

where quality is the IMDb rating weighted by its number of votes (a 9.0 from
2,000 votes counts for less than an 8.6 from 900,000).
"""

import json
import math
import threading
import time

import catalog

WEIGHT = {2: 1.0, 1: 0.4, None: 0.2, -1: -1.0}   # seen verdicts
WATCHLIST_WEIGHT = 0.5
CHANNELS = {"kw": 0.50, "genre": 0.22, "people": 0.12, "lang": 0.08, "era": 0.08}
COMMON = 0.2              # keywords on more than this share of films are skipped in the index
PRIOR_VOTES, PRIOR_RATING = 10000, 6.8
MAX_LOVED_RECS = 12       # loved films whose TMDb lists are fetched
INDIAN = {"hi", "ta", "te", "ml", "kn", "bn", "mr", "pa", "gu", "or", "as"}

# TMDb keywords that say nothing about the film, near-duplicates folded into one,
# and tone words (TMDb tags moods too), which count for half.
KW_DROP = {"duringcreditsstinger", "aftercreditsstinger", "woman director", "sequel", "remake", "3d",
           "based on novel or book", "based on young adult novel", "based on short story", "based on play or musical",
           "based on comic", "based on manga", "based on video game", "live action remake", "anime",
           "female protagonist", "male protagonist", "black and white", "silent film", "3d animation"}
KW_SAME = {}
for canon, words in {
    "true story": ["based on true story", "biography", "based on real person", "historical figure", "true story",
                   "based on real events", "biographical", "real life", "based on memoir or autobiography"],
    "lgbt": ["gay theme", "male homosexuality", "homophobia", "lgbt interest", "gay", "homosexuality", "lesbian",
             "lesbian relationship", "gay relationship", "gay interest", "queer", "lgbt"],
    "world war ii": ["second world war", "ww2", "world war 2"],
    "code breaking": ["cryptography", "codebreaker", "enigma machine", "cryptanalysis", "code"],
    "genius": ["math genius", "prodigy", "child prodigy", "gifted"],
    "espionage": ["spy", "secret agent", "spying"],
    "cia": ["central intelligence agency (cia)"],
    "time travel": ["time machine", "time traveler"],
}.items():
    for w in words:
        KW_SAME[w] = canon
TONES = {"amused", "suspenseful", "intense", "excited", "hilarious", "dramatic", "bold", "admiring", "aggressive",
         "awestruck", "audacious", "playful", "ambiguous", "romantic", "inspirational", "absurd", "cautionary",
         "provocative", "complicated", "sincere", "tense", "critical", "hopeful", "sad", "depressing", "angry",
         "defiant", "comforting", "frustrated", "informative", "sentimental", "celebratory", "whimsical",
         "nostalgic", "euphoric", "assertive", "anxious", "appreciative", "compassionate", "empathetic",
         "introspective", "reflective", "philosophical", "powerful", "cheerful", "joyful", "horrified", "disgusted",
         "suspicious", "tragic", "witty", "adoring", "approving", "candid", "earnest", "factual", "harsh", "hostile",
         "melodramatic", "mocking", "mysterious", "thoughtful", "gloomy", "lighthearted", "touching", "vindictive",
         "enchant", "exhilarated", "dignified", "callous", "baffled", "arrogant", "pessimistic", "optimistic"}


def keywords(raw):
    out = []
    for k in json.loads(raw or "[]"):
        k = KW_SAME.get(k.lower(), k.lower())
        if k not in KW_DROP and k not in out:
            out.append(k)
    return out


_lock = threading.Lock()
_index = {"version": None, "built": 0}
_picks = {"key": None, "ranked": []}
version = {"catalog": 0, "marks": 0}
_urgent = threading.Event()     # rebuild the index on the next request, not after the 5-minute wait


def changed(what, urgent=False):
    version[what] += 1
    if urgent:
        _urgent.set()


def quality(rating, votes):
    if not rating:
        return 0.0
    votes = votes or 0
    bayes = (votes * rating + PRIOR_VOTES * PRIOR_RATING) / (votes + PRIOR_VOTES)
    return max(0.0, min(1.0, (bayes - 6.3) / 2.4))


def _films():
    rows = catalog.db().execute("""SELECT id, title, year, genres, rating, votes, tmdb, lang,
        keywords, directors, writers, cast_ FROM movies WHERE filled > 0""").fetchall()
    films = {}
    for r in rows:
        films[r["id"]] = {
            "id": r["id"], "title": r["title"], "year": r["year"], "rating": r["rating"], "votes": r["votes"],
            "tmdb": r["tmdb"], "lang": r["lang"],
            "genres": [g for g in (r["genres"] or "").split(",") if g],
            "kw": keywords(r["keywords"]),
            "dir": json.loads(r["directors"] or "[]"), "wri": json.loads(r["writers"] or "[]"),
            "cast": json.loads(r["cast_"] or "[]")[:5],
        }
    return films


def index():
    """Keyword vectors and an inverted index, rebuilt when the catalogue changed (at most every 5 min)."""
    with _lock:
        if _index["version"] == version["catalog"] or (_index["version"] is not None and not _urgent.is_set()
                                                       and time.time() - _index["built"] < 300):
            return _index
        _urgent.clear()
        films = _films()
        n = max(len(films), 1)
        df, gdf = {}, {}
        for f in films.values():
            for k in set(f["kw"]):
                df[k] = df.get(k, 0) + 1
            for g in f["genres"]:
                gdf[g] = gdf.get(g, 0) + 1
        idf = {k: math.log(n / c) * (0.5 if k in TONES else 1.0) for k, c in df.items() if c >= 2}
        gidf = {g: 1.0 + math.log(n / c) for g, c in gdf.items()}
        postings = {}
        for f in films.values():
            vec = {k: idf[k] for k in set(f["kw"]) if k in idf}
            norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
            f["vec"] = {k: w / norm for k, w in vec.items()}
            for k, w in f["vec"].items():
                if df[k] <= COMMON * n:
                    postings.setdefault(k, []).append((f["id"], w))
            f["people"] = set(f["dir"]) | set(f["wri"]) | set(f["cast"])
        for f in films.values():
            for name in f["people"]:
                postings.setdefault("@" + name, []).append((f["id"], 0.0))
        _index.update(version=version["catalog"], built=time.time(), films=films, idf=idf, gidf=gidf,
                      postings=postings)
        return _index


def similarity(idx, a, b):
    """Per-channel similarity of film a to film b, plus the shared things that explain it."""
    kw = sum(w * b["vec"].get(k, 0.0) for k, w in a["vec"].items())
    ga, gb = set(a["genres"]), set(b["genres"])
    gw = lambda gs: sum(idx["gidf"].get(g, 1.0) for g in gs)
    genre = gw(ga & gb) / gw(ga | gb) if ga | gb else 0.0
    people = min(1.0, 0.6 * bool(set(a["dir"]) & set(b["dir"])) + 0.3 * bool(set(a["wri"]) & set(b["wri"]))
                 + 0.2 * len(set(a["cast"]) & set(b["cast"])))
    lang = 1.0 if a["lang"] and a["lang"] == b["lang"] else 0.0
    era = max(0.0, 1 - abs((a["year"] or 0) - (b["year"] or 0)) / 30) if a["year"] and b["year"] else 0.0
    parts = {"kw": kw, "genre": genre, "people": people, "lang": lang, "era": era}
    return sum(CHANNELS[c] * v for c, v in parts.items())


def shared(idx, seed, film, limit=3):
    out = [f"dir. {d}" for d in seed["dir"] if d in film["dir"]]
    out += [c for c in seed["cast"] if c in film["cast"]][:2]
    kws = sorted((k for k in seed["vec"] if k in film["vec"]), key=lambda k: -idx["idf"].get(k, 0))
    kws = [k for k in kws if k not in TONES] + [k for k in kws if k in TONES]
    out += kws
    if len(out) < 2:
        out += [g for g in seed["genres"] if g in film["genres"]]
    return out[:limit]


def candidates(idx, seed, limit=600):
    """The films sharing the most keyword weight with the seed, plus any sharing a person."""
    dot = {}
    for k, w in seed["vec"].items():
        for i, v in idx["postings"].get(k, ()):
            dot[i] = dot.get(i, 0.0) + w * v
    found = set(sorted(dot, key=dot.get, reverse=True)[:limit])
    for name in seed["people"]:
        found.update(i for i, _ in idx["postings"].get("@" + name, ()))
    found.discard(seed["id"])
    return found


def seeds():
    out = []
    for r in catalog.db().execute("SELECT id, list, verdict FROM marks WHERE list IN ('seen', 'watchlist')"):
        w = WATCHLIST_WEIGHT if r["list"] == "watchlist" else WEIGHT.get(r["verdict"], 0.2)
        out.append((r["id"], w, r["verdict"] == 2))
    return out


def picks():
    """Every candidate ranked: [(score, id, taste, seed id, shared things)], cached until something changes."""
    idx = index()
    key = (idx["version"], version["marks"])
    if _picks["key"] == key:
        return _picks["ranked"]
    films = idx["films"]
    marked = {r[0] for r in catalog.db().execute("SELECT id FROM marks")}
    sims = {}          # film -> list of (weighted, seed id)
    against = {}       # film -> strongest "not for me" likeness
    loved = []
    for seed_id, weight, is_loved in seeds():
        seed = films.get(seed_id)
        if not seed:
            continue
        if is_loved:
            loved.append(seed)
        scores = {c: similarity(idx, seed, films[c]) for c in candidates(idx, seed) if c not in marked}
        top = max(scores.values(), default=0) or 1.0
        for c, s in scores.items():
            if weight < 0:
                against[c] = max(against.get(c, 0.0), s / top)
            else:
                sims.setdefault(c, []).append((weight * s / top, seed_id))
    bonus = {}
    for seed in loved[-MAX_LOVED_RECS:]:
        try:
            for c in catalog.tmdb_recs(seed["tmdb"]):
                if c not in marked and c in films:
                    bonus[c] = min(0.12, bonus.get(c, 0.0) + 0.06)
                    sims.setdefault(c, []).append((0.25, seed["id"]))
        except Exception:
            pass                # Seerr unreachable: rank without TMDb's lists
    ranked = []
    for c, lst in sims.items():
        lst.sort(reverse=True)
        top3 = sum(w for w, _ in lst[:3]) / 3
        taste = 0.7 * lst[0][0] + 0.3 * top3
        f = films[c]
        score = 0.6 * taste + 0.4 * quality(f["rating"], f["votes"]) + bonus.get(c, 0.0) - 0.3 * against.get(c, 0.0)
        ranked.append((score, c, taste, lst[0][1]))
    ranked.sort(reverse=True)
    ranked = [(s, c, t, seed, shared(idx, films[seed], films[c])) for s, c, t, seed in ranked[:1500]]
    _picks.update(key=key, ranked=ranked)
    return ranked


def like(film_id, limit=12):
    """Films most like one film, IMDb rating counted less than for the main picks."""
    idx = index()
    seed = idx["films"].get(film_id)
    if not seed:
        return []
    hidden = {r[0] for r in catalog.db().execute("SELECT id FROM marks WHERE list='hidden'")}
    scores = {c: similarity(idx, seed, idx["films"][c]) for c in candidates(idx, seed) if c not in hidden}
    top = max(scores.values(), default=0) or 1.0
    ranked = sorted(((0.75 * s / top + 0.25 * quality(idx["films"][c]["rating"], idx["films"][c]["votes"]), c)
                     for c, s in scores.items()), reverse=True)
    return [(c, shared(idx, seed, idx["films"][c])) for _, c in ranked[:limit]]
