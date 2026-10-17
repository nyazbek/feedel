#!/usr/bin/env python3
"""
quietfeed — a distraction-free local reader for YouTube channels, X/Twitter accounts,
Substack / RSS feeds and arXiv.

Runs a tiny web server on http://localhost:8765 and shows one chronological list:
no recommendations, no autoplay, no Shorts, no "For you". Everything is fetched
by this script (not by your browser), and videos play in mpv, so browser-level
blockers like Cold Turkey never see a request to youtube.com or x.com.

Setup (Windows):
    winget install -e --id shinchiro.mpv
    pip install yt-dlp            (streaming through mpv, downloads, browser-session sync)
    python quietfeed.py

Then open the "following" tab in the page to import your subscriptions and follow
new channels, newsletters, arXiv categories and authors. Standard library only.
"""

import sys

# When mpv is pointed at the frozen quietfeed.exe as its "yt-dlp", it calls it with -J. Answer as yt-dlp.
if len(sys.argv) > 1 and any(a in ("-J", "-j", "--dump-single-json", "--dump-json") for a in sys.argv[1:]):
    import yt_dlp
    sys.exit(yt_dlp.main())

import base64, csv, hashlib, html, io, json, os, re, shutil, sqlite3, subprocess, threading, time
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
WEB = os.path.join(getattr(sys, "_MEIPASS", HERE), "web")
DB_PATH = os.path.join(HERE, "quietfeed.db")
LEGACY_JSON = {k: os.path.join(HERE, k + ".json") for k in ("config", "state", "cache")}  # pre-0.3 files, migrated on first run
LOG_PATH = os.path.join(HERE, "quietfeed.log")
AVATAR_DIR = os.path.join(HERE, "cache", "avatars")
PORT = 8765
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
YT_HEADERS = {"Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+cb; SOCS=CAI"}
FEED_ACCEPT = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.5"}
KINDS = ("yt", "tw", "rss", "arxiv")

DEFAULT_CONFIG = {
    "youtube": {
        "channels": ["@veritasium", "@3blue1brown"],
        "min_minutes": 8,
        "hide_shorts": True,
    },
    "twitter": {
        "users": [],
        "nitter_instances": [
            "https://nitter.jaydenha.uk",
            "https://nitter.meowing.monster",
            "https://nitter.netbub.com",
            "https://xcancel.com",
        ],
    },
    "rss": {"feeds": []},                      # Substack and any other RSS/Atom feed
    "arxiv": {"queries": [], "max_results": 50},  # categories (cat:math.PR), authors (au:"Name"), keyword searches
    # titles matching any of these are hidden; plain text = case-insensitive substring, /…/ = regex
    "filters": {"block_words": []},
    # watch_clipboard: links you copy anywhere (WhatsApp, mail…) land in the inbox; clipboard_any widens it from
    # YouTube/X/arXiv links to every link. fallback_browser: where `--open` sends links quietfeed cannot show ("" = auto)
    "ui": {"hide_thumbnails": False, "calm_titles": True, "hide_seen": False, "watch_clipboard": False, "clipboard_any": False},
    "fallback_browser": "",
    # "browser": name of a browser signed in to YouTube whose subscriptions are re-read on every refresh (needs yt-dlp)
    "sync": {"browser": ""},
    # where the conversation lives: "share" posts an item and your note to this stream, as you, with your own API key
    "zulip": {"site": "", "email": "", "api_key": "", "stream": "quietfeed"},
    "player": ["mpv", "--ytdl-format=bestvideo[height<=1080]+bestaudio/best", "{url}"],
    "download_dir": os.path.join(HERE, "downloads"),
    "refresh_minutes": 30,
    "max_items": 400,
    "max_per_channel": 8,
    "keep_days": 90,  # items that have dropped out of their feed are kept this long; saved items are kept forever
}

# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def fetch(url, timeout=15, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def yt_page(url, timeout=20):
    return fetch(url, timeout, headers=YT_HEADERS).decode("utf-8", "ignore")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

def _no_redirect(url, method):
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
    try:
        r = opener.open(req, timeout=12)
        return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")
    except Exception:
        return 0, ""

def head_no_redirect(url):
    """(status, Location) of a HEAD without following redirects; status 0 on network failure."""
    return _no_redirect(url, "HEAD")

def redirect_target(url):
    """Same with GET, for hosts that refuse HEAD."""
    return _no_redirect(url, "GET")

for _stream in (sys.stdout, sys.stderr):  # channel names contain emoji; a cp1252 console must not crash a refresh
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

def log(*a):
    line = time.strftime("%H:%M:%S") + " " + " ".join(str(x) for x in a)
    if sys.stdout is None:  # windowed executable: no console
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    else:
        print(line, file=sys.stderr if "--export" in sys.argv else sys.stdout, flush=True)  # --export owns stdout

def yt_initial_data(page):
    m = (re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
         or re.search(r'window\["ytInitialData"\] = (\{.*?\});', page, re.S))
    return json.loads(m.group(1)) if m else {}

def walk(obj, key):
    """Yield every value stored under `key` anywhere inside nested dicts/lists."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if key in o:
                yield o[key]
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)

def rtext(o):
    """YouTube's text objects: {'simpleText': ..} or {'runs': [{'text': ..}, ..]}."""
    if isinstance(o, str):
        return o
    if isinstance(o, dict):
        if "simpleText" in o:
            return o["simpleText"]
        if "runs" in o:
            return "".join(r.get("text", "") for r in o["runs"])
    return ""

def parse_duration(s):
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", (s or "").strip())
    if not m:
        return None
    h, mi, se = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se)

def abs_url(u):
    return "https:" + u if u.startswith("//") else u

def meta_content(page, prop):
    m = re.search(r'<meta (?:property|name)="%s" content="([^"]*)"' % re.escape(prop), page)
    return html.unescape(m.group(1)) if m else ""

def iso_ts(s):
    try:
        return datetime.fromisoformat((s or "").strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def strip_html(s, limit=400):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s).strip()
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")

# ---------------------------------------------------------------- storage (one SQLite file next to the program)

USER_ID = 1  # single-user mode: one implicit user owns every follow and mark; milestone 3 adds the rest
SUB_LISTS = {"yt": ("youtube", "channels"), "tw": ("twitter", "users"), "rss": ("rss", "feeds"), "arxiv": ("arxiv", "queries")}
FOLLOW_KEYS = ("tags", "muted", "min_minutes", "note", "added")  # the per-user part of a subscription record → `follows`
SOURCE_KEYS = ("id", "name", "error", "last_fetched")           # columns of `sources`; everything else in a record → meta_json
LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', pass_hash TEXT, invited_by INTEGER REFERENCES users(id),
    created INTEGER NOT NULL, settings_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- 'youtube.min_minutes' → JSON value
CREATE TABLE IF NOT EXISTS sources (  -- a channel, account, feed or arXiv query; shared by everyone who follows it
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}', last_fetched REAL, error TEXT, UNIQUE (kind, ref));
CREATE TABLE IF NOT EXISTS follows (
    user_id INTEGER NOT NULL REFERENCES users(id), source_id INTEGER NOT NULL REFERENCES sources(id),
    tags TEXT NOT NULL DEFAULT '[]', muted INTEGER NOT NULL DEFAULT 0, min_minutes INTEGER, note TEXT NOT NULL DEFAULT '',
    added INTEGER, PRIMARY KEY (user_id, source_id));
CREATE TABLE IF NOT EXISTS items (  -- ext_id is the id the page uses: video id, tweet link, post link, arXiv id
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id), kind TEXT NOT NULL, ext_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', ts REAL NOT NULL, payload_json TEXT NOT NULL,
    fetched REAL NOT NULL, UNIQUE (source_id, ext_id));
CREATE INDEX IF NOT EXISTS items_ts ON items (ts);
CREATE TABLE IF NOT EXISTS marks (  -- item_id is an items.ext_id; a saved mark keeps its own snapshot so it outlives the item
    user_id INTEGER NOT NULL REFERENCES users(id), item_id TEXT NOT NULL, seen_at INTEGER, saved_at INTEGER,
    tags TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '', snapshot_json TEXT, PRIMARY KEY (user_id, item_id));
