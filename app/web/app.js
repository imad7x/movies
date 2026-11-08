"use strict";

// --- Helpers -----------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const IMG = "https://image.tmdb.org/t/p/";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg class="ico" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const langName = (() => {
  let names = null;
  try { names = new Intl.DisplayNames(["en"], { type: "language" }); } catch (e) { /* old browser */ }
  return (code) => { try { return names ? names.of(code) : code; } catch (e) { return code; } };
})();

function runtime(min) {
  if (!min) return "";
  const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h}h${m ? ` ${m}m` : ""}` : `${m}m`;
}
function votes(n) {
  if (!n) return "";
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}
function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(`movies.${key}`) || "null");
    localStorage.setItem(`movies.${key}`, JSON.stringify(value));
  } catch (e) { return null; }
}

async function get(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}
async function post(path, body) {
  const res = await fetch(path, {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Movies": "1" },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status}`);
  return data;
}

let toastTimer = null;
function toast(text, undo) {
  const el = $("#toast");
  el.innerHTML = `<span>${esc(text)}</span>${undo ? '<button type="button">Undo</button>' : ""}`;
  el.hidden = false;
  if (undo) $("button", el).onclick = () => { el.hidden = true; undo(); };
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, undo ? 6000 : 3000);
}

// --- State ---------------------------------------------------------------------
const LISTS = [
  ["top", "IMDb Top"], ["gems", "Hidden gems"], ["new", "New & good"], ["indian", "Indian cinema"],
  ["true", "True stories"], ["mind", "Mind-benders"], ["spies", "Spies & codebreakers"],
  ["heist", "Heists & cons"], ["courtroom", "Courtroom"], ["space", "Space"], ["", "Everything"],
];
const WHERE = [["any", "Anywhere"], ["now", "Watch now"], ["library", "In Jellyfin"], ["services", "My services"]];
const LANGS = [["any", "Any language"], ["en", "English"], ["hi", "Hindi"], ["indian", "Indian languages"], ["other", "Not English"]];
const S = {
  tab: "foryou",
  status: null,
  services: [],
  picks: Object.assign({ min: "7", where: "any", lang: "any", genre: "" }, store("picks") || {}),
  explore: Object.assign({ list: "top", genre: "", era: "", min: "", lang: "any", where: "any", sort: "", short: false, all: false, kw: "" },
    store("explore") || {}),
  watch: Object.assign({ where: "any", sort: "added" }, store("watch") || {}),
  dirty: new Set(),          // tabs to reload when shown again
  detail: null,
  pushed: 0,
  loved: 0,                  // how many films are marked loved ("Like X" is only worth saying with two or more)                 // history entries the open film added (closing goes back past all of them)
};
S.explore.kw = "";

// --- Cards -----------------------------------------------------------------------
function posterHTML(f, size = "w342") {
  return f.poster
    ? `<img src="${IMG}${size}${esc(f.poster)}" alt="" loading="lazy" decoding="async">`
    : `<span class="ph">${esc(f.title)}</span>`;
}
function pills(f) {
  const out = [];
  if (f.lib === "library") out.push(`<span class="pill lib">${icon("play")}Jellyfin</span>`);
  else if (f.lib === "requested") out.push('<span class="pill req">Requested</span>');
  if (f.mine && f.mine.length) out.push(`<span class="pill mine">${esc(f.mine[0])}</span>`);
  else if (!f.lib && f.stream && f.stream.length) out.push(`<span class="pill">${esc(f.stream[0])}</span>`);
  return out.slice(0, 2).join("");
}
function metaLine(f) {
  const bits = [f.year];
  if (f.lang && f.lang !== "en") bits.push(langName(f.lang));
  bits.push(runtime(f.runtime));
  if (f.genres && f.genres.length) bits.push(f.genres[0]);
  return bits.filter(Boolean).map(esc).join(" · ");
}

function cardEl(f, opts = {}) {
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.id = f.id;
  const why = f.why && f.why.seed_title && S.loved > 1
    ? `<div class="why">Like <b>${esc(f.why.seed_title)}</b>${f.why.shared.length ? ` · ${f.why.shared.map(esc).join(", ")}` : ""}</div>`
    : f.why && f.why.shared && f.why.shared.length ? `<div class="why">${f.why.shared.map(esc).join(", ")}</div>` : "";
  el.innerHTML = `
    <button class="poster" type="button" data-open aria-label="${esc(f.title)}">${posterHTML(f)}
      ${f.rating ? `<span class="badge-imdb">${icon("star")}${f.rating.toFixed(1)}</span>` : ""}
      <span class="avail">${pills(f)}</span></button>
    <h3><button type="button" data-open>${esc(f.title)}</button></h3>
    <div class="meta">${metaLine(f)}</div>
    ${opts.why === false ? "" : why}
    ${opts.actions === false ? "" : '<div class="acts-slot"></div>'}`;
  $$("[data-open]", el).forEach((b) => b.addEventListener("click", () => openDetail(f.id)));
  if (opts.actions !== false) renderActs(el, f, opts);
  return el;
}