CREATE TABLE IF NOT EXISTS shares (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), item_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY, share_id INTEGER NOT NULL REFERENCES shares(id), user_id INTEGER NOT NULL REFERENCES users(id),
    parent_id INTEGER REFERENCES comments(id), body TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cache (kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS inbox (  -- links handed to quietfeed from elsewhere (clipboard, the link box, the URL handler)
    user_id INTEGER NOT NULL REFERENCES users(id), url TEXT NOT NULL, item_json TEXT NOT NULL, via TEXT NOT NULL DEFAULT '',
    added INTEGER NOT NULL, PRIMARY KEY (user_id, url));
"""

def open_db():
    """Open (creating if needed) quietfeed.db in WAL mode. Returns (connection, True when it holds no settings yet)."""
    con = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)  # transactions are explicit: see tx()
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO users (id, name, created) VALUES (?, '', ?)", (USER_ID, int(time.time())))
    empty = con.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    return con, empty

DB, NEW_DB = open_db()

@contextmanager
def tx():
    """One write transaction, serialised with every other access by LOCK. Not re-entrant: never nest it."""
    with LOCK:
        DB.execute("BEGIN IMMEDIATE")
        try:
            yield DB
        except BaseException:
            DB.execute("ROLLBACK")
            raise
        DB.execute("COMMIT")

def load_settings():
    """settings rows → the nested CONFIG shape ('youtube.min_minutes' → CONFIG['youtube']['min_minutes'])."""
    cfg = {}
    with LOCK:
        rows = DB.execute("SELECT key, value FROM settings").fetchall()
    for r in rows:
        v = json.loads(r["value"])
        if "." in r["key"]:
            sec, name = r["key"].split(".", 1)
            cfg.setdefault(sec, {})[name] = v
        else:
            cfg[r["key"]] = v
    return cfg

def settings_rows(cfg):
    """The inverse: one row per setting, sections flattened to 'section.key'. Subscription lists are not settings."""
    lists = set(SUB_LISTS.values())
    rows = []
    for k, v in cfg.items():
        if isinstance(v, dict):
            rows += [(f"{k}.{kk}", json.dumps(vv, ensure_ascii=False)) for kk, vv in v.items() if (k, kk) not in lists]
        else:
            rows.append((k, json.dumps(v, ensure_ascii=False)))
    return rows

def load_subs(kind):
    """The followed sources of one kind as the subscription records the rest of the program works with."""
    with LOCK:
        rows = DB.execute("""SELECT s.ref, s.name, s.meta_json, s.last_fetched, s.error, f.tags, f.muted, f.min_minutes, f.note, f.added
                             FROM follows f JOIN sources s ON s.id = f.source_id WHERE f.user_id = ? AND s.kind = ?
                             ORDER BY f.rowid""", (USER_ID, kind)).fetchall()  # follows are rewritten in list order
    out = []
    for r in rows:
        rec = {"id": r["ref"], "name": r["name"], "handle": "", "avatar": "", "tags": json.loads(r["tags"]), "muted": bool(r["muted"]),
               "min_minutes": r["min_minutes"], "note": r["note"], "added": r["added"]}
        rec.update(json.loads(r["meta_json"]))  # handle, avatar, site, qtype, an unresolved ref (then id is ''), anything else
        if r["error"]:
            rec["error"] = r["error"]
        if r["last_fetched"]:
            rec["last_fetched"] = r["last_fetched"]
        out.append(rec)
    return out

def write_config(db):
    """Rewrite settings, sources and follows from CONFIG: a few hundred rows at most, simpler than tracking changes."""
    db.execute("DELETE FROM settings")
    db.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", settings_rows(CONFIG))
    sources, follows = [], []
    for kind, s in all_subs():
        key = s.get("id") or s.get("ref") or ""
        meta = {k: v for k, v in s.items() if k not in SOURCE_KEYS and k not in FOLLOW_KEYS}
        if not s.get("id"):
            meta["id"] = ""  # not resolved yet: ref is what the user typed; fetch_channel fills in the real id
        sources.append((kind, key, s.get("name") or "", json.dumps(meta, ensure_ascii=False), s.get("last_fetched"), s.get("error") or None))
        follows.append((USER_ID, kind, key, json.dumps(s.get("tags") or []), int(bool(s.get("muted"))), s.get("min_minutes"),
                        s.get("note") or "", s.get("added")))
    db.executemany("""INSERT INTO sources (kind, ref, name, meta_json, last_fetched, error) VALUES (?, ?, ?, ?, ?, ?)
                      ON CONFLICT (kind, ref) DO UPDATE SET name = excluded.name, meta_json = excluded.meta_json,
                      last_fetched = excluded.last_fetched, error = excluded.error""", sources)
    db.execute("DELETE FROM follows WHERE user_id = ?", (USER_ID,))
    db.executemany("""INSERT OR REPLACE INTO follows (user_id, source_id, tags, muted, min_minutes, note, added)
                      VALUES (?, (SELECT id FROM sources WHERE kind = ? AND ref = ?), ?, ?, ?, ?, ?)""", follows)
    db.execute("DELETE FROM sources WHERE id NOT IN (SELECT source_id FROM follows) AND id NOT IN (SELECT source_id FROM items)")

def load_marks():
    """marks rows → {'seen': id → timestamp, 'saved': id → snapshot of the item with tags_mine / note}."""
    seen, saved = {}, {}
    with LOCK:
        rows = DB.execute("SELECT item_id, seen_at, saved_at, snapshot_json FROM marks WHERE user_id = ? ORDER BY rowid", (USER_ID,)).fetchall()
    for r in rows:
        if r["seen_at"] is not None:
            seen[r["item_id"]] = r["seen_at"]
        if r["saved_at"] is not None:
            saved[r["item_id"]] = json.loads(r["snapshot_json"]) if r["snapshot_json"] else r["saved_at"]  # old entries: just a time
    return {"seen": seen, "saved": saved}

def write_marks(db, field, ids):
    """Write the in-memory seen / saved entries for `ids` to the marks table (a missing entry clears that column)."""
    for i in ids:
        v = SEEN_STATE[field].get(i)
        if field == "seen":
            db.execute("""INSERT INTO marks (user_id, item_id, seen_at) VALUES (?, ?, ?)
                          ON CONFLICT (user_id, item_id) DO UPDATE SET seen_at = excluded.seen_at""", (USER_ID, i, v))
        else:
            rec = v if isinstance(v, dict) else None
            db.execute("""INSERT INTO marks (user_id, item_id, saved_at, tags, note, snapshot_json) VALUES (?, ?, ?, ?, ?, ?)
                          ON CONFLICT (user_id, item_id) DO UPDATE SET saved_at = excluded.saved_at, tags = excluded.tags,
                          note = excluded.note, snapshot_json = excluded.snapshot_json""",
                       (USER_ID, i, rec.get("saved_at") if rec else v, json.dumps(rec.get("tags_mine") or []) if rec else "[]",
                        (rec.get("note") or "") if rec else "", json.dumps(rec, ensure_ascii=False) if rec else None))
    db.execute("DELETE FROM marks WHERE user_id = ? AND seen_at IS NULL AND saved_at IS NULL", (USER_ID,))

def load_cache():
    cache = {"durations": {}, "shorts": {}}
    with LOCK:
        for r in DB.execute("SELECT kind, key, value FROM cache").fetchall():
            cache.setdefault(r["kind"], {})[r["key"]] = json.loads(r["value"])
    return cache

def write_items(db, items):
    """Upsert feed items, keyed by source and external id. Items of sources not in the table are skipped."""
    now = time.time()
    ids = {(r["kind"], r["ref"]): r["id"] for r in db.execute("SELECT id, kind, ref FROM sources").fetchall()}
    rows = []
    for it in items:
        sid = ids.get((it.get("kind"), it.get("cid")))
        if sid is None or not it.get("id"):
            continue
        it = dict(it)
        fetched = it.pop("fetched", None) or now  # exports carry the time an item was last seen in its feed
        rows.append((sid, it["kind"], it["id"], it.get("title") or "", it.get("author") or "", float(it.get("ts") or now),
                     json.dumps(it, ensure_ascii=False), fetched))
    db.executemany("""INSERT INTO items (source_id, kind, ext_id, title, author, ts, payload_json, fetched) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                      ON CONFLICT (source_id, ext_id) DO UPDATE SET title = excluded.title, author = excluded.author, ts = excluded.ts,
                      payload_json = excluded.payload_json, fetched = excluded.fetched""", rows)
    return len(rows)

def prune_items(db):
    """Items that dropped out of their feed more than keep_days ago go; saved ones never do."""
    days = max(1, int(CONFIG.get("keep_days") or 90))
    db.execute("DELETE FROM items WHERE fetched < ? AND ext_id NOT IN (SELECT item_id FROM marks WHERE saved_at IS NOT NULL)",
               (time.time() - days * 86400,))

def load_items(limit=None):
    """Newest items first, with what the cache now knows about durations and Shorts applied to videos."""
    limit = limit or max(5000, 10 * int(CONFIG.get("max_items") or 400))
    with LOCK:
        rows = DB.execute("SELECT payload_json FROM items ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        it = json.loads(r["payload_json"])
        if it.get("kind") == "yt":
            it["dur"] = CACHE["durations"].get(it["id"], it.get("dur"))
            it["short"] = CACHE["shorts"].get(it["id"], it.get("short", False))
        out.append(it)
    return out

def inbox_list():
    with LOCK:
        rows = DB.execute("SELECT url, item_json, via, added FROM inbox WHERE user_id = ? ORDER BY added DESC LIMIT 500", (USER_ID,)).fetchall()
    out = []
    for r in rows:
        it = json.loads(r["item_json"])
        it.update({"url": r["url"], "via": r["via"], "added": r["added"], "cid": "inbox", "tags": it.get("tags") or []})
        out.append(it)
    return out

def inbox_add(url, item, via=""):
    with tx() as db:
        if item.get("id"):  # the same video or post reached through two spellings of its link is one entry
            db.execute("DELETE FROM inbox WHERE user_id = ? AND json_extract(item_json, '$.id') = ?", (USER_ID, item["id"]))
        db.execute("INSERT OR REPLACE INTO inbox (user_id, url, item_json, via, added) VALUES (?, ?, ?, ?, ?)",
                   (USER_ID, url, json.dumps(item, ensure_ascii=False), via, int(time.time())))

def inbox_remove(url):
    with tx() as db:
        db.execute("DELETE FROM inbox WHERE user_id = ? AND url = ?", (USER_ID, url))

def export_data():
    """Everything in the database as one JSON-able dict: the old config / state / cache shapes plus the stored items."""
    with LOCK:
        rows = DB.execute("SELECT payload_json, fetched FROM items ORDER BY ts DESC").fetchall()
        return {"quietfeed_export": 1, "exported_at": int(time.time()),
                "config": json.loads(json.dumps(CONFIG)), "state": json.loads(json.dumps(SEEN_STATE)),
                "cache": json.loads(json.dumps(CACHE)),
                "items": [{**json.loads(r["payload_json"]), "fetched": r["fetched"]} for r in rows]}

def import_data(data):
    """Merge a JSON export (or a bare config.json / state.json / cache.json) into memory and the database.
    Nothing is removed: new follows, marks and items are added; settings in the file replace the current ones."""
    if "youtube" in data or "twitter" in data:
        data = {"config": data}
    elif "seen" in data or "saved" in data:
        data = {"state": data}
    elif "durations" in data or "shorts" in data:
        data = {"cache": data}
    global FIRST_RUN
    n = Counter()
    lists = {v: k for k, v in SUB_LISTS.items()}
    cfg = data.get("config") or {}
    with LOCK:
        if FIRST_RUN and any((k, kk) in lists for k, v in cfg.items() if isinstance(v, dict) for kk in v):
            for sec, key in lists:  # a new database holds nothing but the starter channels: they give way to the import
                CONFIG[sec][key] = []
            FIRST_RUN = False
        for k, v in cfg.items():
            if not isinstance(v, dict):
                CONFIG[k] = v
                continue
            CONFIG.setdefault(k, {})
            for kk, vv in v.items():
                if (k, kk) not in lists:
                    CONFIG[k][kk] = vv
                    continue
                cur = CONFIG[k].setdefault(kk, [])
                have = {(s.get("id") or s.get("ref")) if isinstance(s, dict) else s for s in cur}
                for s in vv if isinstance(vv, list) else []:
                    key = (s.get("id") or s.get("ref")) if isinstance(s, dict) else s
                    if key and key not in have:
                        have.add(key)
                        cur.append(s)
                        n["follows"] += 1
        normalize_subscriptions()  # plain strings → records, duplicates dropped
        save_config()
        st = data.get("state") or {}
        seen_ids, saved_ids = [], []
        for i, ts in (st.get("seen") or {}).items():
            if i not in SEEN_STATE["seen"]:
                SEEN_STATE["seen"][i] = ts
                seen_ids.append(i)
        for i, v in (st.get("saved") or {}).items():
            cur = SEEN_STATE["saved"].get(i)
            SEEN_STATE["saved"][i] = {**cur, **v} if isinstance(cur, dict) and isinstance(v, dict) else v
            saved_ids.append(i)
        for kind, d in (data.get("cache") or {}).items():
            for key, v in (d or {}).items():
                cache_put(kind, key, v)
        with tx() as db:
            write_marks(db, "seen", seen_ids)
            write_marks(db, "saved", saved_ids)
            n["seen"], n["saved"] = len(seen_ids), len(saved_ids)
            n["items"] = write_items(db, data.get("items") or [])
        save_cache()
    return n

def migrate_legacy_json():
    """First run next to the old JSON files: pull them into the database, then rename them *.json.migrated."""
    found = {k: p for k, p in LEGACY_JSON.items() if os.path.exists(p)}
    if not found:
        return False
    data = {k: load_json(p, None) for k, p in found.items()}
    import_data({k: v for k, v in data.items() if isinstance(v, dict)})
    for p in found.values():
        os.replace(p, p + ".migrated")
    log("migrated " + ", ".join(os.path.basename(p) for p in found.values())
        + " into quietfeed.db; the old files are kept as *.json.migrated")
    return True

# ---------------------------------------------------------------- state

CONFIG = load_settings()
for k, v in DEFAULT_CONFIG.items():  # fill in keys added by newer versions
    if k not in CONFIG:
        CONFIG[k] = json.loads(json.dumps(v))
    elif isinstance(v, dict):
        for kk, vv in v.items():
            CONFIG[k].setdefault(kk, json.loads(json.dumps(vv)))
for _kind, (_sec, _key) in SUB_LISTS.items():
    if not NEW_DB:  # a new database keeps DEFAULT_CONFIG's starter channels until normalize_subscriptions() writes them
        CONFIG[_sec][_key] = load_subs(_kind)
FIRST_RUN = NEW_DB  # cleared below if the old JSON files were migrated instead

CACHE = load_cache()
CACHE_DIRTY = set()  # (kind, key) pairs changed since the last save_cache
SEEN_STATE = load_marks()

STATE = {"items": load_items(), "errors": [], "last_refresh": None, "refreshing": False}
AVATARS = {}  # in-memory bytes cache

def save_config():
    with tx() as db:
        write_config(db)

def cache_put(kind, key, value):
    with LOCK:
        CACHE[kind][key] = value
        CACHE_DIRTY.add((kind, key))

def save_cache():
    with tx() as db:
        db.executemany("INSERT OR REPLACE INTO cache (kind, key, value) VALUES (?, ?, ?)",
                       [(k, key, json.dumps(CACHE[k].get(key))) for k, key in CACHE_DIRTY])
        CACHE_DIRTY.clear()

# ---------------------------------------------------------------- subscriptions (all kinds)

def new_sub(kind, id_, name="", handle="", avatar="", **extra):
    rec = {"id": id_, "name": name or (("@" + id_) if kind == "tw" else id_), "handle": handle,
           "avatar": avatar, "tags": [], "muted": False, "min_minutes": None, "note": "",
           "added": int(time.time())}
    rec.update(extra)
    return rec

def sub_list(kind):
    return {"yt": CONFIG["youtube"]["channels"], "tw": CONFIG["twitter"]["users"],
            "rss": CONFIG["rss"]["feeds"], "arxiv": CONFIG["arxiv"]["queries"]}[kind]

def yt_subs():
    return sub_list("yt")

def tw_subs():
    return sub_list("tw")

def all_subs():
    return [(k, s) for k in KINDS for s in sub_list(k)]

def normalize_subscriptions():
    """config.json may hold plain strings ('@veritasium', 'UC…', 'someone', a feed URL, 'math.PR'); make them records."""
    changed = False
    yt = []
    for s in yt_subs():
        if isinstance(s, str):
            s = s.strip()
            if not s:
                continue
            rec = new_sub("yt", s if re.fullmatch(r"UC[\w-]{22}", s) else "")
            rec["name"] = s
            rec["ref"] = s  # resolved to a channel id on the next refresh
            yt.append(rec)
            changed = True
        elif isinstance(s, dict):
            yt.append(s)
    tw = []
    for s in tw_subs():
        if isinstance(s, str):
            s = s.strip().lstrip("@").lower()
            if s:
                tw.append(new_sub("tw", s))
                changed = True
        elif isinstance(s, dict):
            s["id"] = s["id"].lstrip("@").lower()
            tw.append(s)
    rss = []
    for s in CONFIG["rss"]["feeds"]:
        if isinstance(s, str) and s.strip():
            rss.append(new_sub("rss", s.strip(), s.strip()))
            changed = True
        elif isinstance(s, dict):
            rss.append(s)
    ax = []
    for s in CONFIG["arxiv"]["queries"]:
        if isinstance(s, str) and s.strip():
            r = arxiv_ref(s)
            ax.append(new_sub("arxiv", r["id"], r["label"], qtype=r["type"]))
            changed = True
        elif isinstance(s, dict):
            ax.append(s)
    for kind, lst in (("yt", yt), ("tw", tw), ("rss", rss), ("arxiv", ax)):  # the same thing twice (an import): keep the first
        keys, uniq = set(), []
        for s in lst:
            key = s.get("id") or s.get("ref") or ""
            if key in keys:
                changed = True
                continue
            keys.add(key)
            uniq.append(s)
        sec, name = SUB_LISTS[kind]
        CONFIG[sec][name] = uniq
    if changed or FIRST_RUN:
        save_config()

def find_sub(kind, id_):
    for s in sub_list(kind):
        if s.get("id") == id_:
            return s
    return None

def add_sub(kind, info):
    """info: {id, name, handle, avatar, ...}. Returns True if added, False if already followed."""
    with LOCK:
        if find_sub(kind, info["id"]):
            return False
        extra = {k: v for k, v in info.items() if k not in ("id", "name", "handle", "avatar", "matched")}
        rec = new_sub(kind, info["id"], info.get("name", ""), info.get("handle", ""), info.get("avatar", ""), **extra)
        sub_list(kind).append(rec)
        save_config()
        return True

def remove_sub(kind, id_):
    with LOCK:
        lst = sub_list(kind)
        before = len(lst)
        lst[:] = [s for s in lst if s.get("id") != id_]
        save_config()
        return len(lst) < before

def update_sub(kind, id_, patch):
    with LOCK:
        s = find_sub(kind, id_)
        if not s:
            return False
        for k in ("tags", "muted", "min_minutes", "note", "name"):
            if k in patch:
                v = patch[k]
                if k == "tags":
                    v = sorted({t.strip().lower() for t in (v if isinstance(v, list) else str(v).split(",")) if t.strip()})
                if k == "min_minutes":
                    v = None if v in ("", None) else max(0, int(v))
                if k == "muted":
                    v = bool(v)
                s[k] = v
        save_config()
        return True

def subs_payload():
    with LOCK:
        out = []
        for kind, s in all_subs():
            out.append({**{k: s.get(k) for k in ("id", "name", "handle", "avatar", "tags", "muted", "min_minutes", "note", "added", "site", "qtype")},
                        "kind": kind, "ref": s.get("ref", ""), "error": s.get("error", "")})
        return out

# ---------------------------------------------------------------- youtube lookups (done by this script, never the browser)

def yt_channel_info(ref):
    """'@handle', 'UC…' id, or a youtube.com channel URL → {id, name, handle, avatar}."""
    ref = ref.strip()
    if re.fullmatch(r"UC[\w-]{22}", ref):
        url = f"https://www.youtube.com/channel/{ref}"
    elif ref.startswith("http"):
        url = ref.split("?")[0]
    else:
        url = "https://www.youtube.com/@" + ref.lstrip("@")
    page = yt_page(url)
    m = (re.search(r'<meta itemprop="identifier" content="(UC[\w-]{22})"', page)
         or re.search(r'"externalId":"(UC[\w-]{22})"', page)
         or re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page))
    if not m:
        raise RuntimeError(f"no channel found at {url}")
    hm = re.search(r'"canonicalBaseUrl":"/(@[^"/]+)"', page)
    return {"id": m.group(1), "name": meta_content(page, "og:title") or ref,
            "handle": hm.group(1) if hm else "", "avatar": meta_content(page, "og:image")}

def yt_search(q):
    page = yt_page("https://www.youtube.com/results?" + urlencode({"search_query": q, "sp": "EgIQAg=="}))
    out, seen = [], set()
    for c in walk(yt_initial_data(page), "channelRenderer"):
        cid = c.get("channelId")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        # YouTube has swapped these two fields around over time; sniff by content.
        texts = [rtext(c.get(k)) for k in ("subscriberCountText", "videoCountText")]
        thumbs = c.get("thumbnail", {}).get("thumbnails", [])
        out.append({"id": cid, "name": rtext(c.get("title")),
                    "handle": next((t for t in texts if t.startswith("@")), ""),
                    "subs": next((t for t in texts if "subscriber" in t), ""),
                    "avatar": abs_url(thumbs[-1]["url"]) if thumbs else "",
                    "desc": rtext(c.get("descriptionSnippet"))[:200],
                    "following": find_sub("yt", cid) is not None})
    return out[:20]

def subs_number(s):
    m = re.match(r"([\d.]+)\s*([KMB]?)", s or "")
    if not m:
        return 0
    return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]

def yt_match_by_name(name):
    """A channel *name* (as it appears on YouTube, e.g. 'Andrew Steele') → the best-matching channel.
    Exact name or handle match wins; otherwise the biggest channel whose name contains it."""
    rs = yt_search(name)
    if not rs:
        raise RuntimeError(f"{name}: no channel found by that name")
    key = re.sub(r"\W+", "", name.lower())
    exact = [r for r in rs if re.sub(r"\W+", "", r["name"].lower()) == key or re.sub(r"\W+", "", r["handle"].lower()) == key]
    if exact:
        pick = max(exact, key=lambda r: subs_number(r["subs"]))
    else:
        contains = [r for r in rs[:8] if key in re.sub(r"\W+", "", r["name"].lower())]
        pick = max(contains or rs[:3], key=lambda r: subs_number(r["subs"]))
    note = ""
    if len(exact) > 1 and subs_number(pick["subs"]) < 5000:
        note = f" — {len(exact)} channels share this name and none is big, check it"
    return {"id": pick["id"], "name": pick["name"], "handle": pick["handle"], "avatar": pick["avatar"],
            "matched": f"{name} → {pick['name']} {pick['handle']} ({pick['subs'] or 'no count'}){note}"}

def yt_scan_channel(cid):
    """Two requests per channel: /videos gives durations of the latest ~30 uploads,
    /shorts gives the ids of recent Shorts. Replaces per-video yt-dlp calls."""
    durs, shorts = {}, set()
    try:
        d = yt_initial_data(yt_page(f"https://www.youtube.com/channel/{cid}/videos"))
        for lv in walk(d, "lockupViewModel"):
            vid = lv.get("contentId")
            if not vid:
                m = re.search(r"/vi/([\w-]{11})/", json.dumps(lv.get("contentImage", {})))
                vid = m.group(1) if m else None
            if not vid:
                continue
            durs[vid] = None
            for b in walk(lv, "thumbnailBadgeViewModel"):
                s = parse_duration(b.get("text", ""))
                if s is not None:
                    durs[vid] = s
                    break
        for vr in walk(d, "videoRenderer"):  # older layout
            if vr.get("videoId"):
                durs[vr["videoId"]] = parse_duration(rtext(vr.get("lengthText")))
    except Exception as ex:
        log("  videos tab", cid, ex)
    try:
        d = yt_initial_data(yt_page(f"https://www.youtube.com/channel/{cid}/shorts"))
        for s in walk(d, "shortsLockupViewModel"):
            vid = (s.get("onTap", {}).get("innertubeCommand", {}).get("reelWatchEndpoint", {}).get("videoId")
                   or s.get("entityId", "").replace("shorts-shelf-item-", ""))
            if vid and len(vid) == 11:
                shorts.add(vid)
        for r in walk(d, "reelItemRenderer"):  # older layout
            if r.get("videoId"):
                shorts.add(r["videoId"])
    except Exception as ex:
        log("  shorts tab", cid, ex)
    return durs, shorts

def fetch_channel(sub):
    """One subscription → list of feed items. Resolves string refs, fills in avatar/handle, scans tabs."""
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    if not sub.get("id") or not sub.get("avatar"):
        info = yt_channel_info(sub.get("id") or sub.get("ref", ""))
        with LOCK:
            if sub.get("id") != info["id"] and find_sub("yt", info["id"]):
                yt_subs().remove(sub)  # duplicate of something already followed
                save_config()
                return []
            sub.update(info)
            sub.pop("ref", None)
            sub.pop("error", None)
            save_config()
    cid = sub["id"]
    root = ET.fromstring(fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
    if not sub.get("name") or sub["name"] == cid:
        sub["name"] = root.findtext("a:author/a:name", default=cid, namespaces=ns)
    raw = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", namespaces=ns)
        raw.append({"kind": "yt", "id": vid, "cid": cid, "title": e.findtext("a:title", default="", namespaces=ns),
                    "author": sub["name"], "ts": iso_ts(e.findtext("a:published", namespaces=ns)) or time.time(),
                    "desc": (e.findtext("m:group/m:description", default="", namespaces=ns) or "")[:300]})
    need = [r["id"] for r in raw if r["id"] not in CACHE["shorts"] or r["id"] not in CACHE["durations"]]
    if need:
        durs, shorts = yt_scan_channel(cid)
        for v, s in durs.items():
            cache_put("shorts", v, False)
            if s is not None:
                cache_put("durations", v, s)
        for v in shorts:
            cache_put("shorts", v, True)
        for v in need:  # neither tab listed it (live stream, premiere, old): ask YouTube directly
            if v not in CACHE["shorts"]:
                code, _ = head_no_redirect(f"https://www.youtube.com/shorts/{v}")
                if code:
                    cache_put("shorts", v, code == 200)
    for r in raw:
        r["dur"] = CACHE["durations"].get(r["id"])
        r["short"] = CACHE["shorts"].get(r["id"], False)
    return raw

# ---------------------------------------------------------------- twitter

def tw_fetch_user(sub):
    user = sub["id"]
    instances = CONFIG["twitter"].get("nitter_instances", [])
    last = "no instances configured"
    for inst in instances:
        try:
            xml = fetch(f"{inst.rstrip('/')}/{user}/rss", timeout=12, headers=FEED_ACCEPT)
            root = ET.fromstring(xml)
            items = []
            for it in root.iter("item"):
                pubdate = it.findtext("pubDate") or ""
                try:
                    ts = parsedate_to_datetime(pubdate).timestamp()
                except Exception:
                    ts = time.time()
                link = re.sub(r"^https?://[^/]+", "https://x.com", it.findtext("link") or "").split("#")[0]
                creator = it.findtext("{http://purl.org/dc/elements/1.1/}creator") or "@" + user
                items.append({"kind": "tw", "id": link, "cid": user, "title": it.findtext("title") or "",
                              "author": creator, "ts": ts, "desc": ""})
            return items
        except Exception as ex:
            last = f"{inst}: {ex}"
    raise RuntimeError(f"no Nitter instance answered (last: {last})")

def tw_resolve_id(num):
    """Numeric X user id → handle. x.com/intent/user?user_id=… redirects to the profile with no login;
    a Nitter instance's /i/user/… is the fallback."""
    code, loc = redirect_target(f"https://x.com/intent/user?user_id={num}")
    m = re.match(r"(?:https?://(?:www\.)?(?:x|twitter)\.com)?/([A-Za-z0-9_]{1,15})(?:[/?#]|$)", loc or "")
    if code in (301, 302, 303, 307, 308) and m and m.group(1).lower() not in ("i", "intent", "home", "login", "account"):
        return m.group(1).lower()
    for inst in CONFIG["twitter"].get("nitter_instances", []):
        code, loc = head_no_redirect(f"{inst.rstrip('/')}/i/user/{num}")
        m = re.search(r"/([A-Za-z0-9_]{1,15})/?$", loc or "")
        if code in (301, 302, 303, 307, 308) and m and m.group(1) != "i":
            return m.group(1).lower()
    return None

# ---------------------------------------------------------------- rss / substack

def parse_feed(data):
    """RSS 2.0, RDF or Atom bytes → (feed title, [items without kind/cid])."""
    root = ET.fromstring(data)
    A = "{http://www.w3.org/2005/Atom}"
    items = []
    if root.tag == A + "feed":
        title = (root.findtext(A + "title") or "").strip()
        for e in root.findall(A + "entry"):
            link = next((l.get("href") for l in e.findall(A + "link") if l.get("rel") in (None, "alternate")), "") or ""
            desc = e.findtext(A + "summary") or e.findtext(A + "content") or ""
            items.append({"id": link or e.findtext(A + "id") or "", "title": " ".join((e.findtext(A + "title") or "").split()),
                          "ts": iso_ts(e.findtext(A + "published")) or iso_ts(e.findtext(A + "updated")) or time.time(),
                          "desc": strip_html(desc), "link": link,
                          "by": e.findtext(A + "author/" + A + "name") or ""})
        return title, items
    ch = root.find("channel")
    title = ((ch.findtext("title") if ch is not None else "") or root.findtext("{http://purl.org/rss/1.0/}channel/{http://purl.org/rss/1.0/}title") or "").strip()
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        try:
            ts = parsedate_to_datetime(pub).timestamp()
        except Exception:
            ts = iso_ts(pub) or time.time()
        desc = it.findtext("description") or it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
        items.append({"id": link or it.findtext("guid") or "", "title": " ".join((it.findtext("title") or "").split()),
                      "ts": ts, "desc": strip_html(desc), "link": link,
                      "by": it.findtext("{http://purl.org/dc/elements/1.1/}creator") or it.findtext("author") or ""})
    for it in root.iter("{http://purl.org/rss/1.0/}item"):  # RDF
        link = (it.findtext("{http://purl.org/rss/1.0/}link") or "").strip()
        items.append({"id": link, "title": " ".join((it.findtext("{http://purl.org/rss/1.0/}title") or "").split()),
                      "ts": iso_ts(it.findtext("{http://purl.org/dc/elements/1.1/}date")) or time.time(),
                      "desc": strip_html(it.findtext("{http://purl.org/rss/1.0/}description") or ""), "link": link, "by": ""})
    return title, items

def rss_resolve(url):
    """A site or feed URL → {id: feed url, name, site}. Tries the URL itself, the usual feed paths
    (Substack is /feed) and the <link rel=alternate> a page advertises."""
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    cands = [url]
    if not re.search(r"(feed|rss|atom|\.xml)/?$", u.path, re.I):
        cands += [url.rstrip("/") + "/feed", base + "/feed", base + "/rss", base + "/feed.xml", base + "/index.xml", base + "/atom.xml"]
    tried, discovered = [], False
    while cands:
        c = cands.pop(0)
        try:
            data = fetch(c, timeout=15, headers=FEED_ACCEPT)
        except Exception as ex:
            tried.append(f"{c}: {ex}")
            continue
        head = data[:2000].lstrip().lower()
        if head.startswith(b"<!doctype html") or b"<html" in head:
            if not discovered:
                discovered = True
                page = data.decode("utf-8", "ignore")
                for m in re.finditer(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*>', page, re.I):
                    h = re.search(r'href="([^"]+)"', m.group(0))
                    if h:
                        href = html.unescape(h.group(1))
                        cands.insert(0, href if href.startswith("http") else base + "/" + href.lstrip("/"))
            continue
        try:
            title, items = parse_feed(data)
        except Exception as ex:
            tried.append(f"{c}: not a feed ({ex})")
            continue
        return {"id": c, "name": title or u.netloc, "site": base}
    raise RuntimeError("no feed found at " + url + (" — " + tried[0] if tried else ""))

def fetch_rss(sub):
    title, items = parse_feed(fetch(sub["id"], timeout=20, headers=FEED_ACCEPT))
    if title and (not sub.get("name") or sub["name"] == sub["id"]):
        sub["name"] = title
    out = []
    for it in items:
        if not it["id"]:
            continue
        out.append({"kind": "rss", "id": it["id"], "cid": sub["id"], "title": it["title"] or it["link"],
                    "author": sub.get("name") or title, "ts": it["ts"], "desc": it["desc"], "link": it["link"] or it["id"], "by": it["by"]})
    return out

# ---------------------------------------------------------------- arxiv

ARXIV_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_ARCHIVES = ("math", "cs", "stat", "physics", "q-bio", "q-fin", "econ", "eess", "nlin", "astro-ph", "cond-mat",
                  "hep-th", "hep-ph", "hep-ex", "hep-lat", "gr-qc", "quant-ph", "nucl-th", "nucl-ex", "math-ph")
ARXIV_CAT_RE = r"(?:%s)(?:\.[A-Za-z\-]{2,12})?" % "|".join(re.escape(a) for a in ARXIV_ARCHIVES)
ARXIV_LAST = [0.0]
ARXIV_LOCK = threading.Lock()

def arxiv_api(search_query="", max_results=50, sort="submittedDate", id_list=""):
    """One arXiv API call, politely spaced (they ask for 3 s between requests)."""
    params = {"max_results": max_results}
    if id_list:
        params["id_list"] = id_list
    else:
        params.update({"search_query": search_query, "sortBy": sort, "sortOrder": "descending"})
    with ARXIV_LOCK:
        wait = ARXIV_LAST[0] + 3 - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            data = fetch("https://export.arxiv.org/api/query?" + urlencode(params), timeout=40)
        finally:
            ARXIV_LAST[0] = time.time()
    out = []
    for e in ET.fromstring(data).findall("a:entry", ARXIV_NS):
        aid = (e.findtext("a:id", "", ARXIV_NS) or "").rsplit("/", 1)[-1]
        base = re.sub(r"v\d+$", "", aid)
        if not base:
            continue
        authors = [a.findtext("a:name", "", ARXIV_NS) for a in e.findall("a:author", ARXIV_NS)]
        cats = [c.get("term") for c in e.findall("a:category", ARXIV_NS) if c.get("term")]
        prim = e.find("arxiv:primary_category", ARXIV_NS)
        pdf = next((l.get("href") for l in e.findall("a:link", ARXIV_NS) if l.get("title") == "pdf"), f"https://arxiv.org/pdf/{base}")
        out.append({"kind": "arxiv", "id": base, "title": " ".join((e.findtext("a:title", "", ARXIV_NS) or "").split()),
                    "authors": authors, "author": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                    "ts": iso_ts(e.findtext("a:published", "", ARXIV_NS)) or time.time(),
                    "updated": iso_ts(e.findtext("a:updated", "", ARXIV_NS)),
                    "desc": " ".join((e.findtext("a:summary", "", ARXIV_NS) or "").split())[:1500],
                    "cats": cats, "primary": prim.get("term") if prim is not None else (cats[0] if cats else ""),
                    "link": f"https://arxiv.org/abs/{base}", "pdf": pdf, "ver": aid,
                    "comment": " ".join((e.findtext("arxiv:comment", "", ARXIV_NS) or "").split())[:200]})
    return out

def arxiv_ref(text):
    """'math.PR', 'cat:cs.LG', 'math' (whole archive), 'au:Andrew Steele', an arxiv.org/list/... link,
    or free keywords → {id: API query, label, type}."""
    t = text.strip()
    m = re.search(r"arxiv\.org/list/([\w\-.]+)", t)
    if m:
        t = m.group(1)
    m = re.search(r"arxiv\.org/a/([a-z]+)_([a-z])_\d+", t, re.I)
    if m:
        t = f"au:{m.group(1)}_{m.group(2)}"
    low = t.lower()
    if low.startswith(("au:", "author:")):
        name = t.split(":", 1)[1].strip().strip('"')
        return {"id": f'au:"{name}"' if " " in name else f"au:{name}", "label": name, "type": "author"}
    if low.startswith("cat:"):
        t = t[4:].strip()
    if re.fullmatch(r"[a-z\-]+(\.[A-Za-z\-]+)?", t) and (
            "." in t or "-" in t or t in ("math", "cs", "physics", "stat", "eess", "econ", "q-bio", "q-fin", "astro-ph", "cond-mat", "nlin")):
        cat = t if "." in t or "-" in t and t not in ("astro-ph", "cond-mat") else t + ".*"
        if t in ("astro-ph", "cond-mat"):
            cat = t + ".*"
        return {"id": f"cat:{cat}", "label": t, "type": "category"}
    q = t.strip('"')
    return {"id": f'all:"{q}"' if " " in q else f"all:{q}", "label": q, "type": "search"}

def arxiv_search(q="", author="", date_from="", date_to="", sort="submittedDate", max_results=50):
    parts = []
    if q.strip():
        qq = q.strip()
        if qq.startswith('"') and qq.endswith('"'):
            parts.append(f"all:{qq}")
        else:
            parts.append("(" + " AND ".join(f"all:{w}" for w in re.findall(r"[\w\-]+", qq)) + ")")
    if author.strip():
        a = author.strip().strip('"')
        parts.append(f'au:"{a}"' if " " in a else f"au:{a}")
    if date_from or date_to:
        f = re.sub(r"\D", "", date_from)[:8] or "19910101"
        t = re.sub(r"\D", "", date_to)[:8] or time.strftime("%Y%m%d")
        parts.append(f"submittedDate:[{f}0000 TO {t}2359]")
        if not (q.strip() or author.strip()):
            cats = [s["id"] for s in CONFIG["arxiv"]["queries"] if s.get("qtype") == "category"]
            if not cats:
                raise RuntimeError("a date range needs keywords, an author, or at least one followed category")
            parts.append("(" + " OR ".join(cats) + ")")
    if not parts:
        raise RuntimeError("type keywords, an author or a date range")
    return arxiv_api(" AND ".join(parts), max_results=max_results, sort=sort)

def fetch_arxiv(sub):
    items = arxiv_api(sub["id"], max_results=int(CONFIG["arxiv"].get("max_results", 50)))
    for it in items:
        it["cid"] = sub["id"]
    return items

# ---------------------------------------------------------------- refresh loop

def refresh():
    with LOCK:
        if STATE["refreshing"]:
            STATE["rerun"] = True  # something changed mid-refresh (e.g. an import): go again when done
            return
        STATE["refreshing"] = True
    try:
        log("refreshing…")
        sync_accounts()
        errors, items = [], []
        with LOCK:
            jobs = [(k, s) for k, s in all_subs() if not s.get("muted") or k == "yt"]

        def do(kind, sub):
            fn = {"yt": fetch_channel, "tw": tw_fetch_user, "rss": fetch_rss, "arxiv": fetch_arxiv}[kind]
            label = {"yt": "YouTube", "tw": "X", "rss": "Feed", "arxiv": "arXiv"}[kind]
            try:
                r = fn(sub)
                sub.pop("error", None)
                sub["last_fetched"] = time.time()
                return r
            except Exception as ex:
                sub["error"] = str(ex)
                errors.append(f"{label} {sub.get('name') or sub.get('id') or sub.get('ref')}: {ex}")
                return []

        with ThreadPoolExecutor(max_workers=8) as ex:
            lists = list(ex.map(lambda ks: do(*ks), jobs))
        counts = Counter()
        for (k, _), l in zip(jobs, lists):
            counts[k] += len(l)
            items.extend(l)
        save_config()  # resolved ids, names, errors; also makes sure every fetched item has its source row
        save_cache()
        with tx() as db:  # what the feeds returned goes into the items table; the feed itself is read back from there
            write_items(db, items)
            prune_items(db)
        with LOCK:
            STATE["items"], STATE["errors"] = load_items(), errors
            STATE["last_refresh"] = time.time()
        log(f"done: {counts['yt']} videos, {counts['tw']} tweets, {counts['rss']} posts, {counts['arxiv']} papers, {len(errors)} errors")
        for e in errors:
            log("  !", e)
    finally:
        with LOCK:
            STATE["refreshing"] = False
            rerun = STATE.pop("rerun", False)
    if rerun:
        refresh()

def refresh_loop():
    while True:
        refresh()
        time.sleep(max(5, CONFIG.get("refresh_minutes", 30)) * 60)

def compile_blocks(words):
    out = []
    for w in words or []:
        w = (w or "").strip()
        if not w:
            continue
        try:
            out.append(re.compile(w[1:-1], re.I) if len(w) > 2 and w[0] == w[-1] == "/" else re.compile(re.escape(w), re.I))
        except re.error:
            pass
    return out

def visible_items():
    """Apply the personal filters at request time, so changing a setting takes effect without a refetch.
    A paper matched by several arXiv queries appears once, carrying every matching query's tags."""
    with LOCK:
        yc = CONFIG["youtube"]
        blocks = compile_blocks(CONFIG["filters"].get("block_words"))
        cap = int(CONFIG.get("max_per_channel") or 0)
        subs = {(k, s.get("id")): s for k, s in all_subs()}
        per = Counter()
        out, papers = [], {}
        for it in STATE["items"]:
            sub = subs.get((it["kind"], it["cid"]))
            if sub is None or sub.get("muted"):
                continue
            if it["kind"] == "yt":
                if yc.get("hide_shorts", True) and it.get("short"):
                    continue
                mm = sub.get("min_minutes")
                if mm is None:
                    mm = yc.get("min_minutes", 0) or 0
                if mm and it.get("dur") is not None and it["dur"] < mm * 60:
                    continue
            if blocks and any(b.search(it["title"]) for b in blocks):
                continue
            if it["kind"] == "arxiv":
                if it["id"] in papers:
                    p = papers[it["id"]]
                    p["tags"] = sorted(set(p["tags"]) | set(sub.get("tags", [])))
                    p["via"] = sorted(set(p["via"]) | {sub.get("name") or sub["id"]})
                    continue
                papers[it["id"]] = {**it, "tags": list(sub.get("tags", [])), "via": [sub.get("name") or sub["id"]]}
                out.append(papers[it["id"]])
                continue
            if cap and per[(it["kind"], it["cid"])] >= cap:
                continue
            per[(it["kind"], it["cid"])] += 1
            out.append({**it, "tags": sub.get("tags", []), "author": sub.get("name") or it["author"]})
        return out[: int(CONFIG.get("max_items", 400))]

def settings_payload():
    z = CONFIG.get("zulip") or {}
    return {"hide_shorts": CONFIG["youtube"].get("hide_shorts", True), "min_minutes": CONFIG["youtube"].get("min_minutes", 0),
            "max_per_channel": CONFIG.get("max_per_channel", 0), "block_words": CONFIG["filters"].get("block_words", []),
            "zulip": {"site": z.get("site", ""), "email": z.get("email", ""), "stream": z.get("stream", "quietfeed"),
                      "api_key_set": bool(z.get("api_key"))},  # the key itself never goes back to the page
            "fallback_browser": CONFIG.get("fallback_browser", ""), "fallback_browser_auto": browser_for_the_rest() or "",
            **CONFIG["ui"]}

def items_payload():
    with LOCK:
        tags = sorted({t for _, s in all_subs() for t in s.get("tags", [])})
        return {"items": visible_items(), "errors": STATE["errors"], "last_refresh": STATE["last_refresh"],
                "refreshing": STATE["refreshing"], "seen": list(SEEN_STATE["seen"]), "saved": list(SEEN_STATE["saved"]),
                "saved_items": [v for v in SEEN_STATE["saved"].values() if isinstance(v, dict)],
                "tags": tags, "counts": {k: len(sub_list(k)) for k in KINDS},
                "arxiv_authors": [s["name"] for s in sub_list("arxiv") if s.get("qtype") == "author"],
                "inbox": inbox_list(),
                "followed": {k: [s.get("id") for s in sub_list(k)] for k in KINDS},
                "settings": settings_payload()}

def update_settings(patch):
    with LOCK:
        if "hide_shorts" in patch:
            CONFIG["youtube"]["hide_shorts"] = bool(patch["hide_shorts"])
        if "min_minutes" in patch:
            CONFIG["youtube"]["min_minutes"] = max(0, int(patch["min_minutes"] or 0))
        if "max_per_channel" in patch:
            CONFIG["max_per_channel"] = max(0, int(patch["max_per_channel"] or 0))
        if "block_words" in patch:
            v = patch["block_words"]
            CONFIG["filters"]["block_words"] = [w.strip() for w in (v if isinstance(v, list) else str(v).splitlines()) if w.strip()]
        for k in ("hide_thumbnails", "calm_titles", "hide_seen", "watch_clipboard", "clipboard_any"):
            if k in patch:
                CONFIG["ui"][k] = bool(patch[k])
        if "fallback_browser" in patch:
            CONFIG["fallback_browser"] = str(patch["fallback_browser"] or "").strip().strip('"')
        if isinstance(patch.get("zulip"), dict):
            z = CONFIG.setdefault("zulip", {})
            for k in ("site", "email", "stream"):
                if k in patch["zulip"]:
                    z[k] = str(patch["zulip"][k]).strip()
            if patch["zulip"].get("api_key"):  # an empty field means "keep the one I already saved"
                z["api_key"] = str(patch["zulip"]["api_key"]).strip()
            if patch["zulip"].get("clear_key"):
                z["api_key"] = ""
        save_config()
        return settings_payload()

def mark(ids, field, on, item=None, patch=None):
    """seen: id → timestamp. saved: id → a snapshot of the item (plus your tags/note), so it outlives the feed."""
    with LOCK:
        d = SEEN_STATE[field]
        for i in ids:
            if not on:
                d.pop(i, None)
            elif field == "saved":
                cur = d.get(i) if isinstance(d.get(i), dict) else {}
                rec = {**cur, **(item if isinstance(item, dict) else {}), "id": i}
                rec.setdefault("saved_at", int(time.time()))
                if patch:
                    if "tags" in patch:
                        v = patch["tags"]
                        rec["tags_mine"] = sorted({t.strip().lower() for t in (v if isinstance(v, list) else str(v).split(",")) if t.strip()})
                    if "note" in patch:
                        rec["note"] = str(patch["note"])[:2000]
                d[i] = rec
            else:
                d[i] = int(time.time())
        touched = set(ids)
        if field == "seen" and len(d) > 5000:
            keep = dict(sorted(d.items(), key=lambda kv: kv[1])[-4000:])
            touched |= set(d) - set(keep)
            SEEN_STATE["seen"] = keep
        with tx() as db:
            write_marks(db, field, touched)

# ---------------------------------------------------------------- import / follow

def parse_ref(tok, default_kind="yt"):
    """A link, '@handle', 'UC…' id, feed URL, arXiv category/author, or a bare name → (kind, ref) or (None, None).
    A multi-word YouTube entry ('Andrew Steele') becomes a name lookup, resolved through channel search."""
    tok = tok.strip().strip("<>\"'`,;")
    if not tok:
        return None, None
    if re.fullmatch(r"UC[\w-]{22}", tok):
        return "yt", tok
    if default_kind == "arxiv" and "://" not in tok:
        return "arxiv", tok
    # things that are unambiguous whatever the dropdown says: arXiv categories/authors, bare domains
    if re.match(r"^(au|author|cat):", tok, re.I) or re.fullmatch(ARXIV_CAT_RE, tok):
        return "arxiv", tok
    if "://" in tok or re.match(r"^(www\.|m\.|mobile\.)?(youtube\.com|youtu\.be|x\.com|twitter\.com|xcancel\.com|nitter\.[\w.]+|arxiv\.org)/", tok, re.I) \
            or (not tok.startswith("@") and re.fullmatch(r"(?:[\w-]+\.)+[a-z]{2,24}(?:/\S*)?", tok, re.I)):
        u = urlparse(tok if "://" in tok else "https://" + tok)
        host = re.sub(r"^(www|m|mobile|music)\.", "", u.netloc.lower())
        parts = [p for p in u.path.split("/") if p]
        if host in ("youtube.com", "youtu.be"):
            if not parts:
                return None, None
            if parts[0] == "channel" and len(parts) > 1 and re.fullmatch(r"UC[\w-]{22}", parts[1]):
                return "yt", parts[1]
            if parts[0].startswith("@"):
                return "yt", parts[0]
            if parts[0] in ("c", "user") and len(parts) > 1:
                return "yt", f"https://www.youtube.com/{parts[0]}/{parts[1]}"
            m = re.search(r"channel_id=(UC[\w-]{22})", u.query)
            if m:
                return "yt", m.group(1)
            return None, None
        if host in ("x.com", "twitter.com", "xcancel.com") or host.startswith("nitter."):
            if parts and re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0]) and parts[0].lower() not in ("i", "home", "search", "explore", "settings", "intent"):
                return "tw", parts[0].lower()
            m = re.search(r"user_id=(\d+)", u.query)
            if m:
                return "tw", "id:" + m.group(1)
            return None, None
        if host == "arxiv.org":
            return "arxiv", tok
        return "rss", tok  # any other site: look for its feed
    if tok.startswith("@"):
        return default_kind, tok if default_kind == "yt" else tok[1:].lower()
    if default_kind == "yt" and " " in tok:
        return "yt", "name:" + tok
    if re.fullmatch(r"[A-Za-z0-9_.\-]+", tok):
        if default_kind == "rss":
            return "rss", tok
        return default_kind, ("@" + tok) if default_kind == "yt" else tok.lower()
    return None, None

def parse_import(text, filename="", default_kind="yt"):
    """Google Takeout subscriptions.csv, OPML, X archive following.js, or plain lines → ([{kind, ref, name}], [bad tokens]).
    Plain input is split on newlines and commas only, so names with spaces survive."""
    t = text.lstrip("﻿").strip()
    head = t[:4000].lower()
    entries = []
    if "<opml" in head or "<outline" in head:
        for o in ET.fromstring(t).iter("outline"):
            u = o.get("xmlUrl") or o.get("xmlurl") or o.get("htmlUrl") or ""
            m = re.search(r"channel_id=(UC[\w-]{22})", u) or re.search(r"/channel/(UC[\w-]{22})", u)
            if m:
                entries.append({"kind": "yt", "ref": m.group(1), "name": o.get("title") or o.get("text") or ""})
            elif u.startswith("http"):
                entries.append({"kind": "rss", "ref": u, "name": o.get("title") or o.get("text") or ""})
        return entries, []
    if "window.ytd.following" in head or filename.lower().endswith("following.js"):
        return [{"kind": "tw", "ref": "id:" + i, "name": ""} for i in re.findall(r'"accountId"\s*:\s*"(\d+)"', t)], []
    first = t.splitlines()[0].lower() if t else ""
    if "channel id" in first or "channel url" in first or "channel title" in first:
        rd = csv.DictReader(io.StringIO(t))
        for row in rd:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            cid = row.get("channel id") or ""
            if not re.fullmatch(r"UC[\w-]{22}", cid):
                m = re.search(r"(UC[\w-]{22})", row.get("channel url", ""))
                cid = m.group(1) if m else ""
            if cid:
                entries.append({"kind": "yt", "ref": cid, "name": row.get("channel title", "")})
        return entries, []
    bad = []
    for tok in re.split(r"[\r\n,;]+", t):
        tok = tok.strip()
        if not tok:
            continue
        if " " in tok and ("://" in tok or tok.startswith("@")):  # 'Name https://…' or '@x some note': keep the link/handle only
            tok = next((p for p in tok.split() if "://" in p or p.startswith("@")), tok)
        kind, ref = parse_ref(tok, default_kind)
        if ref:
            entries.append({"kind": kind, "ref": ref, "name": ""})
        else:
            bad.append(tok)
    return entries, bad