function renderActs(el, f, opts) {
  const slot = $(".acts-slot", el);
  if (opts.mode === "seen") {
    slot.innerHTML = verdictHTML(f.verdict, false);
    $$(".verdicts button[data-v]", slot).forEach((b) => b.addEventListener("click", () => {
      const v = Number(b.dataset.v);
      if (v === f.verdict) return;
      setMark(f, "seen", v, { el, keep: true, after: () => S.dirty.add("seen") });
    }));
    return;
  }
  const inWatch = f.mark === "watchlist";
  slot.innerHTML = `<div class="acts">
      <button class="act${inWatch ? " on" : ""}" type="button" data-a="watch" title="${inWatch ? "On your watchlist" : "Add to watchlist"}" aria-label="${inWatch ? "Remove from watchlist" : "Add to watchlist"}">${icon(inWatch ? "check" : "plus")}</button>
      <button class="act" type="button" data-a="seen" title="Seen it" aria-label="Seen it">${icon("heart")}</button>
      <button class="act" type="button" data-a="hide" title="${opts.mode === "watchlist" ? "Remove" : "Not interested"}" aria-label="${opts.mode === "watchlist" ? "Remove from watchlist" : "Not interested"}">${icon("x")}</button>
    </div>`;
  $("[data-a=watch]", slot).addEventListener("click", () => {
    if (inWatch) setMark(f, "none", null, { el, remove: opts.mode === "watchlist", label: "Removed from watchlist" });
    else setMark(f, "watchlist", null, { el, keep: true, label: "Added to watchlist", after: () => renderActs(el, f, opts) });
  });
  $("[data-a=hide]", slot).addEventListener("click", () => {
    if (opts.mode === "watchlist") setMark(f, "none", null, { el, remove: true, label: "Removed from watchlist" });
    else setMark(f, "hidden", null, { el, remove: true, label: "Hidden. It won't be suggested again" });
  });
  $("[data-a=seen]", slot).addEventListener("click", () => {
    slot.innerHTML = verdictHTML(null, true);
    $$(".verdicts button[data-v]", slot).forEach((b) => b.addEventListener("click", () => {
      const v = Number(b.dataset.v);
      setMark(f, "seen", v, { el, remove: true, label: v === 2 ? "Loved. Picks will lean towards it" : "Marked seen" });
    }));
    $(".cancel", slot).addEventListener("click", () => renderActs(el, f, opts));
  });
}

function verdictHTML(current, cancel) {
  const b = (v, label, cls = "") => `<button type="button" data-v="${v}" class="${cls}${current === v ? " on" : ""}">${label}</button>`;
  return `<div class="verdicts">${b(2, "Loved it", "love")}${b(1, "Fine")}${b(-1, "Not for me")}
    ${cancel ? '<button type="button" class="cancel">Cancel</button>' : ""}</div>`;
}

async function setMark(f, list, verdict, opts = {}) {
  const before = { list: f.mark || "none", verdict: f.verdict ?? null };
  try {
    await post("/api/mark", { id: f.id, list, verdict });
  } catch (e) {
    toast(`Couldn't save: ${e.message}`);
    return;
  }
  f.mark = list === "none" ? null : list;
  f.verdict = list === "seen" ? verdict : null;
  if (opts.remove && opts.el) {
    opts.el.classList.add("gone");
    setTimeout(() => opts.el.remove(), 250);
  }
  if (opts.after) opts.after();
  ["foryou", "explore", "watchlist", "seen"].filter((t) => t !== S.tab).forEach((t) => S.dirty.add(t));
  if (list === "seen" && verdict === 2) S.dirty.add("foryou");
  refreshCounts();
  if (opts.label) {
    toast(opts.label, async () => {
      await post("/api/mark", { id: f.id, list: before.list, verdict: before.verdict }).catch(() => {});
      f.mark = before.list === "none" ? null : before.list;
      f.verdict = before.verdict;
      refreshCounts();
      ["foryou", "explore", "watchlist", "seen"].forEach((t) => S.dirty.add(t));
      showTab(S.tab, true);
    });
  }
}

// "Show more" pages through the list the server froze on the first load (its "snap"),
// skips films already on screen, and ignores answers to loads that a newer one replaced.
const pagers = {};
async function page(kind, url, params, more) {
  const p = pagers[kind] || (pagers[kind] = { seq: 0, snap: null, next: 0, busy: false, shown: new Set() });
  if (more && (p.busy || !p.snap)) return null;
  if (!more) Object.assign(p, { seq: p.seq + 1, snap: null, next: 0, shown: new Set() });
  const seq = p.seq;
  p.busy = true;
  const q = new URLSearchParams({ limit: 30, ...params, offset: p.next });
  if (more) q.set("snap", p.snap);
  let data;
  try {
    data = await get(`${url}?${q}`);
  } catch (e) {
    if (seq === p.seq) { p.busy = false; throw e; }
    return null;
  }
  if (seq !== p.seq) return null;
  p.busy = false;
  p.snap = data.snap;
  p.next = data.next;
  data.fresh = data.items.filter((f) => !p.shown.has(f.id));
  data.fresh.forEach((f) => p.shown.add(f.id));
  return data;
}

function spinner(grid) { grid.innerHTML = '<span class="spin" aria-label="Loading"></span>'; }
function empty(grid, title, text) { grid.innerHTML = `<div class="empty"><b>${esc(title)}</b>${esc(text)}</div>`; }