def add_entries(entries, trigger_refresh=True):
    """Follow each parsed entry. YouTube ids with a name are added instantly (avatar filled in on refresh);
    handles/URLs/names are resolved in parallel; X numeric ids are resolved now or queued. Returns a summary."""
    added, skipped, failed, pending = [], 0, [], []
    seen, todo = set(), []
    for e in entries:
        key = (e["kind"], e["ref"].lower())
        if key not in seen:
            seen.add(key)
            todo.append(e)

    def one(e):
        try:
            k, ref = e["kind"], e["ref"]
            if k == "yt":
                if re.fullmatch(r"UC[\w-]{22}", ref):
                    if find_sub("yt", ref):
                        return "skip", e
                    if e.get("name"):
                        return "add", {"id": ref, "name": e["name"], "avatar": e.get("avatar", "")}
                if ref.startswith("name:"):
                    info = yt_match_by_name(ref[5:])
                else:
                    try:
                        info = yt_channel_info(ref)
                    except urllib.error.HTTPError as ex:
                        if ex.code != 404 or not ref.startswith("@"):
                            raise
                        info = yt_match_by_name(ref[1:])  # '@Andrew Steele' is a name, not a handle
                return ("skip" if find_sub("yt", info["id"]) else "add"), info
            if k == "tw":
                if ref.startswith("id:"):
                    h = tw_resolve_id(ref[3:])
                    if not h:
                        return "pending", ref[3:]
                    return ("skip" if find_sub("tw", h) else "add"), {"id": h}
                return ("skip" if find_sub("tw", ref) else "add"), {"id": ref}
            if k == "rss":
                info = rss_resolve(ref)
                if e.get("name"):
                    info["name"] = e["name"]
                return ("skip" if find_sub("rss", info["id"]) else "add"), info
            if k == "arxiv":
                r = arxiv_ref(ref)
                if find_sub("arxiv", r["id"]):
                    return "skip", e
                if not arxiv_api(r["id"], max_results=1):
                    return "fail", f"{ref}: arXiv returns nothing for that (unknown category or author?)"
                return "add", {"id": r["id"], "name": r["label"], "qtype": r["type"]}
            return "fail", f"{ref}: unknown kind"
        except Exception as ex:
            return "fail", f"{e['ref']}: {ex}"

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(one, todo))
    for e, (status, payload) in zip(todo, results):
        if status == "add":
            if add_sub(e["kind"], payload):
                added.append({"kind": e["kind"], **payload})
            else:
                skipped += 1
        elif status == "skip":
            skipped += 1
        elif status == "pending":
            pending.append(payload)
        else:
            failed.append(payload)
    if pending:
        with LOCK:
            cur = CONFIG["twitter"].setdefault("pending_ids", [])
            cur.extend(p for p in pending if p not in cur)
            save_config()
    if added and trigger_refresh:
        threading.Thread(target=refresh, daemon=True).start()
    return {"added": added, "skipped": skipped, "failed": failed, "pending": len(pending)}

def resolve_pending_x(limit=100):
    """X archive ids that could not be resolved at import time are retried here, a batch per refresh."""
    pend = list(CONFIG["twitter"].get("pending_ids", []))[:limit]
    if not pend:
        return 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        handles = list(ex.map(tw_resolve_id, pend))
    done = []
    for num, h in zip(pend, handles):
        if h:
            add_sub("tw", {"id": h})
            done.append(num)
    if done:
        with LOCK:
            CONFIG["twitter"]["pending_ids"] = [p for p in CONFIG["twitter"]["pending_ids"] if p not in done]
            save_config()
    return len(done)

def import_from_browser(browser):
    """Read youtube.com cookies from a local browser (via yt-dlp's cookie extractor) and page through
    youtube.com/feed/channels. Nothing is opened in the browser itself, so a browser-level block stays intact."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        raise RuntimeError("this needs yt-dlp as a Python module: pip install yt-dlp")
    jar = extract_cookies_from_browser(browser)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", **(headers or {})})
        return opener.open(req, timeout=25).read().decode("utf-8", "ignore")

    page = get("https://www.youtube.com/feed/channels")
    found, tokens = {}, []

    def collect(node):
        for key in ("channelRenderer", "gridChannelRenderer"):
            for c in walk(node, key):
                cid = c.get("channelId")
                if cid:
                    thumbs = c.get("thumbnail", {}).get("thumbnails", [])
                    found[cid] = {"id": cid, "name": rtext(c.get("title")),
                                  "avatar": abs_url(thumbs[-1]["url"]) if thumbs else ""}
        for c in walk(node, "continuationItemRenderer"):
            for tok in walk(c, "token"):
                tokens.append(tok)

    if '"LOGGED_IN":true' not in page:
        raise RuntimeError(f"{browser} is not signed in to YouTube (YouTube served the anonymous page)")
    collect(yt_initial_data(page))
    if not found:
        raise RuntimeError("signed in, but no subscriptions found on the page; YouTube may have changed its layout")
    key_m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
    ver_m = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page)
    sapisid = next((c.value for c in jar if c.name in ("SAPISID", "__Secure-3PAPISID") and "youtube.com" in c.domain), None)
    headers = {"Content-Type": "application/json", "Origin": "https://www.youtube.com", "X-Origin": "https://www.youtube.com",
               "X-Youtube-Client-Name": "1", "X-Youtube-Client-Version": ver_m.group(1) if ver_m else "2.20241001.00.00"}
    if sapisid:
        ts = int(time.time())
        h = hashlib.sha1(f"{ts} {sapisid} https://www.youtube.com".encode()).hexdigest()
        headers["Authorization"] = f"SAPISIDHASH {ts}_{h}"
    ctx = {"client": {"clientName": "WEB", "clientVersion": headers["X-Youtube-Client-Version"], "hl": "en"}}
    done = set()
    while tokens and len(done) < 200 and key_m:
        tok = tokens.pop(0)
        if tok in done:
            continue
        done.add(tok)
        try:
            body = json.dumps({"context": ctx, "continuation": tok}).encode()
            collect(json.loads(get(f"https://www.youtube.com/youtubei/v1/browse?key={key_m.group(1)}&prettyPrint=false", body, headers)))
        except Exception as ex:
            log("  continuation failed:", ex)
            break
    return list(found.values())

# ---------------------------------------------------------------- account sync (official YouTube Data API, read-only OAuth)

GOOGLE_SECRET_PATH = os.path.join(HERE, "client_secret.json")
GOOGLE_TOKEN_PATH = os.path.join(HERE, "google_token.json")
YT_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
RUN_PORT = PORT
SYNC = {"state": None, "google": None, "browser": None, "x": None}

def google_client():
    d = load_json(GOOGLE_SECRET_PATH, None)
    if not d:
        raise RuntimeError("client_secret.json is missing next to quietfeed (README → 'YouTube account sync')")
    c = d.get("installed") or d.get("web") or {}
    if not c.get("client_id") or not c.get("client_secret"):
        raise RuntimeError("client_secret.json has no client_id / client_secret")
    return c["client_id"], c["client_secret"]

def google_redirect_uri():
    return f"http://localhost:{RUN_PORT}/oauth/google"

def google_auth_url():
    cid, _ = google_client()
    SYNC["state"] = hashlib.sha1(os.urandom(16)).hexdigest()
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": cid, "redirect_uri": google_redirect_uri(), "response_type": "code",
        "scope": YT_SCOPE, "access_type": "offline", "prompt": "consent", "state": SYNC["state"]})

def google_token_request(params):
    cid, sec = google_client()
    body = urlencode({**params, "client_id": cid, "client_secret": sec}).encode()
    try:
        r = fetch("https://oauth2.googleapis.com/token", timeout=20, data=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as e:
        raise RuntimeError("Google token error: " + e.read().decode("utf-8", "ignore")[:300])
    return json.loads(r)

def google_finish(code, state):
    if not state or state != SYNC.get("state"):
        raise RuntimeError("sign-in state mismatch; start the connection again from the page")
    tok = google_token_request({"code": code, "redirect_uri": google_redirect_uri(), "grant_type": "authorization_code"})
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    old = load_json(GOOGLE_TOKEN_PATH, {})
    if not tok.get("refresh_token") and old.get("refresh_token"):
        tok["refresh_token"] = old["refresh_token"]
    save_json(GOOGLE_TOKEN_PATH, tok)
    SYNC["state"] = None
    SYNC["google"] = {"ok": True, "note": "connected", "at": time.time()}

def google_access_token():
    tok = load_json(GOOGLE_TOKEN_PATH, None)
    if not tok:
        return None
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise RuntimeError("Google session expired; connect again")
    new = google_token_request({"refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    tok.update(new)
    tok["expires_at"] = time.time() + int(new.get("expires_in", 3600)) - 60
    save_json(GOOGLE_TOKEN_PATH, tok)
    return tok["access_token"]

def yt_account_subscriptions():
    """Every channel the connected Google account subscribes to. None when no account is connected."""
    at = google_access_token()
    if not at:
        return None
    out, page = [], ""
    while True:
        q = {"part": "snippet", "mine": "true", "maxResults": 50}
        if page:
            q["pageToken"] = page
        try:
            r = json.loads(fetch("https://www.googleapis.com/youtube/v3/subscriptions?" + urlencode(q),
                                 timeout=20, headers={"Authorization": "Bearer " + at}))
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")
            if e.code == 401:
                raise RuntimeError("Google rejected the token; connect again")
            raise RuntimeError(f"YouTube API {e.code}: {msg[:200]}")
        for it in r.get("items", []):
            sn = it.get("snippet", {})
            cid = sn.get("resourceId", {}).get("channelId")
            if cid:
                out.append({"id": cid, "name": sn.get("title", ""),
                            "avatar": sn.get("thumbnails", {}).get("default", {}).get("url", "")})
        page = r.get("nextPageToken")
        if not page:
            return out

def sync_accounts():
    """Runs at the start of every refresh. Mirrors *new* follows from the connected YouTube account, from a
    browser's YouTube session if configured, and resolves queued X ids. It only ever adds: unfollowing
    stays something you do on purpose, in the app."""
    try:
        chans = yt_account_subscriptions()
        if chans is not None:
            r = add_entries([{"kind": "yt", "ref": c["id"], "name": c["name"], "avatar": c["avatar"]} for c in chans], trigger_refresh=False)
            SYNC["google"] = {"ok": True, "count": len(chans), "added": len(r["added"]), "at": time.time()}
            if r["added"]:
                log(f"  YouTube account sync: {len(r['added'])} new of {len(chans)}")
    except Exception as ex:
        SYNC["google"] = {"ok": False, "error": str(ex), "at": time.time()}
        log("  YouTube account sync failed:", ex)
    b = (CONFIG.get("sync") or {}).get("browser")
    if b:
        try:
            chans = import_from_browser(b)
            r = add_entries([{"kind": "yt", "ref": c["id"], "name": c["name"], "avatar": c["avatar"]} for c in chans], trigger_refresh=False)
            SYNC["browser"] = {"ok": True, "count": len(chans), "added": len(r["added"]), "at": time.time()}
        except Exception as ex:
            SYNC["browser"] = {"ok": False, "error": str(ex), "at": time.time()}
            log(f"  {b} sync failed:", ex)
    try:
        n = resolve_pending_x()
        SYNC["x"] = {"ok": True, "resolved": n, "pending": len(CONFIG["twitter"].get("pending_ids", [])), "at": time.time()}
    except Exception as ex:
        SYNC["x"] = {"ok": False, "error": str(ex), "at": time.time()}

def sync_payload():
    return {"google": {"configured": os.path.exists(GOOGLE_SECRET_PATH), "connected": os.path.exists(GOOGLE_TOKEN_PATH), **(SYNC["google"] or {})},
            "browser": {"name": (CONFIG.get("sync") or {}).get("browser", ""), **(SYNC["browser"] or {})},
            "x": {"pending": len(CONFIG["twitter"].get("pending_ids", [])), **(SYNC["x"] or {})}}

OAUTH_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>quietfeed</title>
<body style="font:16px Georgia,serif;background:#f6f4ee;color:#1d1d1b;padding:40px;max-width:520px;margin:auto">
<p>%s</p><p><a href="http://localhost:%d/#following">back to quietfeed</a> — you can close this tab.</p>"""