// --- Filters -------------------------------------------------------------------------
function selectHTML(key, options, value, label) {
  return `<select data-k="${key}" aria-label="${esc(label)}" class="${value && value !== "any" ? "set" : ""}">${options
    .map(([v, t]) => `<option value="${esc(v)}"${String(v) === String(value) ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
}
function genreOptions(first) {
  return [["", first], ...((S.status && S.status.genres) || []).map((g) => [g, g])];
}
function bindFilters(root, state, key, reload) {
  $$("select", root).forEach((s) => s.addEventListener("change", () => {
    state[s.dataset.k] = s.value;
    store(key, state);
    reload();
  }));
  $$("input[type=checkbox]", root).forEach((c) => c.addEventListener("change", () => {
    state[c.dataset.k] = c.checked;
    store(key, state);
    reload();
  }));
}

// --- For you -----------------------------------------------------------------------------
function picksFilters() {
  const f = S.picks;
  const root = $("#picks-filters");
  root.innerHTML = [
    selectHTML("min", [["6", "IMDb 6+"], ["7", "IMDb 7+"], ["7.5", "IMDb 7.5+"], ["8", "IMDb 8+"]], f.min, "Minimum IMDb rating"),
    selectHTML("where", WHERE, f.where, "Where"),
    selectHTML("lang", LANGS, f.lang, "Language"),
    selectHTML("genre", genreOptions("Any genre"), f.genre, "Genre"),
  ].join("");
  bindFilters(root, f, "picks", () => loadPicks());
}

async function loadPicks(more = false) {
  const grid = $("#picks-grid");
  if (!more) spinner(grid);
  const f = S.picks;
  let data;
  try {
    data = await page("picks", "/api/picks", { min: f.min, where: f.where, lang: f.lang, genre: f.genre }, more);
  } catch (e) {
    if (more) toast(`Couldn't load more: ${e.message}`); else empty(grid, "Couldn't load picks", e.message);
    return;
  }
  if (!data) return;
  S.loved = data.loved.length;
  renderLoved(data.loved);
  if (!more) grid.innerHTML = "";
  data.fresh.forEach((film) => grid.appendChild(cardEl(film, { mode: "picks" })));
  $("#picks-more").hidden = data.next >= data.total;
  const note = $("#picks-note");
  if (!data.loved.length) {
    empty(grid, "Tell it what you love", "Add a few films you loved and the picks appear here.");
    note.textContent = "";
  } else if (!data.total) {
    const svc = f.where === "services" && !S.services.length ? " Pick your streaming services in Settings first." : "";
    empty(grid, "Nothing matches these filters", `Try a lower IMDb minimum or "Anywhere".${svc}`);
    note.textContent = "";
  } else {
    note.textContent = `${data.total.toLocaleString()} films, best match first`;
  }
}

function renderLoved(loved) {
  const row = $("#loved-row");
  row.innerHTML = "";
  loved.forEach((f) => {
    const el = document.createElement("div");
    el.className = "loved-item";
    el.innerHTML = `${f.poster ? `<img src="${IMG}w92${esc(f.poster)}" alt="">` : '<span class="ph"></span>'}
      <button class="t" type="button">${esc(f.title)}<span class="y">${esc(f.year || "")}</span></button>
      <button class="rm" type="button" aria-label="Remove ${esc(f.title)} from loved">${icon("x")}</button>`;
    $(".t", el).addEventListener("click", () => openDetail(f.id));
    $(".rm", el).addEventListener("click", async () => {
      await post("/api/mark", { id: f.id, list: "seen", verdict: 1 }).catch(() => {});
      toast(`${f.title} is now "seen, fine"`, async () => {
        await post("/api/mark", { id: f.id, list: "seen", verdict: 2 }).catch(() => {});
        loadPicks();
      });
      loadPicks();
      S.dirty.add("seen");
    });
    row.appendChild(el);
  });
  $("#loved-hint").hidden = loved.length >= 5;
}

// --- Explore ---------------------------------------------------------------------------------
function exploreLists() {
  const root = $("#explore-lists");
  root.innerHTML = LISTS.map(([k, t]) => `<button type="button" role="radio" class="chip${S.explore.list === k && !S.explore.kw ? " on" : ""}"
    aria-checked="${S.explore.list === k}" data-list="${k}">${esc(t)}</button>`).join("");
  $$(".chip", root).forEach((c) => c.addEventListener("click", () => {
    S.explore.list = c.dataset.list;
    S.explore.kw = "";
    store("explore", S.explore);
    exploreLists();
    exploreFilters();
    loadExplore();
  }));
}
function exploreFilters() {
  const f = S.explore;
  const root = $("#explore-filters");
  const eras = [["", "Any time"], ...((S.status && S.status.eras) || []).map((e) => [e, e === "older" ? "Before 1980" : e])];
  root.innerHTML = [
    f.kw ? `<button class="chip on" type="button" data-kw>${esc(f.kw)} ${icon("x")}</button>` : "",
    selectHTML("genre", genreOptions("Any genre"), f.genre, "Genre"),
    selectHTML("era", eras, f.era, "When"),
    selectHTML("min", [["", "Any rating"], ["6", "IMDb 6+"], ["6.5", "IMDb 6.5+"], ["7", "IMDb 7+"], ["7.5", "IMDb 7.5+"], ["8", "IMDb 8+"], ["8.5", "IMDb 8.5+"]], f.min, "Minimum IMDb rating"),
    selectHTML("lang", LANGS, f.lang, "Language"),
    selectHTML("where", WHERE, f.where, "Where"),
    selectHTML("sort", [["", "Default order"], ["imdb", "IMDb top (weighted)"], ["rating", "Highest rating"], ["votes", "Most voted"], ["new", "Newest"]], f.sort, "Sort"),
    `<label class="toggle"><input type="checkbox" data-k="short"${f.short ? " checked" : ""}>Under 2 h</label>`,
    `<label class="toggle"><input type="checkbox" data-k="all"${f.all ? " checked" : ""}>Little-known too</label>`,
  ].join("");
  const kw = $("[data-kw]", root);
  if (kw) kw.addEventListener("click", () => { f.kw = ""; exploreLists(); exploreFilters(); loadExplore(); });
  bindFilters(root, f, "explore", () => { exploreFilters(); loadExplore(); });
}

async function loadExplore(more = false) {
  const grid = $("#explore-grid");
  if (!more) spinner(grid);
  const f = S.explore;
  const params = {
    list: f.kw ? "" : f.list, genre: f.genre, era: f.era, min: f.min, lang: f.lang, where: f.where, sort: f.sort,
    short: f.short ? "1" : "", all: f.all ? "1" : "", kw: f.kw,
  };
  let data;
  try {
    data = await page("explore", "/api/explore", params, more);
  } catch (e) {
    if (more) toast(`Couldn't load more: ${e.message}`); else empty(grid, "Couldn't load", e.message);
    return;
  }
  if (!data) return;
  if (!more) grid.innerHTML = "";
  data.fresh.forEach((film) => grid.appendChild(cardEl(film, { mode: "explore" })));
  $("#explore-more").hidden = !data.more;
  if (!pagers.explore.shown.size) empty(grid, "Nothing matches these filters", "Loosen a filter or tick \"Little-known too\".");
  const notes = {
    top: "Most loved on IMDb, 25,000+ votes, rating weighted by votes",
    gems: "Rated 7.5+ on IMDb by fewer than 40,000 people",
    new: "Released in the last two years, IMDb 7+",
    indian: "Indian-language films, IMDb 7+ with 5,000+ votes",
  };
  $("#explore-note").textContent = f.kw ? `Films tagged "${f.kw}"` : notes[f.list] || "";
}

// --- Watchlist and seen ------------------------------------------------------------------------
async function loadWatchlist() {
  const root = $("#watch-filters");
  root.innerHTML = selectHTML("where", WHERE, S.watch.where, "Where") +
    selectHTML("sort", [["added", "Recently added"], ["rating", "IMDb rating"], ["short", "Shortest first"]], S.watch.sort, "Sort");
  bindFilters(root, S.watch, "watch", loadWatchlist);
  const grid = $("#watch-grid");
  spinner(grid);
  let data;
  try { data = await get("/api/marks?list=watchlist"); } catch (e) { empty(grid, "Couldn't load", e.message); return; }
  const where = S.watch.where;
  let items = data.items.filter((f) => where === "any" || (where === "library" && f.lib === "library")
    || (where === "services" && f.mine.length) || (where === "now" && (f.lib === "library" || f.mine.length)));
  if (S.watch.sort === "rating") items.sort((a, b) => (b.rating || 0) - (a.rating || 0));
  if (S.watch.sort === "short") items.sort((a, b) => (a.runtime || 999) - (b.runtime || 999));
  grid.innerHTML = "";
  items.forEach((film) => grid.appendChild(cardEl(film, { mode: "watchlist" })));
  if (!data.items.length) empty(grid, "Your watchlist is empty", "Tap + on any film to save it here.");
  else if (!items.length) empty(grid, "Nothing on your watchlist is available that way", "Switch to \"Anywhere\".");
}

async function loadSeen() {
  const root = $("#seen-groups");
  root.innerHTML = '<span class="spin" aria-label="Loading"></span>';
  let seen, hidden;
  try {
    [seen, hidden] = await Promise.all([get("/api/marks?list=seen"), get("/api/marks?list=hidden")]);
  } catch (e) { root.innerHTML = `<div class="empty"><b>Couldn't load</b>${esc(e.message)}</div>`; return; }
  root.innerHTML = "";
  const groups = [
    [2, "Loved", "These steer the picks most"], [1, "Fine", ""], [-1, "Not for me", "Films like these are pushed down"],
    [null, "Seen, no verdict yet", "Tap a verdict to count it"],
  ];
  groups.forEach(([v, title, sub]) => {
    const items = seen.items.filter((f) => (f.verdict ?? null) === v);
    if (!items.length) return;
    const head = document.createElement("div");
    head.className = "group-head";
    head.innerHTML = `<h2>${esc(title)}</h2><span>${items.length}${sub ? ` · ${esc(sub)}` : ""}</span>`;
    const grid = document.createElement("div");
    grid.className = "grid";
    items.forEach((film) => grid.appendChild(cardEl(film, { mode: "seen" })));
    root.append(head, grid);
  });
  if (hidden.items.length) {
    const box = document.createElement("details");
    box.innerHTML = `<summary class="group-head"><h2>Not interested</h2><span>${hidden.items.length} · never suggested</span></summary>`;
    const grid = document.createElement("div");
    grid.className = "grid";
    hidden.items.forEach((film) => {
      const el = cardEl(film, { actions: false });
      const b = document.createElement("button");
      b.className = "btn small";
      b.type = "button";
      b.style.marginTop = "8px";
      b.textContent = "Show again";
      b.addEventListener("click", () => setMark(film, "none", null, { el, remove: true, label: "It can be suggested again" }));
      el.appendChild(b);
      grid.appendChild(el);
    });
    box.appendChild(grid);
    root.appendChild(box);
  }
  if (!seen.items.length && !hidden.items.length) {
    root.innerHTML = '<div class="empty"><b>Nothing marked yet</b>Tap the heart on a film you\'ve seen.</div>';
  }
}

async function refreshCounts() {
  try {
    S.status = await get("/api/status");
    const m = S.status.marks || {};
    $("#count-watchlist").textContent = m.watchlist ? m.watchlist : "";
    $("#count-seen").textContent = m.seen ? m.seen : "";
  } catch (e) { /* offline: keep the old counts */ }
}

// --- Search ----------------------------------------------------------------------------------------
let searchTimer = null;
function onSearch() {
  clearTimeout(searchTimer);
  const q = $("#search").value.trim();
  searchTimer = setTimeout(async () => {
    const view = $("#view-search");
    if (q.length < 2) { view.hidden = true; showTab(S.tab); return; }
    $$(".view").forEach((v) => { v.hidden = v !== view; });
    view.innerHTML = '<div class="grid"></div>';
    const grid = $(".grid", view);
    spinner(grid);
    try {
      const data = await get(`/api/search?q=${encodeURIComponent(q)}`);
      if ($("#search").value.trim() !== q) return;
      grid.innerHTML = "";
      data.items.forEach((film) => grid.appendChild(cardEl(film, { mode: "search" })));
      if (!data.items.length) empty(grid, "No film by that name", "Only films with 1,000+ IMDb votes are in the catalogue.");
    } catch (e) { empty(grid, "Search failed", e.message); }
  }, 220);
}

// --- Detail ------------------------------------------------------------------------------------------
async function openDetail(id, push = true) {
  const sheet = $("#detail");
  const body = $("#detail-body");
  if (push && location.hash !== `#/m/${id}`) {
    history.pushState({ detail: id }, "", `#/m/${id}`);
    S.pushed += 1;
  }
  S.detail = id;
  sheet.hidden = false;
  document.body.classList.add("locked");
  body.innerHTML = '<span class="spin" aria-label="Loading"></span>';
  body.scrollTop = 0;
  let f;
  try { f = await get(`/api/movie/${id}`); } catch (e) {
    body.innerHTML = `<div class="panel"><div class="empty"><b>Couldn't load this film</b>${esc(e.message)}</div></div>`;
    return;
  }
  if (S.detail !== id) return;
  renderDetail(f);
}

function renderDetail(f) {
  const body = $("#detail-body");
  const genres = (f.genres || []).join(", ");
  const meta = [f.cert, runtime(f.runtime), genres, f.lang && f.lang !== "en" ? langName(f.lang) : ""].filter(Boolean).map(esc).join(" · ");
  const mine = new Set(S.services);
  const svcTags = (list) => list.map((s) => `<span class="svc-tag${mine.has(s) ? " mine" : ""}">${esc(s)}</span>`).join("");
  const lib = f.lib === "library"
    ? `<a class="btn primary" href="${esc(f.play)}" target="_blank" rel="noopener">${icon("play")}Play in Jellyfin</a>`
    : f.lib === "requested" ? '<button class="btn" type="button" disabled>Requested, downloading</button>'
    : f.tmdb ? `<button class="btn" type="button" data-get>${icon("down")}Get it</button>` : "";
  const trailer = f.trailer ? `https://www.youtube.com/watch?v=${encodeURIComponent(f.trailer)}`
    : `https://www.youtube.com/results?search_query=${encodeURIComponent(`${f.title} ${f.year || ""} trailer`.replace(/\s+/g, " "))}`;
  const people = [["Director", f.directors], ["Writers", f.writers], ["Cast", f.cast]]
    .filter(([, v]) => v && v.length).map(([k, v]) => `<dt>${k}</dt><dd>${v.map(esc).join(", ")}</dd>`).join("");
  body.innerHTML = `
    <div class="hero">${f.backdrop ? `<img class="bd" src="${IMG}w1280${esc(f.backdrop)}" alt="">` : ""}
      <button class="icon-btn" type="button" data-close aria-label="Close">${icon("x")}</button></div>
    <div class="d-main">
      <div class="d-poster">${posterHTML(f, "w342")}</div>
      <div class="d-title">
        <h2>${esc(f.title)} <span>${esc(f.year || "")}</span></h2>
        ${f.original ? `<div class="orig">${esc(f.original)}</div>` : ""}
        <div class="meta">${meta}</div>
        <div class="score">
          ${f.rating ? `<a class="big" href="https://www.imdb.com/title/${esc(f.id)}/" target="_blank" rel="noopener" title="IMDb rating">${icon("star")}${f.rating.toFixed(1)}</a>` : ""}
          <span class="votes">${f.votes ? `${votes(f.votes)} IMDb votes` : ""}</span>
          <a class="btn small" href="${esc(trailer)}" target="_blank" rel="noopener">${icon("play")}Trailer</a>
        </div>
      </div>
    </div>
    <div class="d-sec"><div class="d-acts" id="d-acts"></div></div>
    ${f.overview ? `<div class="d-sec">${f.tagline ? `<p class="tagline">${esc(f.tagline)}</p>` : ""}<p class="overview">${esc(f.overview)}</p></div>` : ""}
    <div class="d-sec"><h3>Where to watch</h3><div class="where">
      <div class="row"><span>Jellyfin</span>${lib || '<span class="svc-tag">Not in the library</span>'}</div>
      ${f.stream && f.stream.length ? `<div class="row"><span>Streaming</span>${svcTags(f.stream)}</div>` : '<div class="row"><span>Streaming</span><span class="svc-tag">Not streaming in India</span></div>'}
      ${f.rent && f.rent.length ? `<div class="row"><span>Rent or buy</span>${svcTags(f.rent)}</div>` : ""}
    </div></div>
    ${people ? `<div class="d-sec"><dl class="people">${people}</dl></div>` : ""}
    ${f.keywords && f.keywords.length ? `<div class="d-sec"><h3>Tags</h3><div class="kw">${f.keywords.slice(0, 16).map((k) => `<button type="button" data-kw="${esc(k)}">${esc(k)}</button>`).join("")}</div></div>` : ""}
    <div class="d-sec"><div class="links">
      <a href="https://www.imdb.com/title/${esc(f.id)}/" target="_blank" rel="noopener">${icon("ext")}IMDb</a>
      ${f.watch_link ? `<a href="${esc(f.watch_link)}" target="_blank" rel="noopener">${icon("ext")}All ways to watch</a>` : ""}
    </div></div>
    ${f.like && f.like.length ? '<div class="d-sec"><h3>More like this</h3></div><div class="strip" id="d-like"></div>' : ""}
    <div class="d-end"></div>`;
  renderDetailActs(f);
  const strip = $("#d-like", body);
  if (strip) f.like.forEach((x) => strip.appendChild(cardEl(x, { actions: false, why: false })));
  $$("[data-close]", body).forEach((b) => b.addEventListener("click", closeDetail));
  $$("[data-kw]", body).forEach((b) => b.addEventListener("click", () => {
    S.explore.kw = b.dataset.kw;
    closeDetail(false);
    location.hash = "#/explore";
    exploreLists(); exploreFilters(); loadExplore();
  }));
  const getBtn = $("[data-get]", body);
  if (getBtn) {
    let armed = false;
    getBtn.addEventListener("click", async () => {
      if (!armed) {
        armed = true;
        getBtn.innerHTML = `${icon("down")}Tap again to download`;
        getBtn.classList.add("primary");
        setTimeout(() => { if (armed && getBtn.isConnected) { armed = false; getBtn.innerHTML = `${icon("down")}Get it`; getBtn.classList.remove("primary"); } }, 4000);
        return;
      }
      getBtn.disabled = true;
      getBtn.textContent = "Requesting…";
      try {
        await post("/api/request", { id: f.id });
        getBtn.textContent = "Requested, downloading";
        getBtn.classList.remove("primary");
        toast("Requested in Seerr. Radarr will fetch it");
        S.dirty.add(S.tab);
      } catch (e) {
        getBtn.disabled = false;
        getBtn.innerHTML = `${icon("down")}Get it`;
        toast(e.message);
      }
    });
  }
}

function renderDetailActs(f) {
  const root = $("#d-acts");
  if (!root) return;
  const inWatch = f.mark === "watchlist", seen = f.mark === "seen", hidden = f.mark === "hidden";
  root.innerHTML = `
    <button class="btn${inWatch ? " on" : ""}" type="button" data-a="watch">${icon(inWatch ? "check" : "plus")}${inWatch ? "On watchlist" : "Watchlist"}</button>
    ${seen ? verdictHTML(f.verdict, false).replace('class="verdicts"', 'class="verdicts" style="margin:0;flex:1;min-width:260px"')
      : `<button class="btn" type="button" data-a="seen">${icon("heart")}Seen it</button>`}
    <button class="btn${hidden ? " on" : ""}" type="button" data-a="hide">${icon("x")}${hidden ? "Hidden" : "Not interested"}</button>
    ${seen ? `<button class="btn small" type="button" data-a="unseen">Not seen</button>` : ""}`;
  const mark = (list, verdict, label) => setMark(f, list, verdict, { label, after: () => renderDetailActs(f) })
    .then(() => { ["foryou", "explore", "watchlist", "seen"].forEach((t) => S.dirty.add(t)); });
  const on = (sel, fn) => { const b = $(sel, root); if (b) b.addEventListener("click", fn); };
  on("[data-a=watch]", () => mark(inWatch ? "none" : "watchlist", null, inWatch ? "Removed from watchlist" : "Added to watchlist"));
  on("[data-a=hide]", () => mark(hidden ? "none" : "hidden", null, hidden ? "It can be suggested again" : "Hidden. It won't be suggested again"));
  on("[data-a=unseen]", () => mark("none", null, "Unmarked"));
  on("[data-a=seen]", () => {
    $("[data-a=seen]", root).outerHTML = verdictHTML(null, false).replace('class="verdicts"', 'class="verdicts" style="margin:0;flex:1;min-width:260px"');
    bindVerdicts();
  });
  const bindVerdicts = () => $$(".verdicts button[data-v]", root).forEach((b) => b.addEventListener("click", () => {
    const v = Number(b.dataset.v);
    mark("seen", v, v === 2 ? "Loved. Picks will lean towards it" : "Marked seen");
  }));
  bindVerdicts();
}

function closeDetail(back = true) {
  if (!S.detail) return;
  S.detail = null;
  $("#detail").hidden = true;
  document.body.classList.remove("locked");
  if (back !== false && S.pushed > 0) {
    const n = S.pushed;
    S.pushed = 0;
    history.go(-n);
  } else if (location.hash.startsWith("#/m/")) {
    S.pushed = 0;
    history.replaceState(null, "", `#/${S.tab}`);
  }
  if (S.dirty.has(S.tab)) showTab(S.tab, true);
}

// --- Loved sheet ---------------------------------------------------------------------------------------
let lovedChanged = false;
function openLoved() {
  lovedChanged = false;
  $("#loved-sheet").hidden = false;
  document.body.classList.add("locked");
  $("#pick-grid").innerHTML = "";
  $("#loved-search").value = "";
  loadPickGrid();
}
function closeSheet(id) {
  $(id).hidden = true;
  if ($("#detail").hidden && $("#loved-sheet").hidden && $("#settings").hidden) document.body.classList.remove("locked");
  if (id === "#loved-sheet" && lovedChanged) { loadPicks(); S.dirty.add("seen"); refreshCounts(); }
  if (id === "#settings") {
    S.dirty.add(S.tab);
    showTab(S.tab, true);
  }
}
async function loadPickGrid(query, more = false) {
  const grid = $("#pick-grid");
  let items;
  try {
    if (query) {
      pagers.pick = null;            // leaving the famous-films list: start it again afterwards
      items = (await get(`/api/search?q=${encodeURIComponent(query)}`)).items;
    } else {
      const data = await page("pick", "/api/explore", { sort: "votes", unseen: "0", limit: 48 }, more);
      if (!data) return;
      if (!more) grid.innerHTML = "";
      items = data.fresh;
    }
  } catch (e) { return; }
  if (query) grid.innerHTML = "";
  $("#pick-more").hidden = !!query;
  items.forEach((f) => {
    const b = document.createElement("button");
    const isLoved = () => f.mark === "seen" && f.verdict === 2;
    b.type = "button";
    b.className = `pick${isLoved() ? " on" : ""}`;
    b.innerHTML = `<span class="poster">${posterHTML(f, "w185")}</span><span class="tick">${icon("heart")}</span>
      <span class="t">${esc(f.title)} <span>${esc(f.year || "")}</span></span>`;
    b.setAttribute("aria-pressed", isLoved());
    b.addEventListener("click", async () => {
      const next = isLoved() ? { list: "none", verdict: null } : { list: "seen", verdict: 2 };
      try { await post("/api/mark", { id: f.id, ...next }); } catch (e) { toast(e.message); return; }
      f.mark = next.list === "none" ? null : "seen";
      f.verdict = next.verdict;
      b.classList.toggle("on", isLoved());
      b.setAttribute("aria-pressed", isLoved());
      lovedChanged = true;
    });
    grid.appendChild(b);
  });
}
async function pasteMatch() {
  const text = $("#paste-text").value;
  const out = $("#paste-result");
  if (!text.trim()) return;
  out.innerHTML = '<span class="spin" aria-label="Loading"></span>';
  let data;
  try { data = await post("/api/match", { text }); } catch (e) { out.textContent = e.message; return; }
  const found = data.items.filter((x) => x.film);
  out.innerHTML = `<div class="matches">${data.items.map((x, i) => x.film
    ? `<label class="match"><input type="checkbox" data-i="${i}" checked>${x.film.poster ? `<img src="${IMG}w92${esc(x.film.poster)}" alt="">` : '<span class="ph"></span>'}
        <span class="m">${esc(x.film.title)} (${esc(x.film.year || "")})<small>for "${esc(x.line)}"</small></span></label>`
    : `<div class="match miss">No match for "${esc(x.line)}"</div>`).join("")}</div>
    ${found.length ? `<p><button class="btn primary" type="button" id="paste-save">Mark ${found.length} as loved</button></p>` : ""}`;
  const save = $("#paste-save");
  if (save) save.addEventListener("click", async () => {
    const picked = $$("input[data-i]:checked", out).map((c) => data.items[Number(c.dataset.i)].film);
    save.disabled = true;
    for (const f of picked) await post("/api/mark", { id: f.id, list: "seen", verdict: 2 }).catch(() => {});
    lovedChanged = true;
    toast(`${picked.length} films marked loved`);
    $("#paste-text").value = "";
    out.innerHTML = "";
  });
}

// --- Settings --------------------------------------------------------------------------------------------
async function openSettings() {
  $("#settings").hidden = false;
  document.body.classList.add("locked");
  const [settings] = await Promise.all([get("/api/settings"), refreshCounts()]);
  S.services = settings.services;
  const list = $("#svc-list");
  list.innerHTML = settings.offered.map((s) => `<label><input type="checkbox" value="${esc(s.name)}"${settings.services.includes(s.name) ? " checked" : ""}>
    ${esc(s.name)} <small>${s.films.toLocaleString()}</small></label>`).join("") || '<p class="hint">Streaming info appears as the catalogue fills in.</p>';
  $$("input", list).forEach((c) => c.addEventListener("change", async () => {
    const picked = $$("input:checked", list).map((x) => x.value);
    try { S.services = (await post("/api/settings", { services: picked })).services; } catch (e) { toast(e.message); }
    ["foryou", "explore", "watchlist"].forEach((t) => S.dirty.add(t));
  }));
  const st = S.status;
  const pct = st.to_fill ? Math.round((100 * st.done) / st.to_fill) : 0;
  const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "not yet");
  const jobErr = Object.entries(st.jobs || {}).filter(([, j]) => j && !j.ok).map(([k, j]) => `<dt>${esc(k)}</dt><dd>failed: ${esc(j.error)}</dd>`).join("");
  $("#status").innerHTML = `
    <dt>Films</dt><dd>${st.films.toLocaleString()} with 1,000+ IMDb votes</dd>
    <dt>Details</dt><dd>${st.done.toLocaleString()} of ${st.to_fill.toLocaleString()} filled in (${pct}%)<div class="bar"><i style="width:${pct}%"></i></div></dd>
    <dt>In Jellyfin</dt><dd>${st.library.toLocaleString()} films</dd>
    <dt>IMDb ratings</dt><dd>updated ${esc(when(st.imdb_at))}, weekly</dd>${jobErr}`;
}