# ---------------------------------------------------------------- playback

def find_exe(name):
    """PATH first, then next to this program, then the places winget, scoop, mpv.net and `pip install --user` use."""
    p = shutil.which(name)
    if p:
        return p
    import sysconfig
    la, pf = os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")
    home = os.path.expanduser("~")
    dirs = [HERE, os.path.join(la, "Microsoft", "WinGet", "Links"), os.path.join(home, "scoop", "shims"),
            os.path.join(pf, "MPV Player"), os.path.join(pf, "mpv.net"), os.path.join(pf, "mpv"), os.path.join(la, "Programs", "mpv.net"),
            "/opt/homebrew/bin", "/usr/local/bin", "/Applications/mpv.app/Contents/MacOS", os.path.join(home, ".local", "bin"),
            sysconfig.get_path("scripts", f"{os.name}_user") or "", os.path.dirname(sys.executable)]
    for d in dirs:
        for ext in ("", ".exe", ".com", ".cmd"):
            c = os.path.join(d, name + ext)
            if d and os.path.isfile(c):
                return c
    return None

def ytdlp_exe():
    """A yt-dlp executable for mpv to call. The frozen quietfeed.exe can answer as one itself."""
    return find_exe("yt-dlp") or (sys.executable if FROZEN else None)

def play(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [a.replace("{url}", url) for a in CONFIG.get("player", [])]
    exe = find_exe(cmd[0]) if cmd else None
    if not exe:
        for alt in ("mpv", "mpvnet"):  # the configured player is missing; use any mpv flavour that is installed
            exe = find_exe(alt)
            if exe:
                cmd = [alt, "--ytdl-format=bestvideo[height<=1080]+bestaudio/best", url]
                break
    if not exe:
        return download(video_id, open_after=True)
    cmd[0] = exe
    ytdl = ytdlp_exe()
    if ytdl and os.path.basename(exe).lower().startswith("mpv") and not shutil.which("yt-dlp"):
        # mpv streams YouTube through yt-dlp; tell it where the executable is when it is not on PATH
        cmd.insert(1, "--script-opts=ytdl_hook-ytdl_path=" + ytdl)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"playing in {os.path.basename(exe)}"

def download(video_id, open_after=False):
    try:
        import yt_dlp
    except ImportError:
        return "yt-dlp not found: run `pip install yt-dlp` (and install mpv for streaming)"
    d = CONFIG.get("download_dir") or os.path.join(HERE, "downloads")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "%(title)s [%(id)s].%(ext)s")

    def run():
        try:
            with yt_dlp.YoutubeDL({"format": "bestvideo[height<=1080]+bestaudio/best", "outtmpl": out,
                                   "quiet": True, "no_warnings": True, "noprogress": True}) as y:
                y.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as ex:
            log("download failed:", ex)
            return
        if open_after:
            for f in os.listdir(d):
                if f"[{video_id}]" in f and not f.endswith(".part"):
                    p = os.path.join(d, f)
                    if hasattr(os, "startfile"):
                        os.startfile(p)
                    else:
                        subprocess.Popen(["xdg-open", p])
                    break
    threading.Thread(target=run, daemon=True).start()
    return "downloading to " + d + (" (will open when done)" if open_after else "")

# ---------------------------------------------------------------- images (proxied so the browser never talks to Google)

def avatar_bytes(kind, id_):
    key = f"{kind}-{id_}"
    if key in AVATARS:
        return AVATARS[key]
    os.makedirs(AVATAR_DIR, exist_ok=True)
    p = os.path.join(AVATAR_DIR, re.sub(r"[^\w-]", "", key) + ".jpg")
    if os.path.exists(p):
        with open(p, "rb") as f:
            AVATARS[key] = f.read()
        return AVATARS[key]
    sub = find_sub(kind, id_)
    url = (sub or {}).get("avatar") or ""
    host = urlparse(url).netloc
    if not url or not (host.endswith("ggpht.com") or host.endswith("googleusercontent.com")):
        return b""
    data = fetch(url, timeout=10)
    with open(p, "wb") as f:
        f.write(data)
    AVATARS[key] = data
    return data

# ---------------------------------------------------------------- links from elsewhere (the inbox)

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+")

def link_kind(url):
    """Which kind of thing a link points at: 'yt', 'tw', 'arxiv', 'page', or None when it is not even a link."""
    u = urlparse(url if "://" in url else "https://" + url)
    host = re.sub(r"^(www|m|mobile|music)\.", "", u.netloc.lower())
    if host in ("youtube.com", "youtu.be", "youtube-nocookie.com"):
        return "yt"
    if host in ("x.com", "twitter.com", "xcancel.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com") or host.startswith("nitter."):
        return "tw"
    if host == "arxiv.org":
        return "arxiv"
    return "page" if host and "." in host else None

def resolve_link(url, via=""):
    """A link from anywhere → the item the feed would have shown for it, fetched by this program.
    Also says what to follow to get more of the same ('follow': {kind, ref, name})."""
    url = url.strip().strip("<>\"'")
    if "://" not in url:
        url = "https://" + url
    kind = link_kind(url)
    u = urlparse(url)
    host = re.sub(r"^(www|m|mobile|music)\.", "", u.netloc.lower())
    parts = [p for p in u.path.split("/") if p]
    qs = parse_qs(u.query)
    now = time.time()
    if kind == "yt":
        vid = None
        if host == "youtu.be" and parts:
            vid = parts[0]
        elif parts and parts[0] in ("shorts", "live", "embed", "v") and len(parts) > 1:
            vid = parts[1]
        elif qs.get("v"):
            vid = qs["v"][0]
        if vid and re.fullmatch(r"[\w-]{11}", vid):
            watch = f"https://www.youtube.com/watch?v={vid}"
            info = json.loads(fetch("https://www.youtube.com/oembed?" + urlencode({"url": watch, "format": "json"}), timeout=15))
            it = {"kind": "yt", "id": vid, "title": info.get("title", ""), "author": info.get("author_name", ""), "ts": now, "desc": "", "link": watch}
            try:  # duration, channel id and upload date from the watch page's player data
                page = yt_page(watch)
                vd = page.find('"videoDetails":{')
                seg = page[vd:vd + 3000] if vd >= 0 else page
                m = re.search(r'"lengthSeconds":"(\d+)"', seg)
                if m:
                    it["dur"] = int(m.group(1))
                    cache_put("durations", vid, it["dur"])
                m = re.search(r'"channelId":"(UC[\w-]{22})"', seg)
                if m:
                    it["follow"] = {"kind": "yt", "ref": m.group(1), "name": it["author"]}
                m = re.search(r'"(?:publishDate|uploadDate)":"(\d{4}-\d{2}-\d{2})', page)
                if m:
                    it["ts"] = iso_ts(m.group(1) + "T12:00:00+00:00") or now
            except Exception as ex:
                log("  watch page", vid, ex)
            it["short"] = "/shorts/" in url
            return it
        if parts and (parts[0].startswith("@") or parts[0] in ("channel", "c", "user")):
            k, ref = parse_ref(url, "yt")
            info = yt_channel_info(ref)
            return {"kind": "follow", "id": url, "title": info["name"], "author": info.get("handle", ""), "ts": now, "link": url,
                    "follow": {"kind": "yt", "ref": info["id"], "name": info["name"]}}
        raise RuntimeError("that YouTube link is not a video or a channel")
    if kind == "tw":
        m = re.search(r"/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)", u.path)
        if m:
            user, sid = m.group(1), m.group(2)
            canon = f"https://x.com/{user}/status/{sid}"
            o = json.loads(fetch("https://publish.twitter.com/oembed?" + urlencode({"url": f"https://twitter.com/{user}/status/{sid}", "omit_script": "true", "dnt": "true", "hide_thread": "true"}), timeout=15))
            h = o.get("html", "")
            pm = re.search(r"<p[^>]*>(.*?)</p>", h, re.S)
            text = strip_html(pm.group(1).replace("<br>", "\n") if pm else h, 2000)
            am = re.search(r"&mdash;\s*(.*?)\s*\(@(\w+)\)\s*<a[^>]*>([^<]*)</a>", h)
            ts = now
            if am:
                try:
                    ts = datetime.strptime(am.group(3).strip(), "%B %d, %Y").timestamp()
                except Exception:
                    pass
            handle = (am.group(2) if am else user).lower()
            return {"kind": "tw", "id": canon, "title": text, "author": "@" + handle, "ts": ts, "desc": "", "link": canon,
                    "follow": {"kind": "tw", "ref": handle, "name": html.unescape(am.group(1)) if am else "@" + handle}}
        if parts and re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0]) and parts[0].lower() not in ("i", "home", "search", "explore", "settings", "intent"):
            handle = parts[0].lower()
            return {"kind": "follow", "id": url, "title": "@" + handle + " on X", "author": "", "ts": now, "link": f"https://x.com/{handle}",
                    "follow": {"kind": "tw", "ref": handle, "name": "@" + handle}}
        raise RuntimeError("that X link is not a post or a profile")
    if kind == "arxiv":
        m = re.search(r"/(?:abs|pdf|html|format)/([\w.\-]+?)(?:v\d+)?(?:\.pdf)?/?$", u.path) or re.search(r"(\d{4}\.\d{4,5})", url)
        if not m:
            r = arxiv_ref(url)
            return {"kind": "follow", "id": url, "title": "arXiv " + r["label"], "author": r["type"], "ts": now, "link": url,
                    "follow": {"kind": "arxiv", "ref": r["label"] if r["type"] != "author" else "au:" + r["label"], "name": r["label"]}}
        items = arxiv_api(id_list=m.group(1), max_results=1)
        if not items:
            raise RuntimeError(f"arXiv knows no paper {m.group(1)}")
        it = items[0]
        it["follow"] = {"kind": "arxiv", "ref": "au:" + it["authors"][0], "name": it["authors"][0]} if it.get("authors") else None
        return it
    # any other page: title, description, site, and the feed it advertises
    page = fetch(url, timeout=20, headers={"Accept-Language": "en-US,en;q=0.9"}).decode("utf-8", "ignore")
    title = meta_content(page, "og:title") or html.unescape(re.sub(r"\s+", " ", (re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I) or [None, ""])[1])).strip()
    desc = meta_content(page, "og:description") or meta_content(page, "description")
    site = meta_content(page, "og:site_name") or host
    pub = meta_content(page, "article:published_time")
    it = {"kind": "rss", "id": url, "title": title or url, "author": site, "ts": iso_ts(pub) or now, "desc": strip_html(desc, 400), "link": url, "by": meta_content(page, "author")}
    fm = re.search(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*>', page, re.I)
    hm = re.search(r'href="([^"]+)"', fm.group(0)) if fm else None
    if hm:
        href = html.unescape(hm.group(1))
        it["follow"] = {"kind": "rss", "ref": href if href.startswith("http") else f"{u.scheme}://{u.netloc}/" + href.lstrip("/"), "name": site}
    return it

def inbox_take(url, via=""):
    """Resolve a link and put it in the inbox (or refresh it if it is already there). Returns the item."""
    it = resolve_link(url, via)
    it["via"] = via
    inbox_add(url.strip(), it, via)
    log(f"  inbox ({via}): {it.get('kind')} {it.get('title', '')[:60]}")
    return it

def clipboard_text():
    """Current clipboard text, or None. Windows through ctypes, macOS through pbpaste, Linux through xclip/wl-paste."""
    try:
        if sys.platform == "win32":
            import ctypes
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            u32.GetClipboardData.restype = ctypes.c_void_p
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            if not u32.IsClipboardFormatAvailable(13) or not u32.OpenClipboard(0):  # 13 = CF_UNICODETEXT
                return None
            try:
                h = u32.GetClipboardData(13)
                if not h:
                    return None
                p = k32.GlobalLock(h)
                try:
                    return ctypes.c_wchar_p(p).value
                finally:
                    k32.GlobalUnlock(h)
            finally:
                u32.CloseClipboard()
        if sys.platform == "darwin":
            return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3).stdout
        for cmd in (["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"]):
            if shutil.which(cmd[0]):
                return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    return None

def clipboard_loop():
    """Opt-in: every link you copy that quietfeed can show goes to the inbox. Only the text is read, only for links,
    and nothing leaves the machine except the fetch of that link's own page."""
    last, seen = None, set()
    while True:
        time.sleep(1.5)
        if not CONFIG["ui"].get("watch_clipboard"):
            continue
        t = clipboard_text()
        if not t or t == last or len(t) > 4000:
            last = t if t and len(t) <= 4000 else last
            continue
        last = t
        for url in URL_RE.findall(t)[:5]:
            url = url.rstrip(".,;:!?")
            k = link_kind(url)
            if not k or (k == "page" and not CONFIG["ui"].get("clipboard_any")) or url in seen:
                continue
            seen.add(url)
            threading.Thread(target=lambda u=url: _safe_take(u, "clipboard"), daemon=True).start()

def _safe_take(url, via):
    try:
        inbox_take(url, via)
    except Exception as ex:
        log(f"  inbox ({via}) failed for {url}: {ex}")

def system_default_browser():
    """Windows: the executable behind the current default browser for https links, or None.
    Returns None too when that default is quietfeed itself (after --register-handler)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
            prog = winreg.QueryValueEx(k, "ProgId")[0]
        if not prog or prog.lower().startswith("quietfeed"):
            return None
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog + r"\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        m = re.match(r'\s*"([^"]+\.exe)"|\s*(\S+\.exe)', cmd, re.I)
        exe = (m.group(1) or m.group(2)) if m else None
        return exe if exe and os.path.isfile(exe) else None
    except OSError:
        return None

def remember_default_browser():
    """Store the current default browser as the fallback once, while it is still knowable."""
    if CONFIG.get("fallback_browser"):
        return None
    b = system_default_browser()
    if b:
        with LOCK:
            CONFIG["fallback_browser"] = b
            save_config()
        log("  links quietfeed cannot show will go to", b)
    return b

def browser_for_the_rest():
    """The browser `--open` hands non-quietfeed links to: the configured one, else the system default,
    else the first browser found."""
    b = CONFIG.get("fallback_browser") or ""
    if b:
        p = b if os.path.isfile(b) else find_exe(b)
        if p:
            return p
    p = system_default_browser()
    if p:
        return p
    pf, pfx, la = os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""), os.environ.get("LOCALAPPDATA", "")
    for c in [os.path.join(la, "Vivaldi", "Application", "vivaldi.exe"), os.path.join(pf, "Vivaldi", "Application", "vivaldi.exe"),
              os.path.join(pf, "Mozilla Firefox", "firefox.exe"), os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(la, "Google", "Chrome", "Application", "chrome.exe"), os.path.join(pfx, "Microsoft", "Edge", "Application", "msedge.exe"),
              os.path.join(pf, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")]:
        if os.path.isfile(c):
            return c
    for n in ("vivaldi", "firefox", "chrome", "msedge", "brave", "chromium"):
        p = find_exe(n)
        if p:
            return p
    return None

def open_link_cli(url):
    """`quietfeed --open URL`: a video, post, paper or page link goes to the inbox and the page opens on it;
    anything else goes to the normal browser. This is what makes quietfeed usable as the system's link handler."""
    k = link_kind(url)
    if k in ("yt", "tw", "arxiv"):
        import webbrowser
        target = f"http://localhost:{PORT}/open?" + urlencode({"u": url})
        import socket
        with socket.socket() as probe:
            probe.settimeout(0.5)
            running = probe.connect_ex(("127.0.0.1", PORT)) == 0
        if running:
            webbrowser.open(target)
            return 0
        # not running: start the server with this link queued and the page opened on the inbox
        STATE["queued_link"] = url
        return None
    b = browser_for_the_rest()
    if b:
        subprocess.Popen([b, url])
        return 0
    import webbrowser
    webbrowser.open(url)
    return 0

def handler_command():
    exe = sys.executable
    if FROZEN:
        return f'"{exe}" --open "%1"'
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return f'"{pyw if os.path.isfile(pyw) else exe}" "{os.path.abspath(__file__)}" --open "%1"'

def register_handler(on=True):
    """Windows: make quietfeed selectable under Settings → Apps → Default apps → Web browser. After choosing it,
    every link from WhatsApp, mail or any app goes through `--open`. Nothing is changed without you picking it there."""
    if sys.platform != "win32":
        raise RuntimeError("the link handler is Windows-only for now; on macOS use the link box or the clipboard watcher")
    import winreg
    base = r"Software\Clients\StartMenuInternet\quietfeed"
    if not on:
        for key in (base + r"\Capabilities\URLAssociations", base + r"\Capabilities", base + r"\shell\open\command", base + r"\shell\open",
                    base + r"\shell", base, r"Software\Classes\quietfeedURL\shell\open\command", r"Software\Classes\quietfeedURL\shell\open",
                    r"Software\Classes\quietfeedURL\shell", r"Software\Classes\quietfeedURL"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications", 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "quietfeed")
        except OSError:
            pass
        return "quietfeed removed from the browser list"
    cmd = handler_command()

    def put(path, values):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
            for name, val in values.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)

    put(r"Software\Classes\quietfeedURL", {"": "quietfeed link", "URL Protocol": ""})
    put(r"Software\Classes\quietfeedURL\shell\open\command", {"": cmd})
    put(base, {"": "quietfeed"})
    put(base + r"\Capabilities", {"ApplicationName": "quietfeed", "ApplicationDescription": "Opens videos, posts and papers in quietfeed; everything else in your normal browser"})
    put(base + r"\Capabilities\URLAssociations", {"http": "quietfeedURL", "https": "quietfeedURL"})
    put(base + r"\shell\open\command", {"": cmd})
    put(r"Software\RegisteredApplications", {"quietfeed": base + r"\Capabilities"})
    # remember today's default browser before it becomes quietfeed; through the running instance if there is one,
    # so its in-memory settings do not overwrite ours on its next save
    kept = CONFIG.get("fallback_browser") or system_default_browser()
    if kept and not CONFIG.get("fallback_browser"):
        try:
            fetch(f"http://localhost:{PORT}/api/settings", timeout=3, data=json.dumps({"fallback_browser": kept}).encode(),
                  headers={"Content-Type": "application/json"})
        except Exception:
            remember_default_browser()
    return ("registered: now pick quietfeed under Settings → Apps → Default apps → Web browser"
            + (f"\nother links will keep opening in {kept}" if kept else "\nno default browser detected: set fallback_browser in settings"))

# ---------------------------------------------------------------- zulip (where the conversation lives)