// --- Tabs and routing ------------------------------------------------------------------------------------------
const loaded = new Set();
function showTab(tab, force = false) {
  S.tab = tab;
  $("#view-search").hidden = true;
  $$(".view").forEach((v) => { v.hidden = v.id !== `view-${tab}`; });
  $$(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
  if (!force && loaded.has(tab) && !S.dirty.has(tab)) return;
  loaded.add(tab);
  S.dirty.delete(tab);
  if (tab === "foryou") { picksFilters(); loadPicks(); }
  if (tab === "explore") { exploreLists(); exploreFilters(); loadExplore(); }
  if (tab === "watchlist") loadWatchlist();
  if (tab === "seen") loadSeen();
}

function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const m = h.match(/^m\/(tt\d+)/);
  if (m) {
    if (!loaded.size) showTab(S.tab);
    if (S.detail !== m[1]) openDetail(m[1], false);
    return;
  }
  if (S.detail) closeDetail(false);
  const tab = ["foryou", "explore", "watchlist", "seen"].includes(h) ? h : "foryou";
  if ($("#search").value) { $("#search").value = ""; }
  showTab(tab);
}

async function init() {
  window.addEventListener("hashchange", route);
  window.addEventListener("popstate", () => {
    S.pushed = Math.max(0, S.pushed - 1);
    if (!location.hash.startsWith("#/m/") && S.detail) closeDetail(false);
  });
  $("#search").addEventListener("input", onSearch);
  $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); onSearch(); });
  $("#picks-more").addEventListener("click", () => loadPicks(true));
  $("#explore-more").addEventListener("click", () => loadExplore(true));
  $("#add-loved").addEventListener("click", openLoved);
  $("#open-settings").addEventListener("click", openSettings);
  $("#pick-more").addEventListener("click", () => loadPickGrid("", true));
  let lovedTimer = null;
  $("#loved-search").addEventListener("input", (e) => {
    clearTimeout(lovedTimer);
    const q = e.target.value.trim();
    lovedTimer = setTimeout(() => {
      if (q.length >= 2) loadPickGrid(q);
      else loadPickGrid();
    }, 220);
  });
  $("#paste-go").addEventListener("click", pasteMatch);
  $$(".seg button").forEach((b) => b.addEventListener("click", () => {
    $$(".seg button").forEach((x) => x.classList.toggle("on", x === b));
    $("#pane-pick").hidden = b.dataset.pane !== "pick";
    $("#pane-paste").hidden = b.dataset.pane !== "paste";
  }));
  $$("#loved-sheet [data-close]").forEach((b) => b.addEventListener("click", () => closeSheet("#loved-sheet")));
  $$("#settings [data-close]").forEach((b) => b.addEventListener("click", () => closeSheet("#settings")));
  $("#detail .sheet-scrim").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#settings").hidden) closeSheet("#settings");
    else if (!$("#loved-sheet").hidden) closeSheet("#loved-sheet");
    else if (S.detail) closeDetail();
  });
  await refreshCounts();
  try { S.services = (await get("/api/settings")).services; } catch (e) { /* none picked */ }
  route();
}

init();