def zulip_cfg():
    z = CONFIG.get("zulip") or {}
    site = (z.get("site") or "").strip().rstrip("/")
    if site and not site.startswith("http"):
        site = "https://" + site
    return site, (z.get("email") or "").strip(), (z.get("api_key") or "").strip(), (z.get("stream") or "quietfeed").strip() or "quietfeed"

def zulip_request(path, data=None):
    """One call to the Zulip REST API, authenticated as you (e-mail + personal API key)."""
    site, email, key, _ = zulip_cfg()
    if not (site and email and key):
        raise RuntimeError("Zulip is not set up yet: add your site, e-mail and API key under settings")
    auth = base64.b64encode(f"{email}:{key}".encode()).decode()
    try:
        r = fetch(site + "/api/v1" + path, timeout=20, data=urlencode(data).encode() if data else None,
                  headers={"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")
        try:
            msg = json.loads(msg).get("msg", msg)
        except Exception:
            pass
        raise RuntimeError(f"Zulip said {e.code}: {msg[:200]}")
    return json.loads(r)

def zulip_test():
    me = zulip_request("/users/me")
    return {"ok": True, "name": me.get("full_name", ""), "email": me.get("email", ""), "stream": zulip_cfg()[3]}

def item_link(it):
    k = it.get("kind")
    if k == "yt":
        return f"https://www.youtube.com/watch?v={it.get('id')}"
    return it.get("link") or it.get("id") or ""

def zulip_share(item, note="", topic=""):
    """Post an item and your note to the configured stream. The message is plain Zulip markdown, so
    LaTeX in the note renders there, and the topic defaults to the item's title."""
    k = item.get("kind", "")
    title = (item.get("title") or item.get("id") or "").strip()
    label = {"yt": "video", "tw": "tweet", "rss": "post", "arxiv": "paper"}.get(k, "link")
    meta = [label, item.get("author") or ""]
    if k == "yt" and item.get("dur"):
        meta.append(f"{int(item['dur']) // 60} min")
    if k == "arxiv" and item.get("primary"):
        meta.append(item["primary"])
    lines = [f"**[{title}]({item_link(item)})**" if k != "tw" else f"**{item.get('author', '')}** on X: {item_link(item)}",
             " · ".join(m for m in meta if m)]
    if k == "tw":
        lines.append("> " + title.replace("\n", "\n> "))
    if k == "arxiv" and item.get("pdf"):
        lines.append(f"[pdf]({item['pdf']})")
    if note.strip():
        lines += ["", note.strip()]
    topic = (topic.strip() or title or label)[:60]
    _, _, _, stream = zulip_cfg()
    r = zulip_request("/messages", {"type": "stream", "to": stream, "topic": topic, "content": "\n".join(lines)})
    return {"ok": True, "id": r.get("id"), "stream": stream, "topic": topic}

# ---------------------------------------------------------------- web

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2"}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, (dict, list)):
            body, ctype = json.dumps(body, ensure_ascii=False), "application/json; charset=utf-8"
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith(("text", "application")) else "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) or b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as ex:
            raise RuntimeError(f"request body is not valid UTF-8 JSON ({ex})")

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        try:
            if p.path in ("/", "/index.html", "/app.js", "/style.css"):
                name = "index.html" if p.path == "/" else p.path[1:]
                with open(os.path.join(WEB, name), "rb") as f:
                    return self._send(f.read(), MIME[os.path.splitext(name)[1]])
            if p.path == "/api/items":
                return self._send(items_payload())
            if p.path == "/api/subs":
                return self._send(subs_payload())
            if p.path == "/api/sync":
                return self._send(sync_payload())
            if p.path == "/api/inbox":
                return self._send(inbox_list())
            if p.path == "/open":  # the link handler and bookmarks land here: take the link, show the inbox
                u = q.get("u", [""])[0].strip()
                if u:
                    try:
                        inbox_take(u, "link")
                    except Exception as ex:
                        return self._send(OAUTH_DONE_PAGE % ("Could not open that link: " + html.escape(str(ex)), RUN_PORT))
                self.send_response(302)
                self.send_header("Location", "/#inbox")
                self.end_headers()
                return
            if p.path == "/api/search":
                s = q.get("q", [""])[0].strip()
                return self._send(yt_search(s) if s else [])
            if p.path == "/api/arxiv/search":
                g = lambda k: q.get(k, [""])[0]
                return self._send(arxiv_search(g("q"), g("author"), g("from"), g("to"), g("sort") or "submittedDate"))
            if p.path == "/oauth/google":  # Google sends the browser back here after consent
                if q.get("error"):
                    return self._send(OAUTH_DONE_PAGE % ("Google said: " + html.escape(q["error"][0]), RUN_PORT))
                try:
                    google_finish(q.get("code", [""])[0], q.get("state", [""])[0])
                    threading.Thread(target=refresh, daemon=True).start()
                    return self._send(OAUTH_DONE_PAGE % ("YouTube account connected. Your subscriptions are being mirrored now.", RUN_PORT))
                except Exception as ex:
                    return self._send(OAUTH_DONE_PAGE % ("Could not connect: " + html.escape(str(ex)), RUN_PORT))
            if p.path.startswith("/thumb/"):
                vid = re.sub(r"[^\w-]", "", p.path[7:])
                try:
                    return self._send(fetch(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"), "image/jpeg")
                except Exception:
                    return self._send(b"", "image/jpeg", 404)
            if p.path == "/img":  # search-result avatars; only Google's image hosts are allowed through
                u = q.get("u", [""])[0]
                host = urlparse(u).netloc
                if u.startswith("https://") and (host.endswith("ggpht.com") or host.endswith("googleusercontent.com")):
                    try:
                        return self._send(fetch(u, timeout=10), "image/jpeg")
                    except Exception:
                        pass
                return self._send(b"", "image/jpeg", 404)
            m = re.fullmatch(r"/avatar/(yt|tw)/([\w@.-]+)", p.path)
            if m:
                try:
                    data = avatar_bytes(m.group(1), m.group(2))
                except Exception:
                    data = b""
                return self._send(data, "image/jpeg", 200 if data else 404)
            self._send("not found", code=404)
        except Exception as ex:
            log("GET", p.path, "failed:", ex)
            self._send({"error": str(ex)}, code=500)

    def do_POST(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        vid = re.sub(r"[^\w-]", "", q.get("id", [""])[0])
        try:
            if p.path == "/api/refresh":
                threading.Thread(target=refresh, daemon=True).start()
                return self._send("ok", "text/plain")
            if p.path == "/api/play" and vid:
                mark([vid], "seen", True)
                return self._send(play(vid), "text/plain")
            if p.path == "/api/dl" and vid:
                return self._send(download(vid), "text/plain")
            if p.path == "/api/quit":
                self._send("bye", "text/plain")
                threading.Timer(0.5, lambda: os._exit(0)).start()
                return
            body = self._json()
            if p.path == "/api/mark":
                ids = body.get("ids") or [body.get("id")]
                field = "saved" if body.get("field") == "saved" else "seen"
                mark([i for i in ids if i], field, bool(body.get("on", True)), body.get("item"), body.get("patch"))
                return self._send({"ok": True})
            if p.path == "/api/settings":
                return self._send(update_settings(body))
            if p.path == "/api/zulip/test":
                return self._send(zulip_test())
            if p.path == "/api/inbox":
                u = str(body.get("url", "")).strip()
                if not link_kind(u):
                    return self._send({"error": "that does not look like a link"}, code=400)
                return self._send(inbox_take(u, str(body.get("via") or "box")))
            if p.path == "/api/inbox/remove":
                inbox_remove(str(body.get("url", "")))
                return self._send({"ok": True})
            if p.path == "/api/share":
                if not isinstance(body.get("item"), dict):
                    return self._send({"error": "nothing to share"}, code=400)
                return self._send(zulip_share(body["item"], str(body.get("note", "")), str(body.get("topic", ""))))
            if p.path == "/api/subs/add":
                kind, ref = parse_ref(str(body.get("ref", "")), body.get("kind", "yt"))
                if not ref:
                    return self._send({"error": "could not understand that; paste a link, @handle, channel id, feed URL or arXiv category"}, code=400)
                r = add_entries([{"kind": kind, "ref": ref, "name": body.get("name", "")}])
                if r["failed"]:
                    return self._send({"error": r["failed"][0]}, code=400)
                return self._send(r)
            if p.path == "/api/subs/remove":
                return self._send({"ok": remove_sub(body.get("kind"), body.get("id"))})
            if p.path == "/api/subs/update":
                return self._send({"ok": update_sub(body.get("kind"), body.get("id"), body.get("patch") or {})})
            if p.path == "/api/import":
                entries, bad = parse_import(body.get("text", ""), body.get("filename", ""), body.get("kind", "yt"))
                if not entries:
                    return self._send({"error": "nothing recognisable in that", "bad": bad[:20]}, code=400)
                r = add_entries(entries)
                r["bad"] = bad[:20]
                r["found"] = len(entries)
                return self._send(r)
            if p.path == "/api/sync/google/start":
                return self._send({"url": google_auth_url()})
            if p.path == "/api/sync/google/disconnect":
                if os.path.exists(GOOGLE_TOKEN_PATH):
                    os.remove(GOOGLE_TOKEN_PATH)
                SYNC["google"] = None
                return self._send({"ok": True})
            if p.path == "/api/sync/browser":
                with LOCK:
                    CONFIG.setdefault("sync", {})["browser"] = re.sub(r"[^a-z]", "", str(body.get("browser", "")).lower())
                    save_config()
                SYNC["browser"] = None
                return self._send({"ok": True})
            if p.path == "/api/sync/now":
                threading.Thread(target=refresh, daemon=True).start()
                return self._send({"ok": True})
            if p.path == "/api/import/browser":
                chans = import_from_browser(body.get("browser", "firefox"))
                r = add_entries([{"kind": "yt", "ref": c["id"], "name": c["name"], "avatar": c["avatar"]} for c in chans])
                r["found"] = len(chans)
                return self._send(r)
            self._send({"error": "bad request"}, code=400)
        except Exception as ex:
            log("POST", p.path, "failed:", ex)
            self._send({"error": str(ex)}, code=500)

if NEW_DB and migrate_legacy_json():
    FIRST_RUN = False
    STATE["items"] = load_items()
normalize_subscriptions()

USAGE = """usage: quietfeed.py [--port N] [--no-open] [--export] [--import FILE]
  --port N        listen on another port (default 8765)
  --no-open       do not open the page in a browser
  --export        print everything in quietfeed.db (settings, follows, seen/saved, cache, items) as JSON on stdout and exit
  --import FILE   merge a JSON export, or an old config.json / state.json / cache.json, into quietfeed.db and exit
                  (nothing is removed; stop a running quietfeed first)
  --open URL                 show a video / post / paper link in quietfeed (anything else opens in your browser)
  --register-handler         Windows: make quietfeed selectable as the default web browser (links from any app go through --open)
  --unregister-handler       undo that
"""

if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        sys.exit(0)
    if "--export" in sys.argv:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        json.dump(export_data(), sys.stdout, indent=2, ensure_ascii=False)
        print()
        sys.exit(0)
    if "--import" in sys.argv:
        i = sys.argv.index("--import")
        if i + 1 >= len(sys.argv):
            print(USAGE)
            sys.exit(2)
        with open(sys.argv[i + 1], encoding="utf-8") as f:
            n = import_data(json.load(f))
        log(f"imported into quietfeed.db: {n['follows']} new follows, {n['seen']} seen, {n['saved']} saved, {n['items']} items")
        sys.exit(0)
    if "--register-handler" in sys.argv or "--unregister-handler" in sys.argv:
        print(register_handler("--register-handler" in sys.argv))
        sys.exit(0)
    if "--open" in sys.argv:
        i = sys.argv.index("--open")
        if i + 1 >= len(sys.argv):
            print(USAGE)
            sys.exit(2)
        rc = open_link_cli(sys.argv[i + 1])
        if rc is not None:
            sys.exit(rc)
        # falls through: start the server, which takes the queued link and opens the page on the inbox
    if FIRST_RUN:
        log("Created quietfeed.db — open the page and use the 'following' tab to import your subscriptions.")
    port = PORT
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    RUN_PORT = port
    import socket
    ThreadingHTTPServer.allow_reuse_address = False  # on Windows a "reusable" port can be bound twice; we want a clean failure instead
    with socket.socket() as probe:
        probe.settimeout(0.5)
        already = probe.connect_ex(("127.0.0.1", port)) == 0
    try:
        if already:
            raise OSError("port in use")
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:  # already running (double-clicked twice): just show the page
        log(f"port {port} is busy; quietfeed is probably already running — opening the page")
        if "--no-open" not in sys.argv:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        sys.exit(0)
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=clipboard_loop, daemon=True).start()
    remember_default_browser()
    log(f"quietfeed on http://localhost:{port}  (Ctrl+C to stop)")
    if STATE.get("queued_link"):
        threading.Thread(target=_safe_take, args=(STATE.pop("queued_link"), "link"), daemon=True).start()
        sys.argv.append("--inbox")
    if "--no-open" not in sys.argv:
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}" + ("/#inbox" if "--inbox" in sys.argv else ""))
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
