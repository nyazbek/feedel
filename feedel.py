#!/usr/bin/env python3
"""
feedel — a distraction-free local reader for YouTube channels, X/Twitter accounts,
Substack / RSS feeds and arXiv.

Runs a tiny web server on http://localhost:8765 and shows one chronological list:
no recommendations, no autoplay, no Shorts, no "For you". Everything is fetched
by this script (not by your browser), and videos play in mpv, so browser-level
blockers like Cold Turkey never see a request to youtube.com or x.com.

Setup (Windows):
    winget install -e --id shinchiro.mpv
    pip install yt-dlp            (streaming through mpv, downloads, browser-session sync)
    pip install pywebview pystray pillow   (optional: `--window` shows the page in its own window with a tray icon)
    python feedel.py

Then open the "following" tab in the page to import your subscriptions and follow
new channels, newsletters, arXiv categories and authors. Standard library only.
"""

import sys

# When mpv is pointed at the frozen feedel.exe as its "yt-dlp", it calls it with -J. Answer as yt-dlp.
if len(sys.argv) > 1 and any(a in ("-J", "-j", "--dump-single-json", "--dump-json") for a in sys.argv[1:]):
    import yt_dlp
    sys.exit(yt_dlp.main())

import base64, csv, hashlib, html, io, json, os, re, shutil, sqlite3, subprocess, threading, time, uuid
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
WEB = os.path.join(getattr(sys, "_MEIPASS", HERE), "web")
DB_PATH = os.path.join(HERE, "feedel.db")
for _old in ("quietfeed.db",):  # the program was called quietfeed until October 2026; the data carries over
    if not os.path.exists(DB_PATH) and os.path.exists(os.path.join(HERE, _old)):
        try:
            for _suf in ("", "-wal", "-shm"):
                if os.path.exists(os.path.join(HERE, _old + _suf)):
                    os.replace(os.path.join(HERE, _old + _suf), DB_PATH + _suf)
        except OSError:  # still open by an older copy: keep using it under its old name for this run
            DB_PATH = os.path.join(HERE, _old)
LEGACY_JSON = {k: os.path.join(HERE, k + ".json") for k in ("config", "state", "cache")}  # pre-0.3 files, migrated on first run
LOG_PATH = os.path.join(HERE, "feedel.log")
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
    # YouTube/X/arXiv links to every link. fallback_browser: where `--open` sends links feedel cannot show ("" = auto)
    # window: the page in its own window (pywebview) with a tray icon instead of a browser tab; the size and position it had
    # last time are kept under "window" below
    "ui": {"hide_thumbnails": False, "calm_titles": True, "hide_seen": False, "watch_clipboard": False, "clipboard_any": False,
           "window": False, "auto_update": True},
    # a GitHub token (fine-grained, "Contents: read" on the repository) lets a copy update itself from a private repository
    "github_token": "",
    "window": {"width": 1100, "height": 800, "x": None, "y": None},
    "fallback_browser": "",
    # "browser": name of a browser signed in to YouTube whose subscriptions are re-read on every refresh (needs yt-dlp)
    "sync": {"browser": ""},
    # where the conversation lives: "share" posts an item and your note to `stream`, as you, with your own API key;
    # "streams": where each kind of share goes (videos → #vids, papers → #papers…); empty means the default stream;
    # "extra_streams": other streams the talk tab shows and lets you read, search and post in;
    # "recs_stream": where recommendations between friends go; a message there mentioning you is a recommendation for you
    # your Instagram direct messages, through Meta's API with Instagram login (a professional account and a token from the app dashboard)
    "instagram": {"token": "", "user_id": "", "username": "", "expires": 0},
    # "take to claude": a new conversation on claude.ai with the item in the prompt; {q} is the URL-encoded prompt
    "claude_url": "https://claude.ai/new?q={q}",
    # the daily briefing Claude writes into the messages tab (run by feedel through Claude Code, headless)
    "briefing": {"on": False, "time": "08:00", "days": "daily", "language": "", "name": "", "profile": "", "extra": "", "model": ""},
    "zulip": {"site": "", "email": "", "api_key": "", "stream": "feedel", "streams": {"yt": "", "tw": "", "rss": "", "arxiv": ""},
              "extra_streams": [], "recs_stream": "recommendations",
              # "invite": a reusable invitation link to the group's Zulip; "organizer": whom a friend writes to for access
              "invite": "", "organizer": ""},
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

NO_CONSOLE = sys.stdout is None  # windowed executable; decided once, since pywebview later puts a null stream in its place

def log(*a):
    line = time.strftime("%H:%M:%S") + " " + " ".join(str(x) for x in a)
    if NO_CONSOLE:  # windowed executable: no console
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

def clean_name(s):
    """A channel's name as one line: YouTube hands over names padded with dozens of newlines, which made a row a screen tall."""
    return re.sub(r"\s+", " ", str(s or "")).strip()

def clean_handle(s):
    """A handle as typed, not percent-encoded ('@MinEconom%C3%ADa_AR' → '@MinEconomía_AR')."""
    s = str(s or "").strip()
    return urllib.parse.unquote(s) if "%" in s else s

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
CREATE TABLE IF NOT EXISTS unfollowed (  -- an unfollow is a decision: syncs and imports do not quietly undo it
    user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, ref TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
    removed INTEGER NOT NULL, PRIMARY KEY (user_id, kind, ref));
CREATE TABLE IF NOT EXISTS inbox (  -- links handed to feedel from elsewhere (clipboard, the link box, the URL handler)
    user_id INTEGER NOT NULL REFERENCES users(id), url TEXT NOT NULL, item_json TEXT NOT NULL, via TEXT NOT NULL DEFAULT '',
    added INTEGER NOT NULL, PRIMARY KEY (user_id, url));
CREATE TABLE IF NOT EXISTS messages (  -- the messages tab: notes from Claude (through /mcp), friends' direct messages (Zulip), your replies
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
    channel TEXT NOT NULL,  -- 'claude' | 'zulip' | 'me'
    ext_id TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '', sender_email TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '[]', meta TEXT NOT NULL DEFAULT '{}',
    ts INTEGER NOT NULL, read_at INTEGER, reply_to INTEGER, archived INTEGER NOT NULL DEFAULT 0);
"""

def open_db():
    """Open (creating if needed) feedel.db in WAL mode. Returns (connection, True when it holds no settings yet)."""
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
        rec["name"], rec["handle"] = clean_name(rec.get("name")) or rec["name"], clean_handle(rec.get("handle"))
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
    cache = {"durations": {}, "shorts": {}, "tweets": {}, "links": {}, "threads": {}, "inspire": {}}
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

ITEM_CAP = {"yt": 4000, "tw": 4000, "rss": 2000, "arxiv": 6000, "ig": 500}  # items of each kind kept in memory (the DB keeps 90 days)

def load_items(limit=None):
    """Newest items first, with what the cache now knows about durations and Shorts applied to videos."""
    # the newest of each kind, not the newest overall: a category's hundreds of papers a day would push a channel's
    # older videos (fetched on request) out of memory, and with them out of every page
    with LOCK:
        rows = []
        for k in KINDS + ("ig",):
            rows += DB.execute("SELECT payload_json FROM items WHERE kind = ? ORDER BY ts DESC LIMIT ?", (k, limit or ITEM_CAP.get(k, 4000))).fetchall()
    out = []
    for r in rows:
        it = json.loads(r["payload_json"])
        if it.get("kind") == "yt":
            it["dur"] = CACHE["durations"].get(it["id"], it.get("dur"))
            it["short"] = CACHE["shorts"].get(it["id"], it.get("short", False))
        out.append(it)
    out.sort(key=lambda it: -(it.get("ts") or 0))  # the kinds were read one by one
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

def unfollowed_list():
    with LOCK:
        rows = DB.execute("SELECT kind, ref, name, removed FROM unfollowed WHERE user_id = ? ORDER BY removed DESC", (USER_ID,)).fetchall()
    return [dict(r) for r in rows]

def is_unfollowed(kind, ref):
    with LOCK:
        return DB.execute("SELECT 1 FROM unfollowed WHERE user_id = ? AND kind = ? AND ref = ?", (USER_ID, kind, ref)).fetchone() is not None

def unfollowed_add(kind, ref, name=""):
    with tx() as db:
        db.execute("INSERT OR REPLACE INTO unfollowed (user_id, kind, ref, name, removed) VALUES (?, ?, ?, ?, ?)",
                   (USER_ID, kind, ref, name or "", int(time.time())))

def unfollowed_remove(kind, ref):
    with tx() as db:
        db.execute("DELETE FROM unfollowed WHERE user_id = ? AND kind = ? AND ref = ?", (USER_ID, kind, ref))

def export_data():
    """Everything in the database as one JSON-able dict: the old config / state / cache shapes plus the stored items."""
    with LOCK:
        rows = DB.execute("SELECT payload_json, fetched FROM items ORDER BY ts DESC").fetchall()
        return {"feedel_export": 1, "exported_at": int(time.time()),
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
        + " into feedel.db; the old files are kept as *.json.migrated")
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

def code_mtime():
    """When the code on disk last changed: the script and the page files (the frozen exe counts as one file)."""
    paths = [sys.executable] if FROZEN else [os.path.abspath(__file__)] + [os.path.join(WEB, f) for f in ("index.html", "app.js", "style.css")]
    try:
        return max(os.path.getmtime(p) for p in paths if os.path.exists(p))
    except Exception:
        return 0.0

CODE_MTIME_AT_START = code_mtime()

def code_is_stale():
    """True when the script or the page on disk is newer than the running process: the page the browser
    loads then expects routes this process does not have."""
    return code_mtime() > CODE_MTIME_AT_START + 1

def restart_self():
    """Re-execute this program in place (same arguments), after the response has gone out."""
    log("restarting on request")
    def go():
        time.sleep(0.6)
        try:
            save_cache()
        except Exception:
            pass
        args = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)]) + [a for a in sys.argv[1:] if a != "--inbox"]
        if "--no-open" not in args and not WINDOW.get("show"):
            args.append("--no-open")  # the page that asked is already open (in a window it dies with this process: reopen it)
        # The frozen one-file exe hands its children PyInstaller's _PYI_* variables, which make a child run out of the
        # parent's temporary folder — and the old bootloader deletes that folder once this process is gone, leaving the
        # new one without its files and a "Warning" box behind. Without them the new process unpacks its own folder.
        env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
        try:
            if WINDOW.get("teardown"):
                WINDOW["teardown"]()  # the tray icon and window go now; os._exit alone would leave a ghost tray icon
            subprocess.Popen(args, cwd=HERE, close_fds=True, env=env)
        finally:
            os._exit(0)
    threading.Thread(target=go, daemon=True).start()
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
    name, handle = clean_name(name), clean_handle(handle)
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
        gone = [s for s in lst if s.get("id") == id_]
        lst[:] = [s for s in lst if s.get("id") != id_]
        save_config()
    if gone:
        unfollowed_add(kind, id_, gone[0].get("name") or "")
    return bool(gone)

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

def yt_video_search(q):
    """A deliberate search for videos on YouTube: a flat list of what matched, fetched by this program,
    with no recommendations around it. Shorts and results without a length are left out."""
    page = yt_page("https://www.youtube.com/results?" + urlencode({"search_query": q, "sp": "EgIQAQ=="}))
    out, seen = [], set()
    units = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000,
             "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000, "y": 31536000}  # "10y ago", "3mo ago"
    for v in walk(yt_initial_data(page), "videoRenderer"):
        vid = v.get("videoId")
        dur = parse_duration(rtext(v.get("lengthText")))
        if not vid or vid in seen or dur is None:
            continue
        seen.add(vid)
        owner = (v.get("ownerText") or {}).get("runs") or [{}]
        cid = owner[0].get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", "")
        ts = time.time()
        m = re.search(r"(\d+)\s*(second|minute|hour|day|week|month|year|mo|s|m|h|d|w|y)\b", rtext(v.get("publishedTimeText")))
        if m:
            ts -= int(m.group(1)) * units[m.group(2)]
        cache_put("durations", vid, dur)
        out.append({"kind": "yt", "id": vid, "cid": cid, "title": rtext(v.get("title")), "author": rtext(v.get("ownerText")),
                    "ts": ts, "dur": dur, "short": False, "desc": "", "tags": [],
                    "follow": {"kind": "yt", "ref": cid, "name": rtext(v.get("ownerText"))} if cid else None,
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
    # a Short needs no length; a video listed without one (live stream, premiere) is remembered as None so the channel
    # is not rescanned every refresh for it; the length is filled in when the channel is next scanned for a new upload
    need = [r["id"] for r in raw if r["id"] not in CACHE["shorts"] or (not CACHE["shorts"][r["id"]] and r["id"] not in CACHE["durations"])]
    if need:
        durs, shorts = yt_scan_channel(cid)
        for v, s in durs.items():
            cache_put("shorts", v, False)
            if s is not None or v not in CACHE["durations"]:
                cache_put("durations", v, s)
        for v in shorts:
            cache_put("shorts", v, True)
        for v in need:  # neither tab listed it (live stream, premiere, old): ask YouTube directly
            if v not in CACHE["shorts"]:
                code, _ = head_no_redirect(f"https://www.youtube.com/shorts/{v}")
                if code:
                    cache_put("shorts", v, code == 200)
            if v not in CACHE["durations"] and not CACHE["shorts"].get(v):
                cache_put("durations", v, None)
    for r in raw:
        r["dur"] = CACHE["durations"].get(r["id"])
        r["short"] = CACHE["shorts"].get(r["id"], False)
    return raw

def yt_rel_ts(text):
    """'3 years ago' / 'Streamed 2 months ago' → an approximate timestamp; None when it is not a relative time."""
    m = re.search(r"(\d+)\s*(seconds?|minutes?|hours?|days?|weeks?|months?|years?|mo|[smhdwy])\s+ago", text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    unit = {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week", "mo": "month", "y": "year"}.get(unit, unit.rstrip("s"))
    secs = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}[unit]
    return time.time() - n * secs

def yt_lockup_items(node, cid, author):
    """The videos listed in a channel page (or a continuation of it) as feed items, and the token for the next page."""
    items, tokens = [], []
    for lv in walk(node, "lockupViewModel"):
        vid = lv.get("contentId")
        if not vid:
            m = re.search(r"/vi/([\w-]{11})/", json.dumps(lv.get("contentImage", {})))
            vid = m.group(1) if m else None
        if not vid or len(vid) != 11:
            continue
        meta = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
        title = (meta.get("title") or {}).get("content") or ""
        texts = [t.get("content", "") for t in walk(meta, "text") if isinstance(t, dict)]
        ts = next((yt_rel_ts(t) for t in texts if yt_rel_ts(t)), None)
        dur = None
        for b in walk(lv, "thumbnailBadgeViewModel"):
            s = parse_duration(b.get("text", ""))
            if s is not None:
                dur = s
                break
        items.append({"kind": "yt", "id": vid, "cid": cid, "title": title, "author": author, "ts": ts or 0, "desc": "", "dur": dur,
                      "short": CACHE["shorts"].get(vid, False)})
    for vr in walk(node, "videoRenderer"):  # older layout
        vid = vr.get("videoId")
        if vid:
            items.append({"kind": "yt", "id": vid, "cid": cid, "title": rtext(vr.get("title")), "author": author,
                          "ts": yt_rel_ts(rtext(vr.get("publishedTimeText"))) or 0, "desc": "", "dur": parse_duration(rtext(vr.get("lengthText"))),
                          "short": CACHE["shorts"].get(vid, False)})
    # the page carries several continuations (the "about" popup among them): only the one inside the video grid, or
    # inside a continuation's own items, leads to older videos
    scopes = list(walk(node, "richGridRenderer")) or [v for a in walk(node, "appendContinuationItemsAction") for v in [a]] or [node]
    for scope in scopes:
        for c in walk(scope, "continuationItemRenderer"):
            for tok in walk(c, "token"):
                if isinstance(tok, str):
                    tokens.append(tok)
    return items, (tokens[-1] if tokens else None)

def yt_older(cid, pages=1):
    """Older videos of a channel: the first call reads the channel's videos page, later ones ask YouTube for the
    next page through the continuation the previous one carried (no login). What comes back is merged into the
    feed for followed channels; the items are returned either way. Durations go to the cache."""
    sub = find_sub("yt", cid)
    author = (sub or {}).get("name") or cid
    cur = CACHE.setdefault("ytmore", {}).get(cid) or {}
    got, more = [], None
    for _ in range(max(1, pages)):
        if not cur.get("token"):
            if cur.get("done"):
                break
            page = yt_page(f"https://www.youtube.com/channel/{cid}/videos")
            key_m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
            ver_m = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page)
            items, token = yt_lockup_items(yt_initial_data(page), cid, author)
            cur = {"token": token, "key": key_m.group(1) if key_m else "", "ver": ver_m.group(1) if ver_m else "2.20241001.00.00", "pages": 0}
            # the page itself only repeats what the feed has (its latest uploads); when it holds nothing new, go on to the continuation
            known = {it["id"] for it in STATE["items"] if it["kind"] == "yt" and it.get("cid") == cid}
            fresh = [it for it in items if it["id"] not in known]
            if fresh or not token:
                got += fresh
                if not token:
                    cur["done"] = True
                    break
                continue
        headers = {"Content-Type": "application/json", "Origin": "https://www.youtube.com", "X-Origin": "https://www.youtube.com",
                   "X-Youtube-Client-Name": "1", "X-Youtube-Client-Version": cur["ver"], "User-Agent": UA}
        body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": cur["ver"], "hl": "en"}}, "continuation": cur["token"]}).encode()
        d = json.loads(fetch(f"https://www.youtube.com/youtubei/v1/browse?key={cur['key']}&prettyPrint=false", timeout=20, data=body, headers=headers))
        items, token = yt_lockup_items(d, cid, author)
        cur["token"], cur["pages"] = token, cur.get("pages", 0) + 1
        got += items
        if not token:
            cur["done"] = True
            break
    more = bool(cur.get("token")) and not cur.get("done")
    cache_put("ytmore", cid, cur)
    # the feed's own copy of a video wins (exact time from the RSS); the rest are new, with approximate times
    seen_ids, out = set(), []
    for it in got:
        if it["id"] in seen_ids:
            continue
        seen_ids.add(it["id"])
        if it.get("dur") is not None:
            cache_put("durations", it["id"], it["dur"])
        out.append(it)
    known = {it["id"]: it for it in STATE["items"] if it["kind"] == "yt" and it.get("cid") == cid}
    out = [known.get(it["id"], it) for it in out]
    new = [it for it in out if it["id"] not in known]
    if sub and new:
        merge_items(new)
    return {"items": out, "new": len(new), "more": more}

def yt_older_all(limit=24):
    """One page of older videos for the channels that posted most recently (the feed's own "load more")."""
    with LOCK:
        subs = sorted([s for s in yt_subs() if s.get("id") and not s.get("muted")], key=lambda s: -(s.get("newest") or 0))[:limit]
    new, errors = 0, 0
    def one(s):
        try:
            return yt_older(s["id"])["new"]
        except Exception as ex:
            log("  older videos", s.get("name") or s["id"], ex)
            return -1
    with ThreadPoolExecutor(max_workers=8) as ex:
        for n in ex.map(one, subs):
            if n < 0:
                errors += 1
            else:
                new += n
    save_cache()
    return {"channels": len(subs), "new": new, "errors": errors}

# ---------------------------------------------------------------- twitter

TW_GOOD = {"inst": None}  # the Nitter instance that answered last; tried first, since the dead ones cost seconds per account

def tw_links_from_html(fragment):
    """The links in a Nitter-rendered fragment (an RSS description, a post's content): [{url, text}], with Nitter's
    own hosts turned back into x.com, and without image and hashtag anchors (mentions are kept: the page links them)."""
    out, seen = [], set()
    for href, inner in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', fragment or "", re.S):
        href = html.unescape(href)
        text = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if not text or text.startswith("#") or "/pic/" in href or href.startswith("#"):
            continue
        host = urlparse(href).netloc.lower()
        if host.startswith("nitter.") or host in ("xcancel.com", "nitter.net") or host in {urlparse(i).netloc.lower() for i in CONFIG["twitter"].get("nitter_instances", [])}:
            href = re.sub(r"^https?://[^/]+", "https://x.com", href).split("#")[0]
            text = re.sub(r"^(?:https?://)?[^/]+/", "x.com/", text).split("#")[0]  # a quoted post's link, shown as x.com/…
        if href in seen:
            continue
        seen.add(href)
        out.append({"url": href, "text": text[:120]})
    return out[:12]

def tw_fetch_user(sub):
    user = sub["id"]
    instances = list(CONFIG["twitter"].get("nitter_instances", []))
    if TW_GOOD["inst"] in instances:
        instances.remove(TW_GOOD["inst"])
        instances.insert(0, TW_GOOD["inst"])
    last = "no instances configured"
    for inst in instances:
        try:
            xml = fetch(f"{inst.rstrip('/')}/{user}/rss", timeout=12, headers=FEED_ACCEPT)
            root = ET.fromstring(xml)
            TW_GOOD["inst"] = inst
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
                              "author": creator, "ts": ts, "desc": "", "link": link, "links": tw_links_from_html(it.findtext("description") or "")})
            def media_of(it):  # videos in posts from people you follow are pre-approved: show them
                m = re.search(r"/status/(\d+)", it["id"])
                return tw_syndication(m.group(1)) if m else None

            with ThreadPoolExecutor(max_workers=4) as pool:  # one syndication call per post; four at a time, not one after another
                medias = list(pool.map(media_of, items))
            for it, media in zip(items, medias):
                if media:
                    it.update({k: media[k] for k in ("video", "mp4", "thumb", "dur", "photos", "reply_to", "reply_to_user", "convo", "parent", "quote") if k in media})
                    if media.get("ts"):
                        it["ts"] = media["ts"]
            return items
        except Exception as ex:
            last = f"{inst}: {ex}"
    raise RuntimeError(f"no Nitter instance answered (last: {last})")

def tw_token(tid):
    """The token X's public syndication endpoint expects: ((id / 1e15) * pi) in base 36, zeros and dot removed."""
    x = (int(tid) / 1e15) * 3.141592653589793
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    n, frac, s, f = int(x), 0.0, "", ""
    frac = x - n
    while n:
        s, n = digits[n % 36] + s, n // 36
    for _ in range(12):
        frac *= 36
        d = int(frac)
        f += digits[d]
        frac -= d
    return (s + "." + f).replace("0", "").replace(".", "")

def tw_expand_links(text, entities):
    """X's syndication text carries t.co links; its entities say where they go. The text gets the real address, and
    the links come back as [{url, text}] with the display text X shows."""
    links = []
    for u in (entities or {}).get("urls") or []:
        short, full, disp = u.get("url") or "", u.get("expanded_url") or "", u.get("display_url") or ""
        if short and full:
            text = text.replace(short, full)
            links.append({"url": full, "text": disp or full})
    return text, links

def _tw_context(t):
    """The post a reply answers, or the post a quote-post quotes, as the page shows it above the post itself."""
    if not isinstance(t, dict) or not t.get("id_str"):
        return None
    u = t.get("user") or {}
    user = (u.get("screen_name") or "").lower()
    media = [m for m in t.get("mediaDetails", []) if m.get("type")] or t.get("photos") or []
    text, links = tw_expand_links(html.unescape(t.get("text", "")), t.get("entities"))
    return {"id": t["id_str"], "user": user, "name": u.get("name", ""), "text": text, "links": links,
            "link": f"https://x.com/{user or 'i'}/status/{t['id_str']}", "ts": iso_ts(t.get("created_at")), "media": bool(media)}

def tw_syndication(tid):
    """One public post as JSON (no login): text, author, time, photos and the video's mp4 variants.
    Cached forever by id since a post's media does not change; failures are not cached."""
    cached = CACHE["tweets"].get(tid)
    if cached is not None and "parent" in cached and "links" in cached:  # entries from before the reply/quote/links fields existed are refreshed once
        return cached
    try:
        d = json.loads(fetch(f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&token={tw_token(tid)}",
                             timeout=10, headers={"Accept": "application/json"}))
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("id_str"):
        return None
    text, links = tw_expand_links(html.unescape(d.get("text", "")), d.get("entities"))
    out = {"video": False, "text": text, "links": links, "name": d.get("user", {}).get("name", ""),
           "user": (d.get("user", {}).get("screen_name") or "").lower(), "ts": iso_ts(d.get("created_at")),
           "photos": [p.get("url") for p in d.get("photos", []) if p.get("url")][:4],
           "reply_to": d.get("in_reply_to_status_id_str") or ((d.get("parent") or {}).get("id_str")) or "",
           "reply_to_user": ((d.get("parent") or {}).get("user") or {}).get("screen_name", "").lower(),
           "convo": bool(d.get("conversation_count")),
           "parent": _tw_context(d.get("parent")), "quote": _tw_context(d.get("quoted_tweet"))}
    for m in d.get("mediaDetails", []):
        if m.get("type") in ("video", "animated_gif"):
            vs = sorted([v for v in m.get("video_info", {}).get("variants", []) if v.get("content_type") == "video/mp4" and v.get("url")],
                        key=lambda v: v.get("bitrate") or 0)
            if vs:
                pick = next((v for v in reversed(vs) if (v.get("bitrate") or 0) <= 2_500_000), vs[-1])  # about 720p at most
                out.update({"video": True, "mp4": pick["url"], "thumb": m.get("media_url_https", ""),
                            "dur": ((m.get("video_info", {}).get("duration_millis") or 0) / 1000) or None})
                break
    cache_put("tweets", tid, out)
    return out

def tw_nitter_item(block, tid=None):
    """One Nitter timeline item (a block of its HTML) → {id, link, user, name, text, ts}. The page's own post
    (main-tweet) carries no permalink anchor, so its id is passed in; its text is the full one, which matters
    for long posts that the syndication endpoint cuts at 280 characters."""
    um = re.search(r'class="username"[^>]*>@?([A-Za-z0-9_]+)<', block)
    nm = re.search(r'class="fullname"[^>]*title="([^"]*)"', block)
    lm = re.search(r'class="tweet-link" href="/([A-Za-z0-9_]+)/status/(\d+)', block)
    dm = re.search(r'class="tweet-date"><a[^>]*title="([^"]*)"', block)
    cm = re.search(r'class="tweet-content[^"]*"[^>]*>(.*?)</div>\s*(?:<p class="tweet-published"|<div class="attachments"|<div class="tweet-stats"|<div class="quote|<div class="card|</div>)', block, re.S)
    if not lm and tid and um:
        lm = re.match(r"(.*)/(.*)", f"{um.group(1)}/{tid}")
    if not lm:
        return None
    ts = None
    if dm:
        for fmt in ("%b %d, %Y · %I:%M %p UTC", "%b %d, %Y · %H:%M UTC"):
            try:
                ts = datetime.strptime(html.unescape(dm.group(1)), fmt).replace(tzinfo=timezone.utc).timestamp()
                break
            except Exception:
                pass
    return {"id": lm.group(2), "link": f"https://x.com/{lm.group(1)}/status/{lm.group(2)}",
            "user": (um.group(1) if um else lm.group(1)).lower(), "name": html.unescape(nm.group(1)) if nm else "",
            "text": strip_html(cm.group(1), 2000) if cm else "", "links": tw_links_from_html(cm.group(1)) if cm else [], "ts": ts or time.time(),
            "media": bool(re.search(r'class="attachments"', block))}

def tw_nitter_items(seg):
    out = []
    for b in re.split(r'<div class="timeline-item[^"]*"', seg)[1:]:
        it = tw_nitter_item(b)
        if it and it["id"] not in {o["id"] for o in out}:
            out.append(it)
    return out

def tw_nitter_page(user, tid, want_after=False):
    """A post's conversation page from the first Nitter instance that answers: {before, main, after, replies}.
    Instances disagree on how much of a thread they list, so when the first one shows no continuation and one
    is expected (want_after), a second instance gets a chance and the longer answer wins."""
    best = None
    for inst in CONFIG["twitter"].get("nitter_instances", []):
        try:
            page = fetch(f"{inst.rstrip('/')}/{user}/status/{tid}", timeout=15).decode("utf-8", "ignore")
        except Exception:
            continue
        if 'class="main-tweet"' not in page:
            continue
        def seg(start_marker, end_markers):
            i = page.find(start_marker)
            if i < 0:
                return ""
            ends = [page.find(e, i + 1) for e in end_markers]
            ends = [e for e in ends if e > 0]
            return page[i:min(ends)] if ends else page[i:]
        main_block = seg('class="main-tweet"', ['class="after-tweet', 'class="replies"', 'id="r"'])
        main = tw_nitter_item(main_block, tid)
        got = {"before": tw_nitter_items(seg('class="before-tweet', ['class="main-tweet"'])),
               "main": [main] if main else [],
               "after": tw_nitter_items(seg('class="after-tweet', ['class="replies"', 'id="r"'])),
               "replies": tw_nitter_items(seg('class="replies"', ['<footer', '</body>']))}
        if best is None or len(got["after"]) > len(best["after"]):
            best = got
        if best["after"] or not want_after:
            break
        want_after = False  # one second opinion is enough
    return best

def tw_thread(url):
    """A post with its whole thread: every post of the author's chain from its first post to its last, wherever
    you clicked in it, plus the posts it replies to (by other people) and other people's replies. From Nitter
    conversation pages when an instance answers; otherwise only the parents, from X's public syndication
    endpoint. Cached for twenty minutes."""
    m = re.search(r"/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)", url)
    if not m:
        raise RuntimeError("that is not a link to a post")
    user, tid = m.group(1), m.group(2)
    cached = CACHE["threads"].get(tid)
    if cached and "posts" in cached and cached.get("at", 0) > time.time() - 1200:
        return cached

    def from_syndication(pid, who=""):
        d = tw_syndication(pid) or {}
        return {"id": pid, "link": f"https://x.com/{d.get('user') or who or 'i'}/status/{pid}", "user": (d.get("user") or who).lower(),
                "name": d.get("name", ""), "text": d.get("text", ""), "ts": d.get("ts") or time.time(),
                "media": bool(d.get("video") or d.get("photos"))}

    def extend_from_cache(posts, me):
        # posts of the author already known from the feed (their syndication data is cached) that reply into the thread
        grew = True
        while grew:
            grew = False
            for pid, d in list(CACHE["tweets"].items()):
                if d and pid not in posts and (d.get("user") or "").lower() == me and d.get("reply_to") in posts:
                    posts[pid] = from_syndication(pid, me)
                    grew = True

    page = tw_nitter_page(user, tid, want_after=True)
    if page is None:  # no instance answered: parents only, through the syndication endpoint
        before, cur, hops = [], tid, 0
        while hops < 8:
            d = tw_syndication(cur)
            if not d or not d.get("reply_to"):
                break
            before.insert(0, from_syndication(d["reply_to"], d.get("reply_to_user", "")))
            cur, hops = d["reply_to"], hops + 1
        main = from_syndication(tid, user)
        me = main["user"]
        k = len(before)
        while k > 0 and before[k - 1]["user"] == me:
            k -= 1
        posts = {p["id"]: p for p in before[k:] + [main]}
        extend_from_cache(posts, me)
        posts = sorted(posts.values(), key=lambda p: int(p["id"]))
        index = next((i for i, p in enumerate(posts) if p["id"] == tid), 0)
        out = {"before": before[:k], "tweet": main, "posts": posts, "index": index, "after": posts[index + 1:], "replies": [],
               "source": "syndication", "at": time.time(),
               "note": "no Nitter instance answered: only the posts this one replies to and the ones already in your feed are shown"}
        cache_put("threads", tid, out)
        return out

    main = page["main"][0] if page["main"] else from_syndication(tid, user)
    main["user"] = main["user"].lower()
    me = main["user"]
    before = page["before"]
    k = len(before)  # the author's own posts directly above this one belong to the thread; the rest is "in reply to"
    while k > 0 and before[k - 1]["user"] == me:
        k -= 1
    own_above, others_above = before[k:], before[:k]
    root = own_above[0] if own_above else main
    chain = own_above + [main] + [a for a in page["after"] if a["user"] == me]
    pool = list(page["replies"])
    if root["id"] != main["id"]:  # the continuation is listed in full only on the thread's first post
        rp = tw_nitter_page(root["user"], root["id"], want_after=True)
        if rp:
            chain = [root] + [a for a in rp["after"] if a["user"] == me] + chain
            pool += rp["replies"]
    posts = {p["id"]: p for p in chain}
    # the author's other posts in the conversation that reply into the thread (Nitter files them under replies)
    cands = [p for p in pool if p["user"] == me and p["id"] not in posts]
    for p in sorted(cands, key=lambda p: int(p["id"])):
        d = tw_syndication(p["id"]) or {}
        if d.get("reply_to") in posts:
            posts[p["id"]] = p
    extend_from_cache(posts, me)
    ordered = sorted(posts.values(), key=lambda p: int(p["id"]))
    index = next((i for i, p in enumerate(ordered) if p["id"] == main["id"]), 0)
    out = {"before": others_above, "tweet": main, "posts": ordered, "index": index,
           "after": ordered[index + 1:], "replies": [r for r in page["replies"] if r["user"] != me and r["id"] not in posts][:40],
           "source": "nitter", "at": time.time()}
    for t in out["before"] + out["posts"] + out["replies"]:  # videos in the thread are playable, photos shown
        media = tw_syndication(t["id"]) if t.get("media") else None
        if media and media.get("video"):
            t.update({k: media[k] for k in ("video", "mp4", "thumb", "dur")})
        if media and media.get("photos"):
            t["photos"] = media["photos"][:4]
    cache_put("threads", tid, out)
    return out

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

# ---- the daily announcement. arXiv dates a paper by its submission and announces it a day or two later, in
# one listing per weekday (none on weekends or holidays). A category subscription reads that listing, so every
# paper carries the day it was announced and whether it was new, a cross-list or a replacement.

try:
    from zoneinfo import ZoneInfo
    ARXIV_TZ = ZoneInfo("America/New_York")
except Exception:  # no timezone database on this machine: arXiv's clock is close enough to UTC-4
    ARXIV_TZ = timezone(timedelta(hours=-4))
ANNOUNCE_RANK = {"new": 0, "cross": 1, "replace": 2}

def arxiv_cat_of(sub):
    """The category a subscription stands for: cat:math.PR → math.PR, cat:math.* → math (the whole archive)."""
    return re.sub(r"^cat:|\.\*$", "", sub["id"])

def arxiv_listing_ts(date):
    """Midnight, arXiv time, of an announcement day: the moment the feed dates its listing."""
    y, m, d = (int(x) for x in date.split("-"))
    return datetime(y, m, d, tzinfo=ARXIV_TZ).timestamp()

def announce_day_for(ts):
    """The listing a submission made at `ts` lands in: received before 14:00 arXiv time on a weekday it is announced
    that evening and listed under the next weekday; later, or on a weekend, it waits for the next cut. Holidays are
    ignored, so this is an estimate, used for papers that reach us through the API (authors, keyword watches)."""
    t = datetime.fromtimestamp(ts, ARXIV_TZ)
    day = t.date() + timedelta(days=1 if t.hour >= 14 else 0)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.isoformat()

def _text(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())

def arxiv_item(aid, title, authors, desc, cats, date, atype, comment=""):
    base = re.sub(r"v\d+$", "", aid)
    return {"kind": "arxiv", "id": base, "title": title, "authors": authors,
            "author": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
            "ts": arxiv_listing_ts(date), "updated": None, "desc": desc[:1500], "cats": cats, "primary": cats[0] if cats else "",
            "link": f"https://arxiv.org/abs/{base}", "pdf": f"https://arxiv.org/pdf/{base}", "ver": aid, "comment": comment[:200],
            "announce_date": date, "announce_type": atype}

def parse_arxiv_rss(data):
    """rss.arxiv.org/rss/<cat>: what arXiv announced today for a category, each entry marked new, cross or replace
    (a replaced cross-list counts as a replacement). Empty on weekends and holidays."""
    root = ET.fromstring(data)
    ch = root.find("channel")
    if ch is None:
        raise RuntimeError("not an arXiv feed")
    try:
        date = parsedate_to_datetime(ch.findtext("pubDate") or "").date().isoformat()
    except Exception:
        date = datetime.now(ARXIV_TZ).date().isoformat()
    out = []
    for it in ch.findall("item"):
        link = (it.findtext("link") or "").strip()
        if "/abs/" not in link:
            continue
        desc = it.findtext("description") or ""
        m = re.search(r"Abstract:\s*(.*)", desc, re.S)
        atype = (it.findtext("{http://arxiv.org/schemas/atom}announce_type") or "new").strip().lower()
        atype = "replace" if atype.startswith("replace") else atype if atype in ANNOUNCE_RANK else "new"
        creators = it.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
        authors = [a.strip() for a in re.split(r",\s*|\s+and\s+", creators) if a.strip()]
        cats = [c.text.strip() for c in it.findall("category") if c.text and c.text.strip()]
        out.append(arxiv_item(link.rsplit("/abs/", 1)[-1], " ".join((it.findtext("title") or "").split()), authors,
                              " ".join((m.group(1) if m else desc).split()), cats, date, atype))
    return out

def parse_arxiv_listing(page):
    """arxiv.org/list/<cat>/new (the latest listing, in sections new / cross / replacement) or /list/<cat>/pastweek
    (the last five listings, new and cross-lists only, without abstracts): dated sections of dt/dd entries."""
    kinds = {"New": "new", "Cross": "cross", "Replacement": "replace"}
    out, date, section = [], "", "new"
    for h3, body in re.findall(r"<h3>(.*?)</h3>(.*?)(?=<h3>|\Z)", page, re.S):
        m = re.search(r"(\d{1,2}) ([A-Za-z]+) (\d{4})", h3)  # "Friday, 16 October 2026" or "Fri, 16 Oct 2026"
        if m:
            try:
                date = datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}", "%d %b %Y").date().isoformat()
                section = "new"
            except ValueError:
                pass
        k = re.match(r"(New|Cross|Replacement) submissions", h3)
        if k:
            section = kinds[k.group(1)]
        if not date:
            continue
        for dt, dd in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", body, re.S):
            idm = re.search(r'href ?="/abs/([^"]+)"', dt)
            if not idm:
                continue
            field = lambda cls: _text((re.search(r"<div class=.list-%s[^>]*>(.*?)</div>" % cls, dd, re.S) or [None, ""])[1])
            title = re.sub(r"^Title:\s*", "", field("title"))
            comment = re.sub(r"^Comments:\s*", "", field("comments"))
            authors_html = re.search(r"<div class=.list-authors.>(.*?)</div>", dd, re.S)
            authors = [_text(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", authors_html.group(1), re.S)] if authors_html else []
            cats = re.findall(r"\(([a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?)\)", field("subjects"))
            abstract = re.search(r"<p class=.mathjax.>(.*?)</p>", dd, re.S)
            atype = "cross" if section == "new" and "cross-list from" in dt else section
            out.append(arxiv_item(idm.group(1), title, authors, _text(abstract.group(1)) if abstract else "", cats, date, atype, comment))
    return out

def arxiv_listing_page(cat, which):
    page = fetch(f"https://arxiv.org/list/{cat}/{which}?skip=0&show=2000", timeout=40).decode("utf-8", "replace")
    items = parse_arxiv_listing(page)
    if not items and "Showing new listings" not in page and "(showing" not in page:
        raise RuntimeError("the listing page did not look like one")
    return items

def arxiv_fill_abstracts(items):
    """Abstracts and comments for items the pastweek page listed without them, by id list from the API."""
    want = [it for it in items if not it["desc"]]
    for k in range(0, len(want), 200):
        batch = want[k:k + 200]
        got = {p["id"]: p for p in arxiv_api(id_list=",".join(it["id"] for it in batch), max_results=len(batch))}
        for it in batch:
            p = got.get(it["id"])
            if p:
                it.update({"desc": p["desc"], "comment": p.get("comment") or it["comment"], "authors": p["authors"] or it["authors"],
                           "author": p["author"] or it["author"]})

def fetch_arxiv_listing(sub):
    """A category subscription reads the official daily announcement: rss.arxiv.org/rss/<cat>, and when that is
    empty (weekends, holidays) or fails, the arxiv.org/list/<cat>/new page, which shows the last listing. The first
    time a category is read this way, the previous week's listings are taken from /list/<cat>/pastweek so the
    papers view has more than one day to show. A paper you already saw as new and that arXiv re-announces as a
    replacement keeps its place and date; the replacement is noted on it instead."""
    cat = arxiv_cat_of(sub)
    items, err = [], None
    try:
        items = parse_arxiv_rss(fetch(f"https://rss.arxiv.org/rss/{cat}", timeout=40, headers=FEED_ACCEPT))
    except Exception as ex:
        err = ex
    if not items:
        try:
            items = arxiv_listing_page(cat, "new")
        except Exception as ex:
            raise RuntimeError(f"the daily feed {'failed (' + str(err) + ')' if err else 'is empty'} and the listing page could not be read: {ex}") from ex
    with LOCK:
        known = {it["id"]: it for it in STATE["items"] if it["kind"] == "arxiv" and it.get("cid") == sub["id"]}
    if not any(it.get("announce_date") for it in known.values()):
        try:
            seen = {it["id"] for it in items}
            week = [it for it in arxiv_listing_page(cat, "pastweek") if it["id"] not in seen]
            arxiv_fill_abstracts(week)
            items += week
        except Exception as ex:
            log(f"arXiv {cat}: could not read last week's listings: {ex}")
    out = {}
    for it in items:
        it["cid"] = sub["id"]
        cur = out.get(it["id"]) or known.get(it["id"])
        if cur and cur.get("announce_type") in ANNOUNCE_RANK and ANNOUNCE_RANK[cur["announce_type"]] <= ANNOUNCE_RANK[it["announce_type"]]:
            keep = dict(cur, **{k: it[k] for k in ("title", "authors", "author", "desc", "cats", "primary", "comment", "ver") if it.get(k)})
            if it["announce_type"] == "replace" and cur["announce_type"] != "replace":
                keep["replaced"] = it["announce_date"]
            out[it["id"]] = keep
        else:
            out[it["id"]] = it
    return list(out.values())

def fetch_arxiv(sub):
    if sub["id"].startswith("cat:"):
        return fetch_arxiv_listing(sub)
    items = arxiv_api(sub["id"], max_results=int(CONFIG["arxiv"].get("max_results", 50)))
    for it in items:
        it["cid"] = sub["id"]
        it["announce_date"] = announce_day_for(it["ts"])  # so "today's listing" can include what an author or keyword watch brought
    return items

# ---- INSPIRE-HEP: citation counts, journals, who cites a paper and what it cites. Covers high-energy physics
# and its neighbours; a paper outside that is simply unknown to it, and the by-line stays as it is.

INSPIRE_FIELDS = ("titles.title,authors.full_name,arxiv_eprints.value,citation_count,publication_info.journal_title,"
                  "publication_info.journal_volume,publication_info.year,earliest_date,control_number")
INSPIRE_TTL = 86400

def inspire_query(q, sort="mostrecent", size=25):
    url = "https://inspirehep.net/api/literature?" + urlencode({"q": q, "sort": sort, "size": size, "fields": INSPIRE_FIELDS})
    hits = json.loads(fetch(url, timeout=30, headers={"Accept": "application/json"}))["hits"]
    return hits["total"], [h["metadata"] for h in hits["hits"]]

def inspire_paper(m):
    ep = (m.get("arxiv_eprints") or [{}])[0].get("value")
    authors = []
    for a in m.get("authors") or []:
        last, _, first = (a.get("full_name") or "").partition(", ")
        authors.append(f"{first} {last}".strip())
    pub = next((p for p in m.get("publication_info") or [] if p.get("journal_title")), {})
    journal = " ".join(str(pub[k]) for k in ("journal_title", "journal_volume") if pub.get(k))
    year = pub.get("year") or str(m.get("earliest_date") or "")[:4]
    recid = m.get("control_number")
    return {"id": ep, "recid": recid, "title": " ".join(((m.get("titles") or [{}])[0].get("title") or "").split()),
            "authors": authors, "year": int(year) if str(year).isdigit() else None, "journal": journal,
            "cites": int(m.get("citation_count") or 0), "link": f"https://inspirehep.net/literature/{recid}"}

def inspire_info(ids):
    """What INSPIRE knows about these arXiv ids: citation count, journal, year, record number. Cached a day; an id
    INSPIRE has not seen is remembered as such for half a day, so a listing of papers outside its field costs one
    lookup, not one per visit. Several ids go in one query."""
    now = time.time()
    out, ask = {}, []
    with LOCK:
        for i in ids:
            c = CACHE["inspire"].get(i)
            if c and now - c.get("at", 0) < (INSPIRE_TTL if c.get("found") else INSPIRE_TTL / 2):
                out[i] = c
            else:
                ask.append(i)
    for k in range(0, len(ask), 20):
        batch = ask[k:k + 20]
        found = {}
        for m in inspire_query(" or ".join(f"arxiv:{i}" for i in batch), size=len(batch) + 5)[1]:
            p = inspire_paper(m)
            if p["id"] in batch:
                found[p["id"]] = p
        for i in batch:
            p = found.get(i)
            rec = {"at": now, "found": bool(p), **({"cites": p["cites"], "journal": p["journal"], "year": p["year"], "recid": p["recid"]} if p else {})}
            cache_put("inspire", i, rec)
            out[i] = rec
    if ask:
        save_cache()
    return {i: {k: v for k, v in r.items() if k != "at"} for i, r in out.items()}

def inspire_related(aid, rel):
    """The papers citing this one (rel 'citedby': INSPIRE's refersto) or the ones it cites (its references, INSPIRE's
    citedby), newest first, with title, authors, year, journal and citation count. Both go by the record number the
    lookup gave us: refersto:arxiv:<id> misreads old-style ids with a slash in them."""
    info = inspire_info([aid]).get(aid) or {}
    if not info.get("found"):
        raise RuntimeError("INSPIRE does not have this paper: it covers high-energy physics and nearby fields, and takes a few days to notice a new one")
    q = f"{'refersto' if rel == 'citedby' else 'citedby'}:recid:{info['recid']}"
    total, metas = inspire_query(q, "mostrecent", 100)
    return {"id": aid, "rel": rel, "total": total, "papers": [inspire_paper(m) for m in metas]}

# ---------------------------------------------------------------- refresh loop

WORKERS = 16        # sources fetched at once; all of it is waiting on the network
FLUSH_EVERY = 1.5   # seconds between merges of finished sources into the feed, so it fills as sources answer
QUIET = ((30 * 86400, 6 * 3600), (7 * 86400, 2 * 3600))  # (newest video older than, refetch at most every) for YouTube channels
KIND_NAMES = {"yt": "YouTube", "tw": "X", "rss": "feeds", "arxiv": "arXiv"}

def newest_by_source():
    """(kind, ref) → timestamp of the newest item the table holds for that source."""
    with LOCK:
        rows = DB.execute("SELECT s.kind, s.ref, MAX(i.ts) AS ts FROM items i JOIN sources s ON s.id = i.source_id GROUP BY i.source_id").fetchall()
    return {(r["kind"], r["ref"]): r["ts"] for r in rows}

def due(kind, sub, now):
    """Adaptive polling, YouTube only: a channel whose newest video is older than 30 days is refetched at most every
    6 hours, older than 7 days every 2 hours, otherwise every refresh. A channel never fetched, in error, or with no
    known items is always due. X, feeds and arXiv are fetched every refresh."""
    if kind != "yt" or not sub.get("last_fetched") or sub.get("error") or not sub.get("newest"):
        return True
    age = now - sub["newest"]
    for older_than, every in QUIET:
        if age > older_than:
            return now - sub["last_fetched"] >= every
    return True

def merge_items(items):
    """Fold freshly fetched items into the feed without waiting for the slowest source: upsert them in the items
    table and in STATE["items"] (newest first). What the table already holds for those sources stays, as it would
    after a full reload; prune_items() at the end of the refresh is what drops old items."""
    items = [it for it in items if it.get("id")]
    if not items:
        return
    with tx() as db:
        if write_items(db, items) < len(items):  # a source resolved or added during this refresh has no row yet
            write_config(db)
            write_items(db, items)
    key = lambda it: (it["kind"], it.get("cid"), it["id"])
    with LOCK:
        fresh = {key(it): it for it in items}
        merged = [it for it in STATE["items"] if key(it) not in fresh] + list(fresh.values())
        merged.sort(key=lambda it: -(it.get("ts") or 0))
        per = Counter()
        kept = []
        for it in merged:  # the same per-kind cap as load_items(), so what was just merged is not cut by another kind's bulk
            per[it["kind"]] += 1
            if per[it["kind"]] <= ITEM_CAP.get(it["kind"], 4000):
                kept.append(it)
        STATE["items"] = kept

def refresh(force=False, kinds=None):
    """Fetch every source due and merge what comes back into the feed as each one finishes. `force` (the refresh
    button) also fetches the quiet YouTube channels adaptive polling would skip this time, and retries a browser
    sync that is waiting out its hour. `kinds` limits the round to those kinds (the refresh button on one tab)."""
    with LOCK:
        if STATE["refreshing"]:
            STATE["rerun"] = True  # something changed mid-refresh (e.g. an import): go again when done
            STATE["rerun_force"] = STATE.get("rerun_force", False) or force
            prev = STATE.get("rerun_kinds", "unset")  # a round for everything absorbs a round for one kind, not the other way
            STATE["rerun_kinds"] = None if kinds is None or prev is None else sorted(set(kinds) | set(prev if prev != "unset" else []))
            return
        STATE["refreshing"] = True
    try:
        t0 = time.time()
        log("refreshing…" + (f" ({', '.join(KIND_NAMES[k] for k in kinds)}, on request)" if kinds else " (everything, on request)" if force else ""))
        if not kinds or "yt" in kinds:
            sync_accounts(force)
        threading.Thread(target=zulip_fetch_stream, daemon=True).start()
        threading.Thread(target=zulip_fetch_dms, daemon=True).start()
        threading.Thread(target=ig_fetch_dms, daemon=True).start()
        errors, counts, done_at = [], Counter(), {}
        with LOCK:
            newest = newest_by_source()
            for k, s in all_subs():
                if k == "yt" and s.get("id"):
                    s["newest"] = max(s.get("newest") or 0, newest.get((k, s["id"])) or 0) or None
            subs = [(k, s) for k, s in all_subs() if (not s.get("muted") or k == "yt") and (not kinds or k in kinds)]
            jobs = [(k, s) for k, s in subs if force or due(k, s, t0)]
            # X, feeds and arXiv first (a handful of sources, each a chip of its own); within a kind the sources that
            # posted most recently go first, so what is new is on the page early and a stopped round loses the quiet ones
            jobs.sort(key=lambda ks: (ks[0] == "yt", -(ks[1].get("newest") or 0)))
        skipped = len(subs) - len(jobs)

        def do(kind, sub):
            fn = {"yt": fetch_channel, "tw": tw_fetch_user, "rss": fetch_rss, "arxiv": fetch_arxiv}[kind]
            label = {"yt": "YouTube", "tw": "X", "rss": "Feed", "arxiv": "arXiv"}[kind]
            try:
                r = fn(sub)
                sub.pop("error", None)
                sub["last_fetched"] = time.time()
                if r:
                    sub["newest"] = max(sub.get("newest") or 0, max(it.get("ts") or 0 for it in r)) or None
            except Exception as ex:
                sub["error"] = str(ex)
                errors.append(f"{label} {sub.get('name') or sub.get('id') or sub.get('ref')}: {ex}")
                r = []
            done_at[kind] = max(done_at.get(kind, 0), time.time() - t0)
            return r

        pending, flushed_at = [], time.time()

        def flush():
            nonlocal pending, flushed_at
            batch, pending, flushed_at = pending, [], time.time()
            merge_items([it for l in batch for it in l])
            with LOCK:
                STATE["errors"] = list(errors)

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(do, k, s): k for k, s in jobs}
            for f in as_completed(futures):
                k, l = futures[f], f.result()
                counts[k] += len(l)
                pending.append(l)
                if time.time() - flushed_at >= FLUSH_EVERY:
                    flush()
        flush()
        save_config()  # resolved ids, names, errors, last_fetched, newest
        save_cache()
        with tx() as db:  # everything fetched is already in the items table; the feed itself is read back from there
            prune_items(db)
        with LOCK:
            STATE["items"], STATE["errors"] = load_items(), errors
            STATE["last_refresh"] = time.time()
        per_kind = ", ".join(f"{KIND_NAMES[k]} {done_at[k]:.0f} s" for k in KINDS if k in done_at)
        log(f"done in {time.time() - t0:.0f} s: {len(jobs)} sources fetched, {skipped} quiet channels skipped ({per_kind}); "
            f"{counts['yt']} videos, {counts['tw']} tweets, {counts['rss']} posts, {counts['arxiv']} papers, {len(errors)} errors")
        for e in errors:
            log("  !", e)
    finally:
        with LOCK:
            STATE["refreshing"] = False
            rerun = STATE.pop("rerun", False)
            rerun_force = STATE.pop("rerun_force", False)
            rerun_kinds = STATE.pop("rerun_kinds", None)
    if rerun:
        refresh(rerun_force, rerun_kinds)

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

def group_threads(items):
    """An author's own reply chain is one item, not one per post: the first post carries the rest as `thread`
    (oldest first) and takes the newest post's time, so a thread that went on today shows up today. Only chains
    whose first post is in the feed are grouped; a continuation of an older post stays a post with its context."""
    def sid(link):
        m = re.search(r"/status(?:es)?/(\d+)", link or "")
        return m.group(1) if m else None
    tw = [it for it in items if it["kind"] == "tw"]
    by_sid = {sid(it["id"]): it for it in tw if sid(it["id"])}
    parent_of = {}
    for it in tw:
        s = sid(it["id"])
        if s and it.get("reply_to") and it.get("reply_to_user") == it.get("cid") and it["reply_to"] in by_sid and it["reply_to"] != s:
            parent_of[s] = it["reply_to"]
    if not parent_of:
        return items
    def root(s):
        hops = set()
        while s in parent_of and s not in hops:
            hops.add(s)
            s = parent_of[s]
        return s
    kids_of = {}
    for s in parent_of:
        kids_of.setdefault(root(s), []).append(by_sid[s])
    drop = {id(k) for kids in kids_of.values() for k in kids}
    out = []
    for it in items:
        if id(it) in drop:
            continue
        s = sid(it["id"]) if it["kind"] == "tw" else None
        if s in kids_of:
            kids = sorted(kids_of[s], key=lambda x: int(sid(x["id"])))
            it = {**it, "thread": [{k: x.get(k) for k in ("id", "link", "title", "ts", "video", "mp4", "thumb", "dur", "photos", "links") if x.get(k) is not None} for x in kids],
                  "thread_ids": [x["id"] for x in kids], "thread_started": it["ts"], "ts": max([it["ts"]] + [x["ts"] for x in kids])}
        out.append(it)
    out.sort(key=lambda x: -(x.get("ts") or 0))
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
        out, papers, seen_links = [], {}, set()
        for it in STATE["items"]:
            sub = subs.get((it["kind"], it["cid"]))
            if sub is None or sub.get("muted"):
                continue
            if it["kind"] == "rss":  # the same post through two feeds (a newsletter's substack.com and its own domain) is one post
                lk = canon_key(it.get("link") or it["id"]) or it["id"]
                if lk in seen_links:
                    continue
                seen_links.add(lk)
            if it["kind"] == "yt":
                if yc.get("hide_shorts", True) and it.get("short"):  # the fight is against scrolling: Shorts are hidden from the feed, not banned
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
                    if ANNOUNCE_RANK.get(it.get("announce_type"), 3) < ANNOUNCE_RANK.get(p.get("announce_type"), 3):  # new in one category, a cross-list in another: it is new to you
                        p.update({k: it[k] for k in ("announce_type", "announce_date", "ts", "replaced") if k in it})
                    continue
                papers[it["id"]] = {**it, "tags": list(sub.get("tags", [])), "via": [sub.get("name") or sub["id"]]}
                out.append(papers[it["id"]])
                continue
            if cap and per[(it["kind"], it["cid"])] >= cap:
                continue
            per[(it["kind"], it["cid"])] += 1
            out.append({**it, "tags": sub.get("tags", []), "author": sub.get("name") or it["author"]})
        out = group_threads(out)
        # the cap is per kind: three hundred X accounts must not push the day's videos out of the feed
        cap = int(CONFIG.get("max_items", 400))
        caps = {"yt": cap, "tw": cap, "rss": cap, "ig": cap, "arxiv": max(cap, 2000)}
        seen_n = Counter()
        keep = set()
        for it in out:
            if seen_n[it["kind"]] < caps.get(it["kind"], cap):
                seen_n[it["kind"]] += 1
                keep.add(id(it))
        return [it for it in out if id(it) in keep]

PAGE = 120  # items of each kind the page gets at first; "load more" asks for the next slice

def page_items(limits):
    """The feed as the page shows it: per kind, the newest `limits[kind]` items (PAGE by default) and how many
    there are in all, so the page can offer "load more" without carrying thousands of posts around."""
    out, total = [], Counter()
    for it in visible_items():
        k = it["kind"]
        total[k] += 1
        if total[k] <= int(limits.get(k) or (2000 if k == "arxiv" else PAGE)):  # papers have their own day windows: all of them, always
            out.append(it)
    return out, dict(total)

def settings_payload():
    z = CONFIG.get("zulip") or {}
    return {"hide_shorts": CONFIG["youtube"].get("hide_shorts", True), "min_minutes": CONFIG["youtube"].get("min_minutes", 0),
            "max_per_channel": CONFIG.get("max_per_channel", 0), "block_words": CONFIG["filters"].get("block_words", []),
            "zulip": {"site": z.get("site", ""), "email": z.get("email", ""), "stream": z.get("stream", "feedel"),
                      "streams": {k: zulip_stream_for(k) for k in KINDS}, "streams_set": z.get("streams") or {},
                      "all_streams": zulip_all_streams(), "extra_streams": parse_streams(z.get("extra_streams")), "shown_streams": zulip_streams(),
                      "recs_stream": zulip_recs_stream(),
                      "invite": z.get("invite", ""), "organizer": z.get("organizer", ""),
                      "api_key_set": bool(z.get("api_key"))},  # the key itself never goes back to the page
            "instagram": {k: v for k, v in ig_status().items() if k != "error"},
            "update": update_status(),
            "fallback_browser": CONFIG.get("fallback_browser", ""), "fallback_browser_auto": browser_for_the_rest() or "",
            "window_libs_missing": window_libs_missing(), "in_window": "show" in WINDOW,
            **CONFIG["ui"]}

def items_payload(limits=None):
    with LOCK:
        tags = sorted({t for _, s in all_subs() for t in s.get("tags", [])})
        items, total = page_items(limits or {})
        return {"items": items, "total": total, "page": PAGE, "errors": STATE["errors"], "last_refresh": STATE["last_refresh"],
                "refreshing": STATE["refreshing"], "seen": list(SEEN_STATE["seen"]), "saved": list(SEEN_STATE["saved"]),
                "saved_items": [v for v in SEEN_STATE["saved"].values() if isinstance(v, dict)],
                "tags": tags, "counts": {k: len(sub_list(k)) for k in KINDS},
                "arxiv_authors": [s["name"] for s in sub_list("arxiv") if s.get("qtype") == "author"],
                "arxiv_keywords": [s["name"] for s in sub_list("arxiv") if s.get("qtype") == "search"],
                "inbox": inbox_list(),
                "messages_unread": msg_unread(),
                "update": {k: v for k, v in update_status().items() if k in ("available", "ready", "mode", "remote", "local", "error")},
                "followed": {k: [s.get("id") for s in sub_list(k)] for k in KINDS},
                "discussions": discussions_payload(),
                "recs": recs_payload(),
                "zulip": zulip_status(),
                "stale": code_is_stale(),
                "settings": settings_payload()}

def discussions_payload():
    """For the page: item id → {topic, count, people, last_ts} for every item a Zulip topic is about."""
    idx = discussions_index()
    if not idx:
        return {}
    out = {}
    for it in STATE["items"] + inbox_list() + [v for v in SEEN_STATE["saved"].values() if isinstance(v, dict)]:
        t = idx.get(item_key(it) or "") or idx.get("title:" + (it.get("title") or "")[:60].lower())
        if t and it.get("id") not in out:
            out[it["id"]] = {k: t[k] for k in ("topic", "stream", "count", "new", "people", "last_ts")}
    return out

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
        for k in ("hide_thumbnails", "calm_titles", "hide_seen", "watch_clipboard", "clipboard_any", "window", "auto_update"):
            if k in patch:
                CONFIG["ui"][k] = bool(patch[k])
        if patch.get("github_token"):  # an empty field means "keep the one I already saved"
            CONFIG["github_token"] = str(patch["github_token"]).strip()
        if patch.get("clear_github_token"):
            CONFIG["github_token"] = ""
        if "fallback_browser" in patch:
            CONFIG["fallback_browser"] = str(patch["fallback_browser"] or "").strip().strip('"')
        if isinstance(patch.get("zulip"), dict):
            z = CONFIG.setdefault("zulip", {})
            for k in ("site", "email", "stream"):
                if k in patch["zulip"]:
                    z[k] = str(patch["zulip"][k]).strip().lstrip("#") if k == "stream" else str(patch["zulip"][k]).strip()
            if patch["zulip"].get("api_key"):  # an empty field means "keep the one I already saved"
                z["api_key"] = str(patch["zulip"]["api_key"]).strip()
            if patch["zulip"].get("clear_key"):
                z["api_key"] = ""
            if isinstance(patch["zulip"].get("streams"), dict):
                z["streams"] = {k: str(patch["zulip"]["streams"].get(k, "")).strip().lstrip("#") for k in KINDS}
            if "extra_streams" in patch["zulip"]:  # the share streams are always shown, so they are not stored twice
                z["extra_streams"] = [s for s in parse_streams(patch["zulip"]["extra_streams"]) if s not in zulip_all_streams()]
            if "recs_stream" in patch["zulip"]:
                z["recs_stream"] = str(patch["zulip"]["recs_stream"]).strip().lstrip("#") or "recommendations"
            for k in ("invite", "organizer"):
                if k in patch["zulip"]:
                    z[k] = str(patch["zulip"][k]).strip()
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

def add_entries(entries, trigger_refresh=True, respect_unfollowed=False):
    """Follow each parsed entry. YouTube ids with a name are added instantly (avatar filled in on refresh);
    handles/URLs/names are resolved in parallel; X numeric ids are resolved now or queued. Returns a summary.
    With respect_unfollowed (syncs and imports), sources you unfollowed earlier are left alone and listed;
    without it (you typed it yourself), following again also forgets the unfollow."""
    added, skipped, failed, pending, unfollowed = [], 0, [], [], []
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
            if respect_unfollowed and is_unfollowed(e["kind"], payload["id"]):
                unfollowed.append(payload.get("name") or payload.get("handle") or payload["id"])
                continue
            if add_sub(e["kind"], payload):
                unfollowed_remove(e["kind"], payload["id"])
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
    return {"added": added, "skipped": skipped, "failed": failed, "pending": len(pending), "unfollowed": unfollowed}

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
            if not is_unfollowed("tw", h):
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
        raise RuntimeError("client_secret.json is missing next to feedel (README → 'YouTube account sync')")
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

BROWSER_RETRY = 3600  # a browser that keeps its cookie database locked is tried again this often, not every refresh

def browser_sync_error(browser, ex):
    """(message, locked): yt-dlp's error for a Chromium browser (Chrome, Vivaldi, Brave, Edge…) that keeps its cookie
    database locked while it is open (yt-dlp issue 7271) is said plainly; anything else passes through."""
    s = str(ex)
    if "cookie database" in s.lower() or "7271" in s or isinstance(ex, PermissionError):
        return (f"{browser} keeps its cookie database locked while it is open: close {browser} during a sync, "
                f"or use the Google account sync instead"), True
    return s, False

def sync_accounts(force=False):
    """Runs at the start of every refresh. Mirrors *new* follows from the connected YouTube account, from a
    browser's YouTube session if configured, and resolves queued X ids. It only ever adds: unfollowing
    stays something you do on purpose, in the app. A browser whose cookie database was locked is left alone for an
    hour unless `force` (the refresh and sync-now buttons)."""
    try:
        chans = yt_account_subscriptions()
        if chans is not None:
            r = add_entries([{"kind": "yt", "ref": c["id"], "name": c["name"], "avatar": c["avatar"]} for c in chans], trigger_refresh=False, respect_unfollowed=True)
            SYNC["google"] = {"ok": True, "count": len(chans), "added": len(r["added"]), "at": time.time()}
            if r["added"]:
                log(f"  YouTube account sync: {len(r['added'])} new of {len(chans)}")
    except Exception as ex:
        SYNC["google"] = {"ok": False, "error": str(ex), "at": time.time()}
        log("  YouTube account sync failed:", ex)
    b = (CONFIG.get("sync") or {}).get("browser")
    if b and not force and (SYNC["browser"] or {}).get("retry_at", 0) > time.time():
        pass  # its cookie database was locked less than an hour ago: not worth a second and a log line every refresh
    elif b:
        try:
            chans = import_from_browser(b)
            r = add_entries([{"kind": "yt", "ref": c["id"], "name": c["name"], "avatar": c["avatar"]} for c in chans], trigger_refresh=False, respect_unfollowed=True)
            SYNC["browser"] = {"ok": True, "count": len(chans), "added": len(r["added"]), "at": time.time()}
        except Exception as ex:
            msg, locked = browser_sync_error(b, ex)
            SYNC["browser"] = {"ok": False, "error": msg, "at": time.time()}
            if locked:
                retry_at = time.time() + BROWSER_RETRY
                SYNC["browser"].update({"retry_at": retry_at, "error": f"{msg} (next try at {time.strftime('%H:%M', time.localtime(retry_at))}, or sync now)"})
            log(f"  {b} sync failed:", msg)
    try:
        n = resolve_pending_x()
        SYNC["x"] = {"ok": True, "resolved": n, "pending": len(CONFIG["twitter"].get("pending_ids", [])), "at": time.time()}
    except Exception as ex:
        SYNC["x"] = {"ok": False, "error": str(ex), "at": time.time()}

def sync_payload():
    return {"google": {"configured": os.path.exists(GOOGLE_SECRET_PATH), "connected": os.path.exists(GOOGLE_TOKEN_PATH), **(SYNC["google"] or {})},
            "browser": {"name": (CONFIG.get("sync") or {}).get("browser", ""), **(SYNC["browser"] or {})},
            "x": {"pending": len(CONFIG["twitter"].get("pending_ids", [])), **(SYNC["x"] or {})}}

OAUTH_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>feedel</title>
<body style="font:16px Georgia,serif;background:#f6f4ee;color:#1d1d1b;padding:40px;max-width:520px;margin:auto">
<p>%s</p><p><a href="http://localhost:%d/#following">back to feedel</a> — you can close this tab.</p>"""

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
    """A yt-dlp executable for mpv to call. The frozen feedel.exe can answer as one itself."""
    return find_exe("yt-dlp") or (sys.executable if FROZEN else None)

def play(video_id):
    return play_url(f"https://www.youtube.com/watch?v={video_id}")

def playable_url(url):
    """Only YouTube videos, X posts, Instagram reels and X's own video files may be handed to the player."""
    return url.startswith("https://") and (link_kind(url) in ("yt", "tw", "ig") or urlparse(url).netloc.endswith("video.twimg.com"))

def play_url(url):
    cmd = [a.replace("{url}", url) for a in CONFIG.get("player", [])]
    exe = find_exe(cmd[0]) if cmd else None
    if not exe:
        for alt in ("mpv", "mpvnet"):  # the configured player is missing; use any mpv flavour that is installed
            exe = find_exe(alt)
            if exe:
                cmd = [alt, "--ytdl-format=bestvideo[height<=1080]+bestaudio/best", url]
                break
    if not exe:
        return download_url(url, open_after=True)
    cmd[0] = exe
    if "instagram.com" in url and ig_cookies_ready() and os.path.basename(exe).lower().startswith("mpv"):
        cmd.insert(1, "--ytdl-raw-options=cookies=" + ig_cookie_file())  # mpv's yt-dlp needs the session for Instagram too
    ytdl = ytdlp_exe()
    if ytdl and os.path.basename(exe).lower().startswith("mpv") and not shutil.which("yt-dlp"):
        # mpv streams YouTube through yt-dlp; tell it where the executable is when it is not on PATH
        cmd.insert(1, "--script-opts=ytdl_hook-ytdl_path=" + ytdl)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"playing in {os.path.basename(exe)}"

def download(video_id, open_after=False):
    return download_url(f"https://www.youtube.com/watch?v={video_id}", open_after)

def download_url(url, open_after=False):
    """Download a YouTube video or an X post's video with yt-dlp (in-process, no console), optionally opening it after."""
    try:
        import yt_dlp
    except ImportError:
        return "yt-dlp not found: run `pip install yt-dlp` (and install mpv for streaming)"
    d = CONFIG.get("download_dir") or os.path.join(HERE, "downloads")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "%(title).80s [%(id)s].%(ext)s")

    def run():
        try:
            with yt_dlp.YoutubeDL({"format": "bestvideo[height<=1080]+bestaudio/best", "outtmpl": out, "noplaylist": True,
                                   "quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLogger(), **ytdlp_cookie_opts(url)}) as y:
                info = y.extract_info(url, download=True)
        except Exception as ex:
            log("download failed:", ex)
            return
        if info and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None
        path = None
        if info:
            rd = info.get("requested_downloads") or []
            path = rd[0].get("filepath") if rd else None
            if not path and info.get("id"):
                path = next((os.path.join(d, f) for f in os.listdir(d) if f"[{info['id']}]" in f and not f.endswith(".part")), None)
        if open_after and path and os.path.exists(path):
            if hasattr(os, "startfile"):
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
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

# ---------------------------------------------------------------- source pages (a channel, an account, a feed, a query)

def source_payload(kind, id_):
    """Everything about one source: its record, whether you follow it, and its recent items. A source you do not
    follow is fetched on the spot so you can look before following."""
    if kind not in KINDS or not id_:
        raise RuntimeError("unknown source")
    sub = find_sub(kind, id_)
    followed = sub is not None
    if followed:
        items = [it for it in STATE["items"] if it["kind"] == kind and it.get("cid") == sub["id"]]
        if not items and not sub.get("muted"):  # just followed, or never fetched: fetch on the spot rather than show an empty page
            try:
                items = {"yt": fetch_channel, "tw": tw_fetch_user, "rss": fetch_rss, "arxiv": fetch_arxiv}[kind](sub) or []
                if items:
                    merge_items(items)
            except Exception as ex:
                sub["error"] = str(ex)
    else:
        if kind == "yt":
            info = yt_channel_info(id_)
            sub = new_sub("yt", info["id"], info["name"], info["handle"], info["avatar"])
            sub = find_sub("yt", info["id"]) or sub
            followed = sub.get("added") is not None and find_sub("yt", info["id"]) is not None
            items = [it for it in STATE["items"] if it["kind"] == "yt" and it.get("cid") == sub["id"]] if followed else fetch_channel(sub)
        elif kind == "tw":
            sub = new_sub("tw", id_.lstrip("@").lower())
            items = tw_fetch_user(sub)
            if items and items[0].get("author"):
                sub["name"] = items[0]["author"]
        elif kind == "rss":
            info = rss_resolve(id_)
            sub = find_sub("rss", info["id"]) or new_sub("rss", info["id"], info["name"], site=info["site"])
            followed = find_sub("rss", info["id"]) is not None
            items = [it for it in STATE["items"] if it["kind"] == "rss" and it.get("cid") == sub["id"]] if followed else fetch_rss(sub)
        else:
            r = arxiv_ref(id_)
            sub = find_sub("arxiv", r["id"]) or new_sub("arxiv", r["id"], r["label"], qtype=r["type"])
            followed = find_sub("arxiv", r["id"]) is not None
            items = [it for it in STATE["items"] if it["kind"] == "arxiv" and it.get("cid") == sub["id"]] if followed else fetch_arxiv(sub)
    hide_shorts = CONFIG["youtube"].get("hide_shorts", True)
    items = [{**it, "tags": sub.get("tags", [])} for it in items if not (kind == "yt" and hide_shorts and it.get("short"))]
    items.sort(key=lambda x: -x["ts"])
    rec = {**{k: sub.get(k) for k in ("id", "name", "handle", "avatar", "tags", "muted", "min_minutes", "note", "added", "site", "qtype", "error")}, "kind": kind}
    return {"sub": rec, "followed": followed, "items": items[:60], "count": len(items)}

# ---------------------------------------------------------------- links from elsewhere (the inbox)

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+")

def link_kind(url):
    """Which kind of thing a link points at: 'yt', 'tw', 'arxiv', 'ig' (an Instagram reel or post), 'page', or None when it is not even a link."""
    u = urlparse(url if "://" in url else "https://" + url)
    host = re.sub(r"^(www|m|mobile|music)\.", "", u.netloc.lower())
    if host in ("youtube.com", "youtu.be", "youtube-nocookie.com"):
        return "yt"
    if host in ("x.com", "twitter.com", "xcancel.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com") or host.startswith("nitter."):
        return "tw"
    if host == "arxiv.org":
        return "arxiv"
    if host in ("instagram.com", "instagr.am") and re.match(r"/(?:reel|reels|p|tv)/[\w-]+", u.path):
        return "ig"
    return "page" if host and "." in host else None

def resolve_link(url, via=""):
    """A link from anywhere → the item the feed would have shown for it, fetched by this program.
    Also says what to follow to get more of the same ('follow': {kind, ref, name})."""
    url = url.strip().strip("<>\"'")
    if "://" not in url:
        url = "https://" + url
    url = unshorten(url)
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
            short = CACHE["shorts"].get(vid)
            if short is None:  # a link you chose to open is never refused; this only labels it
                code, _ = head_no_redirect(f"https://www.youtube.com/shorts/{vid}")
                short = (code == 200) if code else "/shorts/" in url
                if code:
                    cache_put("shorts", vid, short)
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
            it["short"] = bool(short)
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
            media = tw_syndication(sid)
            if media and media.get("text"):
                handle = media.get("user") or user.lower()
                it = {"kind": "tw", "id": canon, "title": media["text"], "author": "@" + handle, "ts": media.get("ts") or now, "desc": "", "link": canon,
                      "follow": {"kind": "tw", "ref": handle, "name": media.get("name") or "@" + handle}}
                it.update({k: media[k] for k in ("video", "mp4", "thumb", "dur", "photos", "reply_to", "reply_to_user", "convo", "parent", "quote") if k in media})
                return it
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
    if kind == "ig":  # a reel someone sent you: public ones come through yt-dlp without a login; private ones do not
        m = re.search(r"/(?:reel|reels|p|tv)/([\w-]+)", u.path)
        canon = f"https://www.instagram.com/{'p' if parts and parts[0] == 'p' else 'reel'}/{m.group(1)}/"
        info = ytdlp_info(canon)
        if not info:
            err = YTDLP_LAST.get("error", "")
            if "rate-limit" in err or "redirected to the login page" in err or "login" in err.lower():
                raise RuntimeError("Instagram is limiting anonymous access from your connection right now. It lifts after a while; to stop depending on it, "
                                   "import your browser's Instagram session under settings → Instagram" + (" (done, but Instagram still refused: sign in there again)" if ig_cookies_ready() else ""))
            if "registered users" in err or "private" in err.lower():
                raise RuntimeError("that account is private, or the post is for signed-in users only: import your browser's Instagram session under settings → Instagram")
            raise RuntimeError("Instagram did not hand over that one: " + (err.split("ERROR:")[-1].strip()[:160] if err else "a post without video, or Instagram is blocking right now"))
        handle = next((h.lstrip("@") for h in (info.get("channel"), info.get("uploader_id"), (re.search(r"^Video by (\S+)", info.get("title") or "") or [None, ""])[1])
                       if h and not str(h).lstrip("@").isdigit()), "")
        caption = (info.get("description") or "").strip()
        title = caption or (info.get("title") or "").strip() or f"a reel by @{handle}"
        return {"kind": "ig", "id": canon, "title": title[:2000], "author": "@" + handle if handle else (info.get("uploader") or "instagram"),
                "name": info.get("uploader") or "", "ts": info.get("timestamp") or now, "desc": "", "link": canon,
                "video": bool(info.get("formats") or info.get("url")), "thumb": info.get("thumbnail") or "", "dur": info.get("duration")}
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

YTDLP_LAST = {"error": ""}

def ig_cookie_file():
    return os.path.join(HERE, "cache", "instagram_cookies.txt")

def ig_cookies_ready():
    p = ig_cookie_file()
    return os.path.isfile(p) and os.path.getsize(p) > 100

def ytdlp_cookie_opts(url):
    """Instagram limits what it hands over anonymously; with your browser's session imported, reels keep working."""
    if "instagram.com" in (url or "") and ig_cookies_ready():
        return {"cookiefile": ig_cookie_file()}
    return {}

def ig_import_cookies(browser=None):
    """Copy the instagram.com cookies of a local browser (through yt-dlp's cookie reader) into feedel's own cookie
    file, once; a Chromium-based browser must be closed for its cookie database to be readable on Windows."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        raise RuntimeError("this needs yt-dlp as a Python module")
    from http.cookiejar import MozillaCookieJar
    name = (browser or "").strip().lower() or browser_cookie_name()
    if not name:
        raise RuntimeError("no browser to read from: set the fallback browser under settings → links, or pass one")
    try:
        jar = extract_cookies_from_browser(name)
    except Exception as ex:
        msg = str(ex)
        if "Could not copy" in msg or "locked" in msg.lower() or "database is locked" in msg.lower():
            raise RuntimeError(f"{name} keeps its cookie database locked while it is open: close {name} completely, press this again, then open it back")
        raise RuntimeError(f"could not read {name}'s cookies: {msg[:200]}")
    out = MozillaCookieJar(ig_cookie_file())
    n = 0
    for c in jar:
        if "instagram.com" in (c.domain or ""):
            out.set_cookie(c)
            n += 1
    if not n:
        raise RuntimeError(f"{name} holds no Instagram session: sign in to instagram.com there once, then import again")
    os.makedirs(os.path.dirname(ig_cookie_file()), exist_ok=True)
    out.save(ignore_discard=True, ignore_expires=True)
    names = {c.name for c in out}
    return {"ok": True, "cookies": n, "browser": name, "session": "sessionid" in names}

def browser_cookie_name():
    """yt-dlp's name for the browser feedel hands links to (or the system default): vivaldi, chrome, edge, brave, firefox."""
    b = (browser_for_the_rest() or "").lower()
    for key in ("vivaldi", "chrome", "chromium", "edge", "brave", "opera", "firefox"):
        if key in b:
            return "edge" if key == "edge" else key
    return ""

def ytdlp_info(url):
    """What yt-dlp knows about a page's video without downloading it (the first entry of a carousel or playlist).
    None when there is no video, yt-dlp fails, or yt-dlp is unavailable."""
    try:
        import yt_dlp
    except ImportError:
        return None
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                               "socket_timeout": 15, "retries": 1, "logger": _QuietLogger(), **ytdlp_cookie_opts(url)}) as y:
            info = y.extract_info(url, download=False)
    except Exception as ex:
        YTDLP_LAST["error"] = str(ex)
        log("  yt-dlp", url, str(ex)[:160])
        return None
    YTDLP_LAST["error"] = ""
    if info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        for k in ("description", "uploader", "uploader_id", "channel", "timestamp"):  # the carousel's own fields describe the post
            if entries and info.get(k) and not entries[0].get(k):
                entries[0][k] = info[k]
        info = entries[0] if entries else None
    if not info or not (info.get("formats") or info.get("url")):
        return None
    return info

def tw_video_info(url):
    """Does this X post carry a video? Asked through yt-dlp without downloading. None when there is none or
    yt-dlp is unavailable. Returns {video, thumb, dur} otherwise."""
    info = ytdlp_info(url)
    if not info:
        return None
    return {"video": True, "thumb": info.get("thumbnail") or "", "dur": info.get("duration")}

class _QuietLogger:
    def debug(self, *a): pass
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass

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
    """Opt-in: every link you copy that feedel can show goes to the inbox. Only the text is read, only for links,
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
            if not k or (not feedel_opens(url) and not CONFIG["ui"].get("clipboard_any")) or url in seen:
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
    Returns None too when that default is feedel itself (after --register-handler)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
            prog = winreg.QueryValueEx(k, "ProgId")[0]
        if not prog or prog.lower().startswith("feedel"):
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
        log("  links feedel cannot show will go to", b)
    return b

def browser_for_the_rest():
    """The browser `--open` hands non-feedel links to: the configured one, else the system default,
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

SHORTENERS = {"t.co", "bit.ly", "buff.ly", "tinyurl.com", "lnkd.in", "ow.ly", "goo.gl", "trib.al", "dlvr.it", "ift.tt", "is.gd", "rb.gy",
              "cutt.ly", "shorturl.at", "tiny.cc", "bbc.in", "nyti.ms", "wapo.st", "econ.st", "reut.rs", "on.ft.com", "youtu.be"}
UNSHORTENED = {}

def unshorten(url, hops=4):
    """A shortener's link (t.co, bit.ly…) → where it lands, following up to four redirects without fetching the
    page itself. t.co answers browsers with a page instead of a redirect, so a plain client name is used. Other
    links come back unchanged."""
    url = (url or "").strip()
    for _ in range(hops):
        host = re.sub(r"^www\.", "", urlparse(url if "://" in url else "https://" + url).netloc.lower())
        if host not in SHORTENERS or host == "youtu.be":
            break
        if url in UNSHORTENED:
            url = UNSHORTENED[url]
            continue
        try:
            opener = urllib.request.build_opener(NoRedirect)
            r = opener.open(urllib.request.Request(url, headers={"User-Agent": "curl/8.0"}), timeout=12)
            body = r.read(4000).decode("utf-8", "ignore")
            m = re.search("URL=([^\"'>\s]+)", body, re.I) or re.search(r'href="(https?://[^"]+)"', body)
            nxt = html.unescape(m.group(1)) if m else ""
        except urllib.error.HTTPError as e:
            nxt = e.headers.get("Location", "") if e.code in (301, 302, 303, 307, 308) else ""
        except Exception:
            nxt = ""
        if not nxt or nxt == url:
            break
        if nxt.startswith("/"):
            nxt = f"https://{host}{nxt}"
        UNSHORTENED[url] = nxt
        url = nxt
    return url

def feedel_opens(url):
    """Which links the link handler keeps for feedel: YouTube, X, arXiv, Instagram reels, Substack, and posts on
    the site of any newsletter or blog you follow. Everything else is for the normal browser. A shortener's link
    is followed first, so a t.co from X or a bit.ly to a video counts as what it leads to."""
    url = unshorten(url)
    k = link_kind(url)
    if k in ("yt", "tw", "arxiv", "ig"):
        return True
    if k != "page":
        return False
    host = re.sub(r"^(www|m)\.", "", urlparse(url if "://" in url else "https://" + url).netloc.lower())
    if host.endswith(".substack.com"):
        return True
    with LOCK:
        feeds = {re.sub(r"^(www|m)\.", "", urlparse(s.get("id") or s.get("ref") or "").netloc.lower()) for s in sub_list("rss")}
    return bool(host) and host in feeds

def open_link_cli(url):
    """`feedel --open URL`: a video, post, paper, reel or newsletter link goes to the inbox and the page opens on
    it; anything else goes to the normal browser. This is what makes feedel usable as the system's link handler."""
    url = unshorten(url)
    if feedel_opens(url):
        import webbrowser
        target = f"http://localhost:{PORT}/open?" + urlencode({"u": url})
        import socket
        with socket.socket() as probe:
            probe.settimeout(0.5)
            running = probe.connect_ex(("127.0.0.1", PORT)) == 0
        if running:
            if not show_running_window(PORT, link=url):  # running in its own window: it shows the link itself
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
    """Windows: make feedel selectable under Settings → Apps → Default apps → Web browser. After choosing it,
    every link from WhatsApp, mail or any app goes through `--open`. Nothing is changed without you picking it there."""
    if sys.platform != "win32":
        raise RuntimeError("the link handler is Windows-only for now; on macOS use the link box or the clipboard watcher")
    import winreg
    def drop(name):  # remove a registration by name: ours, or the one the program had as quietfeed
        b = r"Software\Clients\StartMenuInternet\\" + name
        for key in (b + r"\Capabilities\URLAssociations", b + r"\Capabilities", b + r"\shell\open\command", b + r"\shell\open",
                    b + r"\shell", b, r"Software\Classes\\" + name + r"URL\shell\open\command", r"Software\Classes\\" + name + r"URL\shell\open",
                    r"Software\Classes\\" + name + r"URL\shell", r"Software\Classes\\" + name + "URL"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications", 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except OSError:
            pass

    base = r"Software\Clients\StartMenuInternet\feedel"
    drop("quietfeed")  # an older registration under the old name would otherwise sit beside this one
    if not on:
        drop("feedel")
        return "feedel removed from the browser list"
    cmd = handler_command()

    def put(path, values):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
            for name, val in values.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)

    put(r"Software\Classes\feedelURL", {"": "feedel link", "URL Protocol": ""})
    put(r"Software\Classes\feedelURL\shell\open\command", {"": cmd})
    put(base, {"": "feedel"})
    put(base + r"\Capabilities", {"ApplicationName": "feedel", "ApplicationDescription": "Opens videos, posts and papers in feedel; everything else in your normal browser"})
    put(base + r"\Capabilities\URLAssociations", {"http": "feedelURL", "https": "feedelURL"})
    put(base + r"\shell\open\command", {"": cmd})
    put(r"Software\RegisteredApplications", {"feedel": base + r"\Capabilities"})
    # remember today's default browser before it becomes feedel; through the running instance if there is one,
    # so its in-memory settings do not overwrite ours on its next save
    kept = CONFIG.get("fallback_browser") or system_default_browser()
    if kept and not CONFIG.get("fallback_browser"):
        try:
            fetch(f"http://localhost:{PORT}/api/settings", timeout=3, data=json.dumps({"fallback_browser": kept}).encode(),
                  headers={"Content-Type": "application/json"})
        except Exception:
            remember_default_browser()
    return ("registered: now pick feedel under Settings → Apps → Default apps → Web browser"
            + (f"\nother links will keep opening in {kept}" if kept else "\nno default browser detected: set fallback_browser in settings"))

# ---------------------------------------------------------------- zulip (where the conversation lives)

def zulip_cfg():
    z = CONFIG.get("zulip") or {}
    site = (z.get("site") or "").strip().rstrip("/")
    if site and not site.startswith("http"):
        site = "https://" + site
    return site, (z.get("email") or "").strip(), (z.get("api_key") or "").strip(), (z.get("stream") or "feedel").strip().lstrip("#") or "feedel"

def zulip_ready():
    site, email, key, _ = zulip_cfg()
    return bool(site and email and key)

def zulip_stream_for(kind):
    """The stream a share of this kind goes to: the per-kind one if set, else the default."""
    z = CONFIG.get("zulip") or {}
    return ((z.get("streams") or {}).get(kind) or "").strip() or zulip_cfg()[3]

def zulip_recs_stream():
    return ((CONFIG.get("zulip") or {}).get("recs_stream") or "").strip() or "recommendations"

def zulip_all_streams():
    """Every stream shares can go to, plus the recommendations stream: the default, each per-kind one, without repeats."""
    out = [zulip_cfg()[3]]
    for s in [zulip_stream_for(k) for k in KINDS] + [zulip_recs_stream()]:
        if s not in out:
            out.append(s)
    return out

def parse_streams(v):
    """A streams setting as typed ('feedel, papers' or one per line) or as a list → clean names, no '#', no repeats."""
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    out = []
    for s in v or []:
        s = str(s).strip().lstrip("#").strip()
        if s and s not in out:
            out.append(s)
    return out

def zulip_streams():
    """Every stream feedel reads and the talk tab shows: the share streams (default first, then per kind),
    then the extra streams listed in settings."""
    out = zulip_all_streams()
    for s in parse_streams((CONFIG.get("zulip") or {}).get("extra_streams")):
        if s not in out:
            out.append(s)
    return out

def zulip_request(path, data=None, params=None, method=None):
    """One call to the Zulip REST API, authenticated as you (e-mail + personal API key). `data` posts (or PATCHes /
    DELETEs with `method`), `params` gets. Errors come back as Zulip phrased them."""
    site, email, key, _ = zulip_cfg()
    if not (site and email and key):
        raise RuntimeError("Zulip is not set up yet: add your site, e-mail and API key under settings")
    auth = base64.b64encode(f"{email}:{key}".encode()).decode()
    try:
        r = fetch(site + "/api/v1" + path + ("?" + urlencode(params) if params else ""), timeout=25,
                  data=urlencode(data).encode() if data else None, method=method,
                  headers={"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")
        try:
            msg = json.loads(msg).get("msg", msg)
        except Exception:
            pass
        raise RuntimeError(f"Zulip said {e.code}: {msg[:200]}")
    return json.loads(r)

def zulip_people():
    """The humans on the Zulip, minus me and bots: who a recommendation can be addressed to."""
    me = zulip_me()
    r = zulip_request("/users")
    out = [{"user_id": u.get("user_id"), "name": u.get("full_name", ""), "email": u.get("email", "")}
           for u in r.get("members", []) if u.get("is_active", True) and not u.get("is_bot") and u.get("user_id") != me.get("user_id")]
    return sorted(out, key=lambda u: u["name"].lower())


def zulip_me():
    """Who you are on Zulip (id, name), asked once per run; needed to tell your own messages and reactions apart."""
    me = STATE.get("zulip_me")
    if me and me.get("for") == zulip_cfg()[:2]:
        return me
    r = zulip_request("/users/me")
    me = {"user_id": r.get("user_id"), "name": r.get("full_name", ""), "email": (r.get("email") or "").lower(), "for": zulip_cfg()[:2]}
    STATE["zulip_me"] = me
    return me

def zulip_test():
    STATE.pop("zulip_me", None)
    me = zulip_me()
    return {"ok": True, "name": me["name"], "email": me["email"], "stream": zulip_cfg()[3], "share_streams": zulip_all_streams(), "streams": zulip_streams(),
            "realm": zulip_realm_name(), "admin": bool(me.get("admin"))}

def zulip_realm_name():
    """The organization's name, from the public server settings; '' when unreachable."""
    site = zulip_cfg()[0]
    if not site:
        return ""
    try:
        return json.loads(fetch(site + "/api/v1/server_settings", timeout=10)).get("realm_name", "")
    except Exception:
        return ""

# ---- getting friends in: a setup code the organizer sends, invitations, and joining the group's streams

def zulip_setup_code():
    """Everything a friend's feedel needs to point at this group, as one pasteable code (no keys, no e-mails
    of yours beyond the organizer contact you chose)."""
    z = CONFIG.get("zulip") or {}
    d = {"v": 1, "site": zulip_cfg()[0], "stream": zulip_cfg()[3], "streams": {k: zulip_stream_for(k) for k in KINDS},
         "extra_streams": parse_streams(z.get("extra_streams")), "recs_stream": zulip_recs_stream(),
         "invite": z.get("invite", ""), "organizer": z.get("organizer", ""), "realm": zulip_realm_name()}
    return "qf1." + base64.urlsafe_b64encode(json.dumps(d, ensure_ascii=False).encode()).decode().rstrip("=")

def zulip_apply_setup(code):
    """A setup code → this feedel's Zulip settings (site, streams, invite link, organizer); the person's own
    e-mail and API key are theirs to add."""
    code = code.strip()
    if not code.startswith("qf1."):
        raise RuntimeError("that is not a feedel setup code (it starts with qf1.)")
    raw = code[4:] + "=" * (-len(code[4:]) % 4)
    try:
        d = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
    except Exception:
        raise RuntimeError("the setup code is damaged; ask for it again")
    with LOCK:
        z = CONFIG.setdefault("zulip", {})
        z["site"] = str(d.get("site", "")).strip()
        z["stream"] = str(d.get("stream") or "feedel").strip()
        z["streams"] = {k: str((d.get("streams") or {}).get(k, "")).strip() for k in KINDS}
        z["extra_streams"] = parse_streams(d.get("extra_streams") or [])
        z["recs_stream"] = str(d.get("recs_stream") or "recommendations").strip()
        z["invite"] = str(d.get("invite", "")).strip()
        z["organizer"] = str(d.get("organizer", "")).strip()
        save_config()
    return {"ok": True, "site": z["site"], "realm": d.get("realm", ""), "invite": z["invite"], "organizer": z["organizer"],
            "streams": zulip_streams()}

def zulip_stream_ids(names):
    r = zulip_request("/streams", params={"include_subscribed": "true", "include_public": "true"})
    by = {s.get("name"): s.get("stream_id") for s in r.get("streams", [])}
    return [by[n] for n in names if n in by], [n for n in names if n not in by]

def zulip_invite_link():
    """A reusable invitation link that lands new members in the group's streams. Needs invitation rights on the
    Zulip; the link is kept in settings so the setup code can carry it."""
    ids, missing = zulip_stream_ids(zulip_streams())
    r = zulip_request("/invites/multiuse", {"invite_expires_in_minutes": json.dumps(None), "invite_as": 400,
                                           "stream_ids": json.dumps(ids), "include_realm_default_subscriptions": "true"})
    link = r.get("invite_link", "")
    if link:
        with LOCK:
            CONFIG.setdefault("zulip", {})["invite"] = link
            save_config()
    return {"ok": True, "invite": link, "missing_streams": missing}

def zulip_invite_emails(emails):
    """Have Zulip e-mail invitations to these addresses, subscribing them to the group's streams on arrival."""
    emails = [e.strip() for e in re.split(r"[\s,;]+", emails or "") if "@" in e]
    if not emails:
        raise RuntimeError("no e-mail addresses given")
    ids, missing = zulip_stream_ids(zulip_streams())
    zulip_request("/invites", {"invitee_emails": ",".join(emails), "stream_ids": json.dumps(ids), "invite_as": 400,
                               "invite_expires_in_minutes": json.dumps(None), "include_realm_default_subscriptions": "true"})
    return {"ok": True, "sent": emails, "missing_streams": missing}

def zulip_subscribe_all():
    """Subscribe me to every stream the group uses (creating the ones that do not exist yet, when allowed)."""
    names = zulip_streams()
    r = zulip_request("/users/me/subscriptions", {"subscriptions": json.dumps([{"name": n} for n in names])})
    return {"ok": True, "subscribed": list((r.get("subscribed") or {}).values())[0] if r.get("subscribed") else [],
            "already": list((r.get("already_subscribed") or {}).values())[0] if r.get("already_subscribed") else [],
            "unauthorized": r.get("unauthorized") or [], "streams": names}

def item_link(it):
    k = it.get("kind")
    if k == "yt":
        return f"https://www.youtube.com/watch?v={it.get('id')}"
    return it.get("link") or it.get("id") or ""

def canon_key(url):
    """What makes two links 'the same thing': video id, paper id, post id, or the link without its query noise."""
    k = link_kind(url or "")
    if k == "yt":
        m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", url)
        return "yt:" + m.group(1) if m else None
    if k == "arxiv":
        m = re.search(r"(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})", url)
        return "arxiv:" + m.group(1) if m else None
    if k == "tw":
        m = re.search(r"/status(?:es)?/(\d+)", url)
        return "tw:" + m.group(1) if m else None
    if k == "ig":
        m = re.search(r"/(?:reel|reels|p|tv)/([\w-]+)", url)
        return "ig:" + m.group(1) if m else None
    if k == "page":
        return "url:" + re.sub(r"[?#].*$", "", url).rstrip("/").lower()
    return None

def item_key(it):
    if it.get("kind") == "arxiv":
        return "arxiv:" + str(it.get("id"))
    return canon_key(item_link(it))

def zulip_clean(content, site):
    """Zulip's rendered HTML, made safe for the page: no scripts, absolute links back to the site, images as links,
    videos as a small player (uploads on the Zulip itself go through /zmedia, which carries your credentials),
    emoji as the characters they stand for. Safe to apply twice."""
    content = re.sub(r"<(script|style|iframe|object|embed)[^>]*>.*?</\1>", "", content or "", flags=re.S | re.I)
    content = re.sub(r"<(iframe|object|embed)[^>]*/?>", "", content, flags=re.I)
    content = re.sub(r'(href|src)="/(?!zmedia\?)', r'\1="' + site + "/", content)
    def video(m):
        src = html.unescape(m.group(1))
        if src.startswith("/zmedia?"):
            return m.group(0)
        if site and src.startswith(site + "/"):
            src = "/zmedia?" + urlencode({"u": src})
        return f'<video controls preload="metadata" src="{html.escape(src)}"></video>'
    content = re.sub(r'<video[^>]+src="([^"]+)"[^>]*>(?:\s*</video>)?', video, content, flags=re.I)
    content = re.sub(r'<a[^>]*>\s*(<video[^>]*></video>)\s*</a>', r'\1', content, flags=re.I | re.S)  # the link around a player would take the clicks
    content = re.sub(r'<img[^>]+src="([^"]+)"[^>]*>', r'<a href="\1" target="_blank" rel="noopener">[image]</a>', content)
    def emoji(m):
        try:
            return "".join(chr(int(h, 16)) for h in m.group(1).split("-"))
        except ValueError:
            return m.group(2)
    content = re.sub(r'<span class="emoji emoji-([0-9a-f-]+)"[^>]*>(.*?)</span>', emoji, content, flags=re.I)
    return content

def zulip_message(m, site, me, stream=None):
    """One Zulip message as the page sees it: who, when, rendered content, the item links in it, whether you have
    read it, the reactions on it (yours marked), and whether it is yours (so it can be edited)."""
    content = zulip_clean(m.get("content", ""), site)
    links = [html.unescape(h) for h in re.findall(r'href="([^"]+)"', content)]
    reactions = {}
    for r in m.get("reactions") or []:
        key = (r.get("reaction_type") or "unicode_emoji", r.get("emoji_code") or r.get("emoji_name"))
        uid = r.get("user_id") if r.get("user_id") is not None else (r.get("user") or {}).get("id")
        uemail = ((r.get("user") or {}).get("email") or "").lower()
        rec = reactions.setdefault(key, {"name": r.get("emoji_name", ""), "code": r.get("emoji_code", ""), "type": key[0], "count": 0, "mine": False})
        rec["count"] += 1
        if (uid is not None and uid == me.get("user_id")) or (uemail and uemail == me.get("email")):
            rec["mine"] = True
    sender = (m.get("sender_email") or "").lower()
    return {"id": m.get("id"), "who": m.get("sender_full_name", ""), "email": sender, "ts": m.get("timestamp"),
            "topic": m.get("subject", ""), "stream": stream or m.get("display_recipient") or "", "content": content,
            "keys": sorted({k for k in (canon_key(l) for l in links) if k}), "text": strip_html(content, 200),
            "links": [l for l in links if canon_key(l)],
            "mentions": re.findall(r'<span class="user-mention[^"]*"[^>]*data-user-id="([^"]+)"', content),
            "read": "read" in (m.get("flags") or []), "mine": bool(sender) and sender == me.get("email") or m.get("sender_id") == me.get("user_id"),
            "edited": bool(m.get("last_edit_timestamp")), "reactions": sorted(reactions.values(), key=lambda r: -r["count"])}

def zulip_fetch_stream(stream=None):
    """A stream's recent messages (rendered by Zulip, so LaTeX arrives as KaTeX markup), kept in STATE["zulip"]["streams"]
    keyed by stream. Without an argument every listed stream is fetched. Returns the state of the stream(s) fetched."""
    if not zulip_ready():
        STATE["zulip"] = None
        return None
    streams = [stream] if stream else zulip_streams()
    site = zulip_cfg()[0]
    with LOCK:
        z = STATE.get("zulip") or {"streams": {}}
        z["site"] = site
        for s in list(z["streams"]):  # a stream taken out of the settings leaves the state too
            if s not in zulip_streams():
                del z["streams"][s]
        STATE["zulip"] = z
    for s in streams:
        try:
            me = zulip_me()
            r = zulip_request("/messages", params={"anchor": "newest", "num_before": 400, "num_after": 0, "apply_markdown": "true",
                                                  "narrow": json.dumps([{"operator": "stream", "operand": s}])})
            msgs = [zulip_message(m, site, me, s) for m in r.get("messages", [])]
            with LOCK:
                STATE["zulip"]["streams"][s] = {"at": time.time(), "messages": msgs, "error": None}
        except Exception as ex:
            with LOCK:
                prev = STATE["zulip"]["streams"].get(s) or {}
                STATE["zulip"]["streams"][s] = {**prev, "error": str(ex), "at": prev.get("at") or time.time(), "messages": prev.get("messages", [])}
            log(f"  zulip #{s}:", ex)
    threading.Thread(target=resolve_rec_items, daemon=True).start()
    return STATE["zulip"]["streams"].get(stream) if stream else STATE["zulip"]

def zulip_state(stream):
    return ((STATE.get("zulip") or {}).get("streams") or {}).get(stream) or {}

def zulip_messages(stream=None):
    """The cached messages of one stream, or of every stream when none is given."""
    z = (STATE.get("zulip") or {}).get("streams") or {}
    if stream is not None:
        return list((z.get(stream) or {}).get("messages", []))
    return [m for s in zulip_streams() for m in (z.get(s) or {}).get("messages", [])]

def zulip_status():
    """For the page: is Zulip on, which streams, when each was read and whether that failed."""
    z = STATE.get("zulip") or {}
    streams = [{"name": s, "share": s == zulip_cfg()[3], "at": zulip_state(s).get("at"), "error": zulip_state(s).get("error")} for s in zulip_streams()]
    err = next((s["error"] for s in streams if s["error"]), None)
    return {"on": zulip_ready(), "stream": zulip_cfg()[3], "site": zulip_cfg()[0], "streams": streams,
            "at": max([s["at"] for s in streams if s["at"]] or [None]) if any(s["at"] for s in streams) else None, "error": err}

def zulip_topics(stream=None):
    """One summary per topic, newest activity first, with the keys of the items it is about and how many of its
    messages you have not read (shown as muted text on the row, never as a badge)."""
    topics = {}
    for m in zulip_messages(stream):
        t = topics.setdefault((m["stream"], m["topic"]), {"topic": m["topic"], "stream": m["stream"], "count": 0, "new": 0, "first_ts": m["ts"],
                                                           "last_ts": m["ts"], "people": [], "keys": [], "last": "", "last_who": ""})
        t["count"] += 1
        if not m["read"]:
            t["new"] += 1
        t["first_ts"] = min(t["first_ts"], m["ts"])
        if m["ts"] >= t["last_ts"]:
            t["last_ts"], t["last"], t["last_who"] = m["ts"], m["text"], m["who"]
        if m["who"] not in t["people"]:
            t["people"].append(m["who"])
        for k in m["keys"]:
            if k not in t["keys"]:
                t["keys"].append(k)
    return sorted(topics.values(), key=lambda t: -t["last_ts"])

def discussions_index():
    """item key → the topic discussing it (the first one that linked it, in any shown stream, plus a title match as a fallback)."""
    out = {}
    for t in zulip_topics():
        for k in t["keys"]:
            out.setdefault(k, t)
        out.setdefault("title:" + t["topic"].lower(), t)
    return out

def zulip_topic_messages(topic, stream=None):
    """The messages of one topic, oldest first. Without a stream, the first shown stream that has the topic."""
    if not stream:
        stream = next((s for s in zulip_streams() if any(m["topic"] == topic for m in zulip_messages(s))), zulip_cfg()[3])
    return sorted([m for m in zulip_messages(stream) if m["topic"] == topic], key=lambda m: m["ts"])

def zulip_search(stream, query):
    """Ask Zulip's own search within one stream; matching messages grouped by topic, newest topic first, with
    Zulip's match highlighting kept (its `highlight` spans)."""
    query = (query or "").strip()
    if not query:
        return {"topics": [], "query": query, "stream": stream}
    site = zulip_cfg()[0]
    me = zulip_me()
    r = zulip_request("/messages", params={"anchor": "newest", "num_before": 200, "num_after": 0, "apply_markdown": "true",
                                          "narrow": json.dumps([{"operator": "stream", "operand": stream}, {"operator": "search", "operand": query}])})
    topics = {}
    for raw in r.get("messages", []):
        m = zulip_message({**raw, "content": raw.get("match_content") or raw.get("content", "")}, site, me, stream)
        m["topic_html"] = zulip_clean(raw.get("match_subject") or html.escape(raw.get("subject", "")), site)
        t = topics.setdefault(m["topic"], {"topic": m["topic"], "stream": stream, "topic_html": m["topic_html"], "last_ts": m["ts"], "messages": []})
        t["last_ts"] = max(t["last_ts"], m["ts"])
        t["messages"].append(m)
    for t in topics.values():
        t["messages"].sort(key=lambda m: m["ts"])
    return {"topics": sorted(topics.values(), key=lambda t: -t["last_ts"]), "query": query, "stream": stream}

def zulip_post(stream, topic, content):
    """Post to a topic in a stream, as you, then re-read that stream so the page sees the message rendered."""
    if not content.strip():
        raise RuntimeError("nothing to post")
    topic = (topic or "").strip()[:60]
    if not topic:
        raise RuntimeError("a topic is needed")
    stream = (stream or "").strip().lstrip("#") or zulip_cfg()[3]
    r = zulip_request("/messages", {"type": "stream", "to": stream, "topic": topic, "content": content.strip()})
    zulip_fetch_stream(stream)  # synchronous, so the page can show the thread with the new message right away
    return {"ok": True, "id": r.get("id"), "stream": stream, "topic": topic}

def zulip_reply(topic, content, stream=None):
    return zulip_post(stream, topic, content)

def zulip_mark_read(ids):
    """Tell Zulip you have read these messages (its own read flag, the one its apps use), and remember it locally."""
    ids = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return {"ok": True, "n": 0}
    zulip_request("/messages/flags", {"messages": json.dumps(ids), "op": "add", "flag": "read"})
    with LOCK:
        for m in zulip_messages():
            if m["id"] in ids:
                m["read"] = True
    return {"ok": True, "n": len(ids)}

def zulip_react(mid, emoji_name, on=True, emoji_code="", reaction_type=""):
    """Add or take back one reaction of yours on a message; the local copy is patched so the page can redraw at once."""
    mid = int(mid)
    emoji_name = (emoji_name or "").strip().strip(":")
    if not emoji_name:
        raise RuntimeError("which emoji?")
    data = {"emoji_name": emoji_name}
    if emoji_code:
        data["emoji_code"] = emoji_code
    if reaction_type:
        data["reaction_type"] = reaction_type
    zulip_request(f"/messages/{mid}/reactions", data, method="POST" if on else "DELETE")
    me = zulip_me()
    with LOCK:
        for m in zulip_messages():
            if m["id"] != mid:
                continue
            rec = next((r for r in m["reactions"] if r["name"] == emoji_name), None)
            if on and not rec:
                m["reactions"].append({"name": emoji_name, "code": emoji_code, "type": reaction_type or "unicode_emoji", "count": 1, "mine": True})
            elif on and not rec["mine"]:
                rec["count"] += 1
                rec["mine"] = True
            elif not on and rec and rec["mine"]:
                rec["count"] -= 1
                rec["mine"] = False
                if rec["count"] <= 0:
                    m["reactions"].remove(rec)
            return {"ok": True, "message": m}
    return {"ok": True, "me": me["name"]}

def zulip_raw(mid):
    """The markdown source of one message, for editing."""
    r = zulip_request(f"/messages/{int(mid)}", params={"apply_markdown": "false"})
    return {"id": int(mid), "content": (r.get("message") or {}).get("content") or r.get("raw_content") or ""}

def zulip_edit(mid, content, stream=None):
    """Change the text of one of your messages, then re-read its stream so the rendered version comes back from Zulip."""
    if not content.strip():
        raise RuntimeError("nothing to save")
    mid = int(mid)
    zulip_request(f"/messages/{mid}", {"content": content.strip()}, method="PATCH")
    stream = stream or next((m["stream"] for m in zulip_messages() if m["id"] == mid), None)
    if stream:
        zulip_fetch_stream(stream)
    return {"ok": True, "id": mid}

def recommendations():
    """What friends recommended to me: messages in the recommendations stream that mention nobody (for everyone)
    or mention me, grouped by the thing they link to. My own recommendations are not for me."""
    me = STATE.get("zulip_me") or {}
    recs_stream = zulip_recs_stream()
    out = {}
    for m in zulip_messages(recs_stream):
        if not m.get("keys") or m.get("mine"):
            continue
        mentions = m.get("mentions") or []
        if mentions and "*" not in mentions and str(me.get("user_id")) not in mentions:
            continue
        key, link = m["keys"][0], m["links"][0] if m.get("links") else ""
        qm = re.search(r"<blockquote>(.*?)</blockquote>", m["content"], re.S)
        note = strip_html(qm.group(1), 600) if qm else ""
        thanked = any(r.get("mine") and r.get("name") in ("+1", "thumbs_up") for r in m.get("reactions", []))
        rec = out.setdefault(key, {"key": key, "link": link, "topic": m["topic"], "stream": recs_stream, "by": [], "last_ts": m["ts"],
                                   "for_all": not mentions})
        rec["by"].append({"who": m["who"], "note": note, "ts": m["ts"], "msg_id": m["id"], "thanked": thanked})
        rec["last_ts"] = max(rec["last_ts"], m["ts"])
        rec["for_all"] = rec["for_all"] and not mentions
    return sorted(out.values(), key=lambda r: -r["last_ts"])

def rec_item_for(key, link):
    """The item a recommendation points at: something already in the feed, inbox or library, else a resolved link."""
    for it in STATE["items"]:
        if item_key(it) == key:
            return it
    for it in inbox_list():
        if item_key(it) == key:
            return it
    for it in SEEN_STATE["saved"].values():
        if isinstance(it, dict) and item_key(it) == key:
            return it
    cached = CACHE.setdefault("links", {}).get(key)
    return cached

def resolve_rec_items():
    """Resolve recommended links feedel has never seen, once each, in the background."""
    for r in recommendations():
        if r.get("link") and rec_item_for(r["key"], r["link"]) is None:
            try:
                it = resolve_link(r["link"], "friend")
                it["cid"] = it.get("cid") or "rec"
                cache_put("links", r["key"], it)
            except Exception as ex:
                cache_put("links", r["key"], {"kind": "rss", "id": r["link"], "title": r["link"], "author": "", "ts": r["last_ts"], "link": r["link"], "desc": "", "error": str(ex)})
    save_cache()

def recs_payload():
    """For the page: the recommendation cards (with their items) and, by item id, who recommended it."""
    cards, by_id = [], {}
    for r in recommendations():
        it = rec_item_for(r["key"], r["link"])
        if it is None:
            continue
        card = {**r, "item": {**it, "tags": it.get("tags") or []}}
        cards.append(card)
        by_id[it.get("id")] = [{"who": b["who"], "note": b["note"], "ts": b["ts"], "msg_id": b["msg_id"], "thanked": b["thanked"]} for b in r["by"]]
    return {"cards": cards, "by_id": by_id}

def zulip_share(item, note="", topic="", stream="", recommend=False, to=()):
    """Post an item and your note to the stream for its kind (videos to #vids, papers to #papers, as configured),
    or to the stream given. With recommend=True it goes to the recommendations stream as "X recommends … to @Y",
    mentioning each person in `to` (nobody mentioned = for everyone). Plain Zulip markdown, so LaTeX renders."""
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
    if recommend:
        me = zulip_me()
        who = ", ".join(f"@**{n}**" for n in to if n) or "everyone"
        lines.insert(0, f"**{me.get('full_name') or me.get('name') or 'someone'}** recommends this to {who}")  # bold only the name: @**x** is a mention
        if note.strip():
            lines += ["", "> " + note.strip().replace("\n", "\n> ")]
        stream = zulip_recs_stream()
    elif note.strip():
        lines += ["", note.strip()]
    topic = (topic.strip() or title or label)[:60]
    stream = (stream or "").strip() or zulip_stream_for(k)
    r = zulip_request("/messages", {"type": "stream", "to": stream, "topic": topic, "content": "\n".join(lines)})
    threading.Thread(target=zulip_fetch_stream, args=(stream,), daemon=True).start()
    return {"ok": True, "id": r.get("id"), "stream": stream, "topic": topic}

def topics_with_items(topics):
    """Attach to each topic the item it is about, when feedel has it (feed, inbox or library)."""
    by_key = {}
    with LOCK:
        pool = STATE["items"] + inbox_list() + [v for v in SEEN_STATE["saved"].values() if isinstance(v, dict)]
    for it in pool:
        k = item_key(it)
        if k and k not in by_key:
            by_key[k] = it
    for t in topics:
        t["item"] = next((by_key[k] for k in t["keys"] if k in by_key), None)
    return topics

# ---------------------------------------------------------------- instagram (your direct messages, through Meta's API with Instagram login)

IG_API = "https://graph.instagram.com/v23.0"

def ig_cfg():
    z = CONFIG.get("instagram") or {}
    return (z.get("token") or "").strip(), str(z.get("user_id") or ""), z.get("username") or "", int(z.get("expires") or 0)

def ig_ready():
    token, uid, _, _ = ig_cfg()
    return bool(token and uid)

def ig_api():
    return ((CONFIG.get("instagram") or {}).get("api") or IG_API).rstrip("/")

def doh_resolve(host):
    """An address for a host through DNS over HTTPS, for when the system's resolver refuses it (a VPN or ad-blocking
    resolver that lists graph.instagram.com as a tracker). Cached for the run."""
    cache = STATE.setdefault("doh", {})
    if host in cache:
        return cache[host]
    for base in ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve"):
        try:
            d = json.loads(fetch(base + "?" + urlencode({"name": host, "type": "A"}), timeout=10, headers={"Accept": "application/dns-json"}))
            ip = next((a["data"] for a in d.get("Answer") or [] if a.get("type") == 1), None)
            if ip:
                cache[host] = ip
                return ip
        except Exception:
            continue
    return None

DOH_NOTED = set()

def fetch_pinned(url, ip, timeout=25, headers=None, data=None, method=None):
    """fetch(), but to a given address, with TLS still verified against the host's name (SNI and certificate)."""
    import http.client, socket, ssl
    u = urlparse(url)
    class Pinned(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((ip, self.port), self.timeout)
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    conn = Pinned(u.hostname, u.port or 443, timeout=timeout)
    hdrs = {"User-Agent": "feedel/1.0", "Host": u.hostname, **(headers or {})}
    conn.request(method or ("POST" if data is not None else "GET"), u.path + ("?" + u.query if u.query else ""), body=data, headers=hdrs)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    if r.status >= 400:
        raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, io.BytesIO(body))
    return body

def ig_http(url, data=None, headers=None, method=None, timeout=25):
    """A request to Meta's API that survives a resolver blocking its host: on a name-resolution failure the host is
    looked up over DNS over HTTPS and the connection pinned to that address."""
    import socket
    try:
        return fetch(url, timeout=timeout, data=data, headers=headers, method=method)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), socket.gaierror) or "getaddrinfo" in str(e):
            host = urlparse(url).hostname
            ip = doh_resolve(host)
            if not ip:
                raise RuntimeError(f"{host} cannot be resolved here (your DNS blocks it) and the DNS-over-HTTPS fallback failed too")
            if host not in DOH_NOTED:
                DOH_NOTED.add(host)
                log(f"  {host}: the system resolver refuses it; connecting to {ip} directly (from now on, quietly)")
            return fetch_pinned(url, ip, timeout=timeout, headers=headers, data=data, method=method)
        raise

def ig_request(path, params=None, data=None, token=None):
    """One call to Meta's Instagram API as you (Bearer token). `data` posts JSON. Errors come back as Meta phrased them."""
    token = token or ig_cfg()[0]
    if not token:
        raise RuntimeError("Instagram is not connected yet: paste a token under settings")
    url = (ig_api() if path.startswith("/") else path) + (path if path.startswith("/") else "")
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    headers = {"Authorization": "Bearer " + token}
    body = None
    if data is not None:
        body, headers["Content-Type"] = json.dumps(data).encode(), "application/json"
    try:
        r = ig_http(url, data=body, headers=headers)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")
        log(f"  instagram: {e.code} on {path[:40]}: {msg[:400]}")  # Meta's whole answer (code, subcode, trace), for when the message alone says nothing
        try:
            err = json.loads(msg).get("error") or {}
            msg = err.get("error_user_msg") or err.get("message") or msg
            if err.get("code") == 190:
                msg = "the token is no longer valid (expired or revoked): generate a new one in Meta's app dashboard. " + msg
            elif err.get("code") == 200 and "blocked" in (err.get("message") or "").lower():
                msg = ("Meta has blocked the app itself, not the token (this follows switching the app to Live without App Review, or an "
                       "alert left unanswered): in the app dashboard look for an alert or restriction, set App Mode back to Development, "
                       "then generate a new token under Instagram → API setup and paste it under settings → Instagram. " + msg)
        except Exception:
            pass
        raise RuntimeError(f"Instagram said {e.code}: {msg[:300]}")
    return json.loads(r)

def ig_connect(token):
    """Validate a token against /me, remember who you are, and pull the direct messages once."""
    token = (token or "").strip()
    if not token:
        raise RuntimeError("paste the token first")
    me = ig_request("/me", {"fields": "id,user_id,username,name,account_type"}, token=token)
    if not me.get("id"):
        raise RuntimeError("Meta answered without an account id: is that an Instagram user token?")
    with LOCK:
        CONFIG["instagram"] = {**(CONFIG.get("instagram") or {}), "token": token, "user_id": str(me["id"]), "pro_id": str(me.get("user_id") or ""),
                               "username": me.get("username", ""), "name": me.get("name", ""), "account_type": me.get("account_type", ""),
                               "expires": int(time.time()) + 60 * 86400}
        save_config()
    STATE["ig_conv"] = {}
    n = ig_fetch_dms()
    return {"ok": True, "username": me.get("username", ""), "name": me.get("name", ""), "account_type": me.get("account_type", ""), "new": n,
            "error": STATE.get("ig_error")}

def ig_disconnect():
    with LOCK:
        CONFIG["instagram"] = {k: v for k, v in (CONFIG.get("instagram") or {}).items() if k == "api"}
        save_config()
    STATE["ig_conv"] = {}
    return {"ok": True}

def ig_refresh_token():
    """A long-lived token lasts 60 days and can be refreshed once it is older than a day; done when under ten days remain."""
    token, _, _, expires = ig_cfg()
    if not token or expires - time.time() > 10 * 86400:
        return
    try:
        r = json.loads(ig_http("https://graph.instagram.com/refresh_access_token?" + urlencode({"grant_type": "ig_refresh_token", "access_token": token}), timeout=20))
        if r.get("access_token"):
            with LOCK:
                CONFIG["instagram"].update({"token": r["access_token"], "expires": int(time.time()) + int(r.get("expires_in") or 60 * 86400)})
                save_config()
            log("instagram: token refreshed")
    except Exception as ex:
        log("instagram: token refresh failed:", ex)

def _ig_links(m):
    """The links a direct message carries: a shared reel or post (Meta lists it under `shares`, as a plain
    instagram.com link), attached media, and any link written in the text."""
    out = [str(sh.get("link") or "") for sh in ((m.get("shares") or {}).get("data") or [])]
    for a in ((m.get("attachments") or {}).get("data") or []):
        for key in ("image_data", "video_data", "file_data"):
            u = (a.get(key) or {}).get("url")
            if u:
                out.append(u)
        u = (a.get("payload") or {}).get("url") or a.get("url")
        if u:
            out.append(u)
    out += re.findall(r"https?://\S+", m.get("message") or "")
    seen, uniq = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

def _ig_body(m, text, links):
    """What to show for a direct message: its text, plus what Meta only describes: a shared reel, a reply to or a
    mention in a story (with the story while it lasts), or something it keeps for its app (a sticker, a voice note)."""
    body = html.escape(text).replace("\n", "<br>") if text else ""
    story = m.get("story") or {}
    for key, what in (("reply_to", "replied to"), ("mention", "mentioned you in")):
        if key in story:
            link = (story.get(key) or {}).get("link") or ""
            noun = "your story" if key == "reply_to" else "a story"
            tag = f'<a href="{html.escape(link)}" target="_blank" rel="noopener">{noun}</a>' if link.startswith("https://") else noun
            body = f'<span class="meta">{what} {tag}' + (":</span> " + body if body else "</span>")
    if not body:
        shared = [u for u in links if link_kind(u) == "ig"]
        titles = [str((a.get("generic_template") or {}).get("title") or "") for a in ((m.get("attachments") or {}).get("data") or [])]
        if shared:
            body = '<span class="meta">shared ' + ("a post" if "/p/" in shared[0] else "a reel") + "</span>"
        elif links:
            body = '<span class="meta">[shared media]</span>'
        elif any(titles):
            body = '<span class="meta">[Instagram: ' + html.escape(next(t for t in titles if t)) + "]</span>"
        elif m.get("is_unsupported"):
            body = '<span class="meta">[a sticker, a voice note or a post Instagram does not hand over]</span>'
    return body

def ig_fetch_dms():
    """Your Instagram direct messages into the messages tab: the conversations that changed since last time, their
    20 most recent messages (all the API hands over). Messages from you are stored as yours."""
    if not ig_ready():
        return 0
    token, my_id, my_name, _ = ig_cfg()
    if not (CONFIG.get("instagram") or {}).get("pro_id"):  # connected before feedel learned your second id: ask for it once
        try:
            me = ig_request("/me", {"fields": "id,user_id"})
            with LOCK:
                CONFIG["instagram"]["pro_id"] = str(me.get("user_id") or me.get("id") or "")
                save_config()
        except Exception as ex:
            log("  instagram: could not learn your messaging id:", ex)
    my_ids = {my_id, str((CONFIG.get("instagram") or {}).get("pro_id") or "")} - {""}
    with tx() as db:  # messages of yours stored as a friend's while that id was unknown
        db.execute("UPDATE messages SET channel = 'me', read_at = COALESCE(read_at, ?) WHERE user_id = ? AND channel = 'ig' AND lower(sender) = lower(?)",
                   (int(time.time()), USER_ID, my_name))
    ig_refresh_token()
    try:
        convs = ig_request("/me/conversations", {"platform": "instagram", "fields": "id,updated_time", "limit": 25}).get("data") or []
    except Exception as ex:
        STATE["ig_error"] = str(ex)
        return 0
    STATE["ig_empty"] = not convs  # an unpublished Meta app is handed only the conversations with its testers
    if my_name:  # messages of yours stored before the professional id counted as you: they are yours, and their other party is the conversation's
        with tx() as db:
            db.execute("UPDATE messages SET channel='me', read_at=COALESCE(read_at, ts) WHERE channel='ig' AND sender=? AND ext_id LIKE 'ig:%'", (my_name,))
            rows = db.execute("SELECT id, meta FROM messages WHERE user_id = ? AND channel='me' AND ext_id LIKE 'ig:%' AND meta LIKE ?",
                              (USER_ID, '%"username": "' + my_name + '"%')).fetchall()
            for r in rows:
                meta = json.loads(r["meta"] or "{}")
                cid = (meta.get("ig") or {}).get("conversation")
                other = db.execute("SELECT meta FROM messages WHERE user_id = ? AND channel='ig' AND meta LIKE ? LIMIT 1",
                                   (USER_ID, '%"conversation": "' + str(cid) + '"%')).fetchone() if cid else None
                if other:
                    o = (json.loads(other["meta"] or "{}").get("ig") or {})
                    meta["ig"] = {"igsid": o.get("igsid", ""), "username": o.get("username", ""), "conversation": cid}
                    meta["to"] = [{"name": o.get("username", ""), "email": ""}]
                    db.execute("UPDATE messages SET meta = ? WHERE id = ?", (json.dumps(meta), r["id"]))
    with LOCK:
        known = CACHE.setdefault("ig_conv", {})
    n = 0
    to_inbox = []
    for c in convs:
        cid, upd = c.get("id"), str(c.get("updated_time") or "") + "|s"
        if not cid or known.get(cid) == upd:
            continue
        msgs = None
        for fields in ("messages.limit(20){id,created_time,from,to,message,attachments,shares,story,is_unsupported}",
                       "messages.limit(20){id,created_time,from,to,message,attachments}", "messages.limit(20){id,created_time,from,to,message}"):
            try:
                msgs = ((ig_request("/" + cid, {"fields": fields}).get("messages") or {}).get("data")) or []
                break
            except Exception as ex:
                last = ex
        if msgs is None:  # a conversation Meta lists but will not hand over (a request, a deleted one): skipped until it changes
            log("  instagram: conversation", cid[:24], "refused:", str(last)[:120])
            with LOCK:
                known[cid] = upd
                CACHE_DIRTY.add(("ig_conv", cid))
            continue
        for m in msgs:
            frm = m.get("from") or {}
            mine = str(frm.get("id")) in my_ids or (frm.get("username") or "").lower() == my_name.lower()
            tos = [t for t in ((m.get("to") or {}).get("data") or []) if str(t.get("id")) not in my_ids]
            other = tos[0] if (mine and tos) else ({} if mine else frm)
            text = (m.get("message") or "").strip()
            links = _ig_links(m)
            body = _ig_body(m, text, links)
            if not body:
                continue
            ts = iso_ts(m.get("created_time")) or time.time()
            fresh = ts >= time.time() - MSG_FRESH
            row = msg_add("me" if mine else "ig", body, sender=frm.get("username") or ("you" if mine else "an Instagram account"), sender_email="",
                          ext_id="ig:" + str(m.get("id")), ts=ts, links=links,
                          meta={"html": True, "to": [{"name": t.get("username", ""), "email": ""} for t in tos] if mine else [],
                                "ig": {"igsid": str(other.get("id") or ""), "username": other.get("username", ""), "conversation": cid}},
                          read=mine or not fresh)  # older than a week: kept for the conversation, not as unread
            if row.get("new") and not mine and fresh:
                n += 1
                to_inbox += [(u, "@" + (frm.get("username") or "someone") + " on instagram") for u in links]
        with LOCK:
            known[cid] = upd  # the updated_time plus "|s" (fetched with its shares): conversations fetched before that are read again once
            CACHE_DIRTY.add(("ig_conv", cid))
    save_cache()
    msg_links_to_inbox(to_inbox)
    STATE["ig_error"] = None
    STATE["ig_at"] = int(time.time())
    return n

def ig_send(igsid, text, username=""):
    """A direct message to someone on Instagram. Meta only allows it within 24 hours of their last message to you."""
    token, my_id, _, _ = ig_cfg()
    if not igsid:
        raise RuntimeError("that conversation has no Instagram id to answer to")
    r = ig_request("/me/messages", data={"recipient": {"id": str(igsid)}, "message": {"text": text}})
    with LOCK:  # the conversation this belongs to, as Meta names it, so the page files the reply with the exchange
        row = DB.execute("SELECT meta FROM messages WHERE user_id = ? AND json_extract(meta, '$.ig.igsid') = ? "
                         "AND json_extract(meta, '$.ig.conversation') IS NOT NULL ORDER BY ts DESC LIMIT 1", (USER_ID, str(igsid))).fetchone()
    cid = (json.loads(row["meta"]).get("ig") or {}).get("conversation") if row else None
    return msg_add("me", html.escape(text).replace("\n", "<br>"), ext_id="ig:" + str(r.get("message_id") or r.get("id") or uuid.uuid4().hex),
                   links=[u for u in re.findall(r"https?://\S+", text) if canon_key(u)],
                   meta={"html": True, "to": [{"name": username, "email": ""}], "ig": {"igsid": str(igsid), "username": username, "conversation": cid}})

def ig_status():
    token, uid, username, expires = ig_cfg()
    z = CONFIG.get("instagram") or {}
    return {"on": bool(token and uid), "username": username, "expires": expires, "at": STATE.get("ig_at"), "error": STATE.get("ig_error"),
            "account_type": z.get("account_type", ""), "empty": bool(STATE.get("ig_empty")) if STATE.get("ig_at") else None,
            "cookies": ig_cookies_ready(), "browser": browser_cookie_name()}

# ---------------------------------------------------------------- messages (a box for notes from Claude and from friends)

MSG_LIMIT = 1500

def msg_row(r):
    d = dict(r)
    d["links"] = json.loads(d.get("links") or "[]")
    d["meta"] = json.loads(d.get("meta") or "{}")
    d["read"] = d.get("read_at") is not None
    return d

def msg_add(channel, body, title="", links=(), sender="", sender_email="", ext_id="", ts=None, reply_to=None, meta=None, read=False):
    """Store one message. channel: 'claude' (left through the MCP endpoint), 'zulip' (a friend's direct message),
    'me' (your reply or your own direct message). A Zulip message is stored once, by its id."""
    body = str(body or "").strip()
    if not body and not title:
        raise RuntimeError("the message is empty")
    now = int(time.time())
    with tx() as db:
        if ext_id:
            r = db.execute("SELECT id FROM messages WHERE user_id = ? AND channel IN ('zulip', 'ig', 'me') AND ext_id = ?", (USER_ID, ext_id)).fetchone()
            if r:
                return msg_row(db.execute("SELECT * FROM messages WHERE id = ?", (r["id"],)).fetchone())
        cur = db.execute("INSERT INTO messages (user_id, channel, ext_id, sender, sender_email, title, body, links, ts, read_at, reply_to, meta) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (USER_ID, channel, ext_id or "", sender or "", (sender_email or "").lower(), str(title or "")[:200], body[:20000],
                          json.dumps([str(l) for l in links or []][:30]), int(ts or now), now if (read or channel == "me") else None, reply_to,
                          json.dumps(meta or {})))
        row = msg_row(db.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone())
        row["new"] = True
        return row

MSG_FRESH = 7 * 86400  # a message this recent is news: unread, and its links go to the inbox

def msg_links_to_inbox(pairs):
    """Links friends send you (a reel, a video, a post, a paper) go to the inbox on their own, the way links you copy
    do: whatever feedel would open, from the week's new messages, resolved one at a time in the background."""
    if not pairs:
        return
    def go():
        seen = set()
        for url, via in pairs:
            key = canon_key(url) or url
            if key in seen:
                continue
            seen.add(key)
            try:
                if feedel_opens(url):
                    inbox_take(url, via)
            except Exception as ex:
                log(f"  inbox ({via}) failed for {url}: {ex}")
    threading.Thread(target=go, daemon=True).start()

def msg_list(include_archived=False, limit=MSG_LIMIT):
    with LOCK:
        rows = DB.execute("SELECT * FROM messages WHERE user_id = ? " + ("" if include_archived else "AND archived = 0 ") +
                          "ORDER BY ts DESC, id DESC LIMIT ?", (USER_ID, limit)).fetchall()
    return [msg_row(r) for r in rows]

def msg_update(mid, read=None, archived=None, body=None):
    with tx() as db:
        if read is not None:
            db.execute("UPDATE messages SET read_at = ? WHERE id = ? AND user_id = ?", (int(time.time()) if read else None, mid, USER_ID))
        if archived is not None:
            db.execute("UPDATE messages SET archived = ? WHERE id = ? AND user_id = ?", (1 if archived else 0, mid, USER_ID))
        if body is not None:
            db.execute("UPDATE messages SET body = ? WHERE id = ? AND user_id = ?", (str(body)[:20000], mid, USER_ID))
        r = db.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    if not r:
        raise RuntimeError("no such message")
    return msg_row(r)

def msg_update_many(ids, read=None, archived=None):
    ids = [int(i) for i in (ids or [])][:500]
    if not ids:
        return {"ok": True, "n": 0}
    marks = ",".join("?" * len(ids))
    with tx() as db:
        if read is not None:
            db.execute(f"UPDATE messages SET read_at = ? WHERE user_id = ? AND id IN ({marks})", (int(time.time()) if read else None, USER_ID, *ids))
        if archived is not None:
            db.execute(f"UPDATE messages SET archived = ? WHERE user_id = ? AND id IN ({marks})", (1 if archived else 0, USER_ID, *ids))
    return {"ok": True, "n": len(ids), "unread": msg_unread()}

def msg_unread():
    with LOCK:
        return DB.execute("SELECT COUNT(*) FROM messages WHERE user_id = ? AND archived = 0 AND read_at IS NULL AND channel != 'me'", (USER_ID,)).fetchone()[0]

def link_index():
    """Every item the feed, the library and the inbox hold, by the canonical key of its link: built once per request,
    since a messages tab holds hundreds of messages and the pool thousands of items."""
    known = {}
    with LOCK:
        pool = list(STATE["items"]) + [v for v in SEEN_STATE["saved"].values() if isinstance(v, dict)]
    pool += inbox_list()  # a reel a friend sent is in the inbox before anywhere else
    for it in pool:
        for u in (it.get("link"), it.get("id")):
            k = canon_key(u) if u else None
            if k and k not in known:
                known[k] = it
    return known

def link_titles(links, known=None):
    """What the feed knows about each link: kind, title and author when the item is in the feed, the library or the inbox."""
    out = []
    if not links:
        return out
    if known is None:
        known = link_index()
    for u in links:
        it = known.get(canon_key(u) or "")
        out.append({"url": u, "kind": it["kind"], "title": it.get("title", ""), "author": it.get("author", "")} if it else {"url": u})
    return out

MSG_FETCHING = {"on": False}

def msg_refresh_async():
    """Fetch the direct messages (Zulip, Instagram) in the background when the last look is over a minute old.
    Returns whether a fetch is running, so the page knows to ask again in a moment."""
    if MSG_FETCHING["on"]:
        return True
    z_due = zulip_ready() and (STATE.get("dm_at") or 0) < time.time() - 60
    i_due = ig_ready() and (STATE.get("ig_at") or 0) < time.time() - 60
    if not (z_due or i_due):
        return False
    MSG_FETCHING["on"] = True
    def go():
        try:
            if z_due:
                zulip_fetch_dms()
            if i_due:
                ig_fetch_dms()
        except Exception as ex:
            log("messages:", ex)
        finally:
            MSG_FETCHING["on"] = False
    threading.Thread(target=go, daemon=True).start()
    return True

def msg_payload(include_archived=False):
    msgs = msg_list(include_archived)
    site = zulip_cfg()[0]
    known = link_index() if any(m["links"] for m in msgs) else {}
    for m in msgs:
        m["links"] = link_titles(m["links"], known)
        if m["meta"].get("html") and m["channel"] in ("zulip", "me"):  # earlier messages get the same cleaning as new ones
            m["body"] = zulip_clean(m["body"], site)
    return {"messages": msgs, "unread": msg_unread(), "zulip": zulip_ready(), "dm_error": STATE.get("dm_error"), "dm_at": STATE.get("dm_at"),
            "instagram": ig_status(), "fetching": MSG_FETCHING["on"]}

def zulip_fetch_dms():
    """Your direct messages on Zulip (both ways), into the box; the 20 most recent conversations' last 100 messages."""
    if not zulip_ready():
        return 0
    site, email, _, _ = zulip_cfg()
    try:
        me = zulip_me()
        r = zulip_request("/messages", params={"anchor": "newest", "num_before": 100, "num_after": 0, "apply_markdown": "true",
                                                "narrow": json.dumps([{"operator": "is", "operand": "dm"}])})
    except Exception as ex:
        if "dm" in str(ex) and "operand" in str(ex).lower():  # older servers call it "private"
            try:
                r = zulip_request("/messages", params={"anchor": "newest", "num_before": 100, "num_after": 0, "apply_markdown": "true",
                                                        "narrow": json.dumps([{"operator": "is", "operand": "private"}])})
            except Exception as ex2:
                STATE["dm_error"] = str(ex2)
                return 0
        else:
            STATE["dm_error"] = str(ex)
            return 0
    n, to_inbox = 0, []
    for m in r.get("messages", []):
        mine = (m.get("sender_email") or "").lower() == email.lower() or m.get("sender_id") == me.get("user_id")
        people = [p for p in (m.get("display_recipient") or []) if isinstance(p, dict)]
        others = [{"name": p.get("full_name", ""), "email": (p.get("email") or "").lower()} for p in people if (p.get("email") or "").lower() != email.lower()]
        content = zulip_clean(m.get("content", ""), site)
        links = [html.unescape(h) for h in re.findall(r'href="([^"]+)"', content)]
        before = msg_unread()
        row = msg_add("me" if mine else "zulip", content, sender=m.get("sender_full_name", ""), sender_email=m.get("sender_email", ""),
                      ext_id=str(m.get("id")), ts=m.get("timestamp"), links=[l for l in links if canon_key(l)],
                      meta={"html": True, "to": others, "read": "read" in (m.get("flags") or [])}, read="read" in (m.get("flags") or []))
        n += 1 if msg_unread() != before else 0
        if row.get("new") and not mine and (m.get("timestamp") or 0) >= time.time() - MSG_FRESH:
            to_inbox += [(l, (m.get("sender_full_name") or "a friend") + " on zulip") for l in row["links"]]
    msg_links_to_inbox(to_inbox)
    STATE["dm_error"] = None
    STATE["dm_at"] = int(time.time())
    return n

def msg_send(text, to=(), reply_to=None, title=""):
    """A message from you: to friends it goes as a Zulip direct message (to the people given, or to the sender of the
    message replied to); a reply to a Claude note is kept here for Claude to read the next time it asks."""
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("nothing to send")
    parent = None
    if reply_to:
        with LOCK:
            r = DB.execute("SELECT * FROM messages WHERE id = ? AND user_id = ?", (reply_to, USER_ID)).fetchone()
        parent = msg_row(r) if r else None
    if parent and parent["channel"] == "ig":
        return ig_send(parent["meta"].get("ig", {}).get("igsid"), text, parent["sender"])
    if parent and parent["channel"] == "me" and parent["meta"].get("ig") and not to:
        return ig_send(parent["meta"]["ig"].get("igsid"), text, parent["meta"]["ig"].get("username", ""))
    emails = [str(e).strip().lower() for e in (to or []) if str(e).strip()]
    if parent and parent["channel"] in ("zulip", "me") and not emails:
        emails = [parent["sender_email"]] if parent["channel"] == "zulip" else [p["email"] for p in parent["meta"].get("to", [])]
        emails = [e for e in emails if e]
    if emails:
        r = zulip_request("/messages", {"type": "direct", "to": json.dumps(emails), "content": text})
        names = []
        try:
            names = [p["name"] for p in zulip_people() if p["email"].lower() in emails]
        except Exception:
            pass
        return msg_add("me", html.escape(text).replace("\n", "<br>"), ext_id=str(r.get("id")), reply_to=reply_to, links=[u for u in re.findall(r"https?://\S+", text) if canon_key(u)],
                       meta={"html": True, "to": [{"name": n, "email": e} for n, e in zip(names + [""] * len(emails), emails)]})
    if parent is None or parent["channel"] != "claude":
        raise RuntimeError("choose whom to send it to")
    return msg_add("me", text, title=title, reply_to=reply_to, links=[u for u in re.findall(r"https?://\S+", text) if canon_key(u)], meta={"to": [{"name": "Claude", "email": ""}]})

# ---------------------------------------------------------------- MCP (so Claude can read the feed and leave you notes)

MCP_SESSION = uuid.uuid4().hex

def mcp_item(it):
    """A feed item as a tool result: enough to judge it, nothing the page needs only for rendering."""
    link = it.get("link") or it.get("id") or ""
    if it["kind"] == "yt" and not link.startswith("http"):
        link = "https://www.youtube.com/watch?v=" + link
    out = {"kind": it["kind"], "id": it.get("id"), "link": link, "title": it.get("title", ""),
           "author": it.get("author") or it.get("channel") or "", "when": datetime.fromtimestamp(it.get("ts") or 0, timezone.utc).strftime("%Y-%m-%d %H:%M")}
    if it.get("dur"):
        out["minutes"] = round(it["dur"] / 60)
    if it.get("desc"):
        out["summary"] = strip_html(it["desc"], 300)
    if it.get("tags"):
        out["tags"] = it["tags"]
    if it["kind"] == "arxiv":
        out.update({k: it[k] for k in ("authors", "categories", "announce_type") if k in it})
    if it.get("thread"):
        out["thread"] = [t.get("title", "") for t in it["thread"]]
    if it.get("id") in SEEN_STATE["seen"]:
        out["seen"] = True
    if it.get("id") in SEEN_STATE["saved"]:
        out["saved"] = True
    return out

def mcp_feed(a):
    kind = (a.get("kind") or "").strip()
    q = (a.get("query") or "").strip().lower()
    hours = float(a.get("hours") or 0)
    limit = max(1, min(int(a.get("limit") or 40), 200))
    items = visible_items()
    if kind:
        items = [i for i in items if i["kind"] == kind]
    if hours:
        items = [i for i in items if (i.get("ts") or 0) >= time.time() - hours * 3600]
    if a.get("unseen_only"):
        items = [i for i in items if i.get("id") not in SEEN_STATE["seen"]]
    if q:
        items = [i for i in items if q in (i.get("title", "") + " " + strip_html(i.get("desc", ""), 2000) + " " + str(i.get("author", ""))).lower()]
    items.sort(key=lambda i: i.get("ts") or 0, reverse=True)
    return {"count": len(items), "items": [mcp_item(i) for i in items[:limit]]}

def mcp_library(a):
    q = (a.get("query") or "").strip().lower()
    tag = (a.get("tag") or "").strip().lower()
    limit = max(1, min(int(a.get("limit") or 50), 300))
    out = []
    with LOCK:
        saved = [v for v in SEEN_STATE["saved"].values() if isinstance(v, dict)]
    for it in sorted(saved, key=lambda v: v.get("saved_at") or 0, reverse=True):
        if tag and tag not in [t.lower() for t in (it.get("tags_mine") or []) + (it.get("tags") or [])]:
            continue
        if q and q not in (it.get("title", "") + " " + (it.get("note") or "") + " " + str(it.get("author", ""))).lower():
            continue
        d = mcp_item(it)
        d["saved_on"] = datetime.fromtimestamp(it.get("saved_at") or 0, timezone.utc).strftime("%Y-%m-%d")
        if it.get("tags_mine"):
            d["my_tags"] = it["tags_mine"]
        if it.get("note"):
            d["note"] = it["note"]
        out.append(d)
    return {"count": len(out), "items": out[:limit]}

def mcp_following(a):
    kind = (a.get("kind") or "").strip()
    out = []
    for k, s in all_subs():
        if kind and k != kind:
            continue
        out.append({"kind": k, "id": s.get("id"), "name": s.get("name") or s.get("id"), "tags": s.get("tags", []), "muted": bool(s.get("muted"))})
    return {"count": len(out), "following": out}

def mcp_discussions(a):
    if not zulip_ready():
        return {"on": False, "note": "Zulip is not set up in feedel, so there are no discussions to read"}
    limit = max(1, min(int(a.get("limit") or 30), 100))
    topics = zulip_topics((a.get("stream") or "").strip() or None)[:limit]
    return {"on": True, "topics": [{"stream": t["stream"], "topic": t["topic"], "messages": t["count"], "unread": t["new"], "people": t["people"],
                                    "last": t["last"], "last_by": t["last_who"], "about": t.get("keys", [])[:5],
                                    "last_at": datetime.fromtimestamp(t["last_ts"], timezone.utc).strftime("%Y-%m-%d %H:%M")} for t in topics]}

def mcp_messages(a):
    limit = max(1, min(int(a.get("limit") or 30), 200))
    msgs = msg_list(bool(a.get("include_archived")))
    if a.get("unread_only"):
        msgs = [m for m in msgs if not m["read"] and m["channel"] != "me"]
    out = []
    for m in msgs[:limit]:
        body = strip_html(m["body"], 4000) if m["meta"].get("html") else m["body"]
        out.append({"id": m["id"], "from": "Claude" if m["channel"] == "claude" else ("you" if m["channel"] == "me" else (m["sender"] or "a friend") + (" (Instagram)" if m["channel"] == "ig" else "")),
                    "channel": m["channel"], "to": [p.get("name") or p.get("email") for p in m["meta"].get("to", [])] or None,
                    "title": m["title"], "body": body, "links": m["links"], "read": m["read"], "reply_to": m["reply_to"],
                    "at": datetime.fromtimestamp(m["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M")})
    return {"unread": msg_unread(), "messages": out}

def mcp_send_message(a):
    body = str(a.get("body") or "").strip()
    if not body:
        raise RuntimeError("body is required")
    links = [str(u) for u in (a.get("links") or []) if str(u).startswith("http")]
    m = msg_add("claude", body, title=str(a.get("title") or "")[:200], links=links, sender="Claude", reply_to=a.get("reply_to"))
    return {"ok": True, "id": m["id"], "note": "left in the messages tab of feedel"}

def mcp_add_link(a):
    u = str(a.get("url") or "").strip()
    if not link_kind(u):
        raise RuntimeError("that does not look like a link feedel can take (a YouTube video or channel, an X post, a web page or feed, an arXiv paper)")
    return inbox_take(u, "claude")

def mcp_mark(a):
    ids = [str(i) for i in (a.get("ids") or []) if str(i)]
    if not ids:
        raise RuntimeError("ids is required")
    done = {}
    if "seen" in a:
        mark(ids, "seen", bool(a["seen"]))
        done["seen"] = bool(a["seen"])
    if "saved" in a:
        if a["saved"]:
            items = {i.get("id"): i for i in visible_items()}
            for i in ids:
                mark([i], "saved", True, item=items.get(i) or {"id": i, "kind": link_kind(i) or "rss", "title": i, "link": i},
                     patch={k: a[k] for k in ("tags", "note") if k in a})
        else:
            mark(ids, "saved", False)
        done["saved"] = bool(a["saved"])
    return {"ok": True, "ids": ids, **done}

def mcp_search_papers(a):
    q = str(a.get("query") or "").strip()
    if not q:
        raise RuntimeError("query is required")
    res = arxiv_search(q=q, author=str(a.get("author") or ""), max_results=max(1, min(int(a.get("limit") or 20), 50)))
    items = res.get("items", res) if isinstance(res, dict) else res
    return {"count": len(items), "items": [mcp_item(i) for i in items]}

MCP_TOOLS = [
    {"name": "feed", "fn": mcp_feed,
     "description": "What is in the person's feed right now: videos (kind yt), X posts (tw), blog and newsletter posts (rss) and papers (arxiv), "
                    "newest first, after their own filters (muted sources, hidden Shorts, blocked words). Filter by kind, a text query, "
                    "the last N hours, or only what they have not seen yet.",
     "schema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["yt", "tw", "rss", "arxiv"]}, "query": {"type": "string"},
                                                "hours": {"type": "number", "description": "only items from the last N hours"},
                                                "unseen_only": {"type": "boolean"}, "limit": {"type": "integer", "default": 40}}}},
    {"name": "library", "fn": mcp_library,
     "description": "What the person set aside (saved items) with their own tags and notes, newest first. Filter by text or tag.",
     "schema": {"type": "object", "properties": {"query": {"type": "string"}, "tag": {"type": "string"}, "limit": {"type": "integer", "default": 50}}}},
    {"name": "following", "fn": mcp_following,
     "description": "The channels, accounts, feeds and arXiv queries the person follows, with their tags.",
     "schema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["yt", "tw", "rss", "arxiv"]}}}},
    {"name": "discussions", "fn": mcp_discussions,
     "description": "The discussions among the person's friends (Zulip topics the app shows in its talk tab): who said what last and how much is unread.",
     "schema": {"type": "object", "properties": {"stream": {"type": "string"}, "limit": {"type": "integer", "default": 30}}}},
    {"name": "messages", "fn": mcp_messages,
     "description": "The messages tab: notes Claude left before, direct messages from friends, and the person's replies (channel 'me'). "
                    "Read it before writing a new note, so you can follow up on their replies and on unfinished tasks.",
     "schema": {"type": "object", "properties": {"unread_only": {"type": "boolean"}, "include_archived": {"type": "boolean"}, "limit": {"type": "integer", "default": 30}}}},
    {"name": "send_message", "fn": mcp_send_message,
     "description": "Leave the person a note in the messages tab of feedel. Markdown body; '- [ ] task' lines become checkboxes they can tick. "
                    "Put the links you refer to in `links` so the app shows them as cards.",
     "schema": {"type": "object", "required": ["body"], "properties": {"title": {"type": "string"}, "body": {"type": "string"},
                                                                       "links": {"type": "array", "items": {"type": "string"}},
                                                                       "reply_to": {"type": "integer", "description": "id of the message this answers"}}}},
    {"name": "add_link", "fn": mcp_add_link,
     "description": "Put a link into the person's inbox (a YouTube video, an X post, a web page, an arXiv paper), the same box links from WhatsApp land in.",
     "schema": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}}},
    {"name": "mark", "fn": mcp_mark,
     "description": "Mark feed items (by their id from `feed`) as seen or unseen, or save them to the library (optionally with tags and a note). Only on the person's request.",
     "schema": {"type": "object", "required": ["ids"], "properties": {"ids": {"type": "array", "items": {"type": "string"}}, "seen": {"type": "boolean"},
                                                                      "saved": {"type": "boolean"}, "tags": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"}}}},
    {"name": "search_papers", "fn": mcp_search_papers,
     "description": "Search arXiv (title, abstract, author) through feedel, which rate-limits politely.",
     "schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "author": {"type": "string"}, "limit": {"type": "integer", "default": 20}}}},
]

MCP_INSTRUCTIONS = ("feedel is this person's quiet reader: a feed of what they chose to follow (YouTube channels, X accounts, blogs, arXiv queries), "
                    "a library of what they saved, their friends' discussions, and a messages tab where you can leave notes. Read before you write: "
                    "`messages` shows your earlier notes and their replies. A note should be short, link the few things worth their time and say why, "
                    "and carry over the tasks they have not ticked. Never mark things seen or saved unless they asked.")

def mcp_handle(msg):
    """One JSON-RPC message of the Model Context Protocol (Streamable HTTP, requests only; no server-initiated stream).
    Returns the response dict, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "not a JSON-RPC 2.0 message"}}
    method, mid, params = msg.get("method", ""), msg.get("id"), msg.get("params") or {}
    if mid is None:  # a notification
        return None
    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "feedel", "version": "1.0"}, "instructions": MCP_INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["schema"]} for t in MCP_TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        tool = next((t for t in MCP_TOOLS if t["name"] == name), None)
        if tool is None:
            return err(-32602, f"unknown tool {name!r}")
        try:
            result = tool["fn"](params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}], "isError": False})
        except Exception as ex:
            return ok({"content": [{"type": "text", "text": str(ex)}], "isError": True})
    if method in ("resources/list", "resources/templates/list"):
        return ok({"resources": [], "resourceTemplates": []})
    if method == "prompts/list":
        return ok({"prompts": []})
    return err(-32601, f"method not found: {method}")

# ---------------------------------------------------------------- updates (a newer build on GitHub, fetched and swapped in by itself)

UPDATE_REPO = "nyazbek/feedel"
UPDATE_ASSET = {"win32": "feedel-windows.exe", "darwin": "feedel-macos"}.get(sys.platform)

def build_info():
    """Which commit this copy is: web/build.json stamped by the GitHub build, or git's answer when run from a checkout."""
    info = {}
    try:
        with open(os.path.join(WEB, "build.json"), encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        pass
    if not FROZEN and os.path.isdir(os.path.join(HERE, ".git")) and shutil.which("git"):
        try:
            sha = subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
            date = subprocess.run(["git", "-C", HERE, "log", "-1", "--format=%cI"], capture_output=True, text=True, timeout=10).stdout.strip()
            if sha:
                info = {"sha": sha, "short": sha[:7], "date": date, "ref": "source"}
        except Exception:
            pass
    if info.get("sha") and not info.get("short"):
        info["short"] = info["sha"][:7]
    return info

def gh_headers():
    h = {"Accept": "application/vnd.github+json", "User-Agent": "feedel/1.0", "X-GitHub-Api-Version": "2022-11-28"}
    t = (CONFIG.get("github_token") or "").strip()
    if t:
        h["Authorization"] = "Bearer " + t
    return h

def update_mode():
    """How this copy can update: 'exe' (swap the executable), 'git' (pull the checkout), or 'none'."""
    if FROZEN:
        return "exe" if UPDATE_ASSET else "none"
    return "git" if os.path.isdir(os.path.join(HERE, ".git")) and shutil.which("git") else "none"

def pending_path():
    return os.path.join(HERE, "feedel.new.exe" if sys.platform == "win32" else "feedel.new")

def update_status():
    st = dict(STATE.get("update") or {})
    st["mode"] = update_mode()
    st["local"] = st.get("local") or build_info()
    st["ready"] = bool(st.get("ready_sha")) and st.get("ready_sha") == (st.get("remote") or {}).get("sha") and os.path.exists(pending_path())
    st["token_set"] = bool((CONFIG.get("github_token") or "").strip())
    return st

def update_check():
    """Ask GitHub for the rolling 'latest' release of main: its commit (in the notes) and the asset for this platform."""
    st = STATE.setdefault("update", {})
    st.update({"checked": int(time.time()), "error": None, "local": build_info()})
    try:
        r = json.loads(fetch(f"https://api.github.com/repos/{UPDATE_REPO}/releases/tags/latest", timeout=20, headers=gh_headers()))
    except urllib.error.HTTPError as e:
        st["error"] = {404: "no build of main on GitHub yet, or the repository is private and no GitHub token is set (settings)",
                       401: "GitHub refused the token", 403: "GitHub refused: a private repository without a token, or too many requests"}.get(e.code, f"GitHub said {e.code}")
        st["available"] = False
        return update_status()
    except Exception as ex:
        st["error"] = str(ex)
        st["available"] = False
        return update_status()
    notes = r.get("body") or ""
    m = re.search(r"sha=([0-9a-f]{7,40})", notes)
    d = re.search(r"date=(\S+)", notes)
    remote = {"sha": m.group(1) if m else "", "short": (m.group(1) if m else "")[:7], "date": d.group(1) if d else (r.get("published_at") or "")}
    asset = next((a for a in r.get("assets", []) if a.get("name") == UPDATE_ASSET), None)
    st.update({"remote": remote, "asset_url": asset.get("url") if asset else None, "asset_size": asset.get("size") if asset else None})
    local = st["local"]
    newer = bool(remote["sha"]) and remote["sha"] != local.get("sha") and (not local.get("date") or remote["date"] > local.get("date", ""))
    st["available"] = newer and update_mode() != "none" and (update_mode() != "exe" or asset is not None)  # a plain source folder is not nagged
    return update_status()

def update_download():
    """Fetch the new executable next to this one, as feedel.new.exe; it is swapped in at the next start (or on request).
    The API's asset URL answers with a redirect to a signed download, and the token must not travel to that host."""
    st = STATE.setdefault("update", {})
    if update_mode() != "exe":
        raise RuntimeError("this copy runs from source: it updates with git pull")
    if not st.get("asset_url"):
        update_check()
    if not st.get("asset_url"):
        raise RuntimeError(st.get("error") or "no build for this platform on GitHub")
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(st["asset_url"], headers={**gh_headers(), "Accept": "application/octet-stream"})
    loc, first = "", None
    try:
        first = opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise RuntimeError(f"GitHub said {e.code} for the download")
        loc = e.headers.get("Location", "")
    tmp = pending_path() + ".part"
    with open(tmp, "wb") as f:
        if first is not None and not first.headers.get("Location"):
            shutil.copyfileobj(first, f, 1 << 20)
        else:
            loc = loc or first.headers.get("Location", "")
            with urllib.request.urlopen(urllib.request.Request(loc, headers={"User-Agent": "feedel/1.0"}), timeout=900) as r:
                shutil.copyfileobj(r, f, 1 << 20)
    if os.path.getsize(tmp) < 5_000_000:
        os.remove(tmp)
        raise RuntimeError("the download is too small to be feedel; not applied")
    os.replace(tmp, pending_path())
    if sys.platform != "win32":
        os.chmod(pending_path(), 0o755)
    st["ready_sha"] = (st.get("remote") or {}).get("sha")
    log(f"update downloaded: {st['ready_sha'][:7] if st.get('ready_sha') else '?'} → {pending_path()}")
    return update_status()

def swap_pending_exe():
    """Put the downloaded executable in place of this one. A running executable can be renamed on Windows, not
    overwritten: this one becomes feedel.exe.old, which the next start deletes. True when a swap happened."""
    new, exe = pending_path(), sys.executable
    if not (FROZEN and os.path.exists(new) and os.path.getsize(new) > 5_000_000):
        return False
    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:  # a copy of the last version is still running (it did not exit after its update): another name
        old = exe + f".old{int(time.time())}"
    os.replace(exe, old)
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)  # put it back; nothing changed
        raise
    log("updated: the new executable is in place")
    return True

def cleanup_old_exe():
    if not FROZEN:
        return
    for name in os.listdir(os.path.dirname(sys.executable) or "."):
        if name.startswith(os.path.basename(sys.executable) + ".old"):
            try:
                os.remove(os.path.join(os.path.dirname(sys.executable), name))
            except OSError:
                pass  # still exiting; next start

def update_apply():
    """Use the new version now: swap the executable, or pull the checkout, then restart."""
    mode = update_mode()
    st = STATE.get("update") or {}
    if mode == "exe":
        if not swap_pending_exe():
            if not st.get("available"):
                raise RuntimeError("this copy is already the latest build")
            update_download()
            swap_pending_exe()
    elif mode == "git":
        r = subprocess.run(["git", "-C", HERE, "pull", "--ff-only"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("git pull failed: " + (r.stderr or r.stdout).strip()[:300])
        log("updated: " + (r.stdout.strip().splitlines() or ["pulled"])[-1])
    else:
        raise RuntimeError("this copy cannot update itself (no executable asset and no git checkout)")
    STATE.setdefault("update", {}).update({"available": False, "ready_sha": None})
    restart_self()
    return {"ok": True, "restarting": True}

def update_loop():
    """Once soon after start and then every six hours: look, and (unless turned off) fetch the new executable so the
    next start is already the new version."""
    time.sleep(25)
    while True:
        try:
            st = update_check()
            if st.get("available") and CONFIG["ui"].get("auto_update", True) and update_mode() == "exe" and not st.get("ready"):
                update_download()
        except Exception as ex:
            log("update check:", ex)
        time.sleep(6 * 3600)

# ---------------------------------------------------------------- claude (Claude Code: the MCP connection and the daily briefing)

def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")

def claude_env():
    """The environment for a `claude` child. When feedel itself was started from inside a Claude Code session, the
    session's CLAUDE_* variables would make the child behave as that session's helper (and fail to log in on its
    own): a child of feedel is always a fresh, stand-alone Claude Code."""
    return {k: v for k, v in os.environ.items() if not (k.startswith("CLAUDE") or k == "ANTHROPIC_BASE_URL")}

def claude_cli():
    """The `claude` command-line program (Claude Code), if installed."""
    p = find_exe("claude")
    if p:
        return p
    for c in (os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe"), os.path.join(os.path.expanduser("~"), ".local", "bin", "claude"),
              "/usr/local/bin/claude", "/opt/homebrew/bin/claude"):
        if os.path.isfile(c):
            return c
    return None

def mcp_url():
    return f"http://127.0.0.1:{RUN_PORT}/mcp"

def claude_json_path():
    return os.path.join(os.path.expanduser("~"), ".claude.json")

def mcp_installed():
    """Is this feedel registered as the MCP server `feedel` in Claude Code's user config?"""
    try:
        with open(claude_json_path(), encoding="utf-8") as f:
            d = json.load(f)
        e = (d.get("mcpServers") or {}).get("feedel") or {}
        return e.get("url") == mcp_url(), e.get("url")
    except Exception:
        return False, None

def mcp_install(on=True):
    """Register (or remove) this feedel as the MCP server `feedel` for Claude Code, user-wide: through the `claude`
    program when it is there, else by editing ~/.claude.json the way it would."""
    cli = claude_cli()
    if cli:
        cmd = [cli, "mcp", "add", "--transport", "http", "--scope", "user", "feedel", mcp_url()] if on else [cli, "mcp", "remove", "--scope", "user", "feedel"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=claude_env())
            if r.returncode == 0 or (not on and "not found" in (r.stderr + r.stdout).lower()):
                return {"ok": True, "installed": mcp_installed()[0], "how": "claude mcp", "note": (r.stdout or r.stderr).strip()[:200]}
            note = (r.stderr or r.stdout).strip()[:200]
        except Exception as ex:
            note = str(ex)[:200]
    else:
        note = "the claude program is not installed (Claude Code); the config file was edited directly"
    path = claude_json_path()
    try:
        d = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            shutil.copy2(path, path + ".feedel-backup")
        servers = d.setdefault("mcpServers", {})
        if on:
            servers["feedel"] = {"type": "http", "url": mcp_url()}
        else:
            servers.pop("feedel", None)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, path)
        return {"ok": True, "installed": mcp_installed()[0], "how": "config file", "note": note}
    except Exception as ex:
        raise RuntimeError(f"could not register with Claude Code: {note}; {ex}")

def briefing_cfg():
    b = CONFIG.setdefault("briefing", {})
    for k, v in DEFAULT_CONFIG["briefing"].items():
        b.setdefault(k, v)
    return b

def briefing_prompt():
    """The instructions a run of the briefing gets. Self-contained: the run has no memory of anything else."""
    b = briefing_cfg()
    who = (b.get("name") or "").strip()
    lang = (b.get("language") or "").strip() or "the language the person writes in"
    profile = (b.get("profile") or "").strip()
    extra = (b.get("extra") or "").strip()
    L = [f"Write today's briefing into feedel, {who + chr(39) + 's' if who else 'the person' + chr(39) + 's'} quiet feed reader, as a note in its messages tab. "
         f"Write it in {lang}, under 200 words.", "",
         "## How to talk to feedel", "",
         f"feedel runs on this computer and is an MCP server named `feedel` ({mcp_url()}) with the tools `messages`, `feed`, `library`, "
         "`discussions`, `following`, `send_message`. Use those tools. If they are not available, call the same tools over plain HTTP "
         f"(JSON-RPC 2.0): POST to {mcp_url()} a body like "
         '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"feed","arguments":{"hours":36,"unseen_only":true,"limit":40}}} '
         "with header Content-Type: application/json; the answer's result.content[0].text is JSON. If feedel does not answer at all, stop and "
         "report that in one line; do not write the briefing anywhere else.", ""]
    if profile:
        L += ["## Papers and posts: only their area", "",
              f"What {who or 'the person'} works on, in their words: {profile}", "",
              "A category watch is far too wide to recommend from: recommend a paper only when it sits in that area, at most three per "
              "briefing, each with one line on which of their topics it touches, newest first; none is fine when nothing fits, say so in one "
              "line rather than padding. Before choosing, call `library` with an empty query and look at the papers they saved and their notes, "
              "and `discussions` for what friends are talking about; both refine the area (a saved paper's topic counts as in).", ""]
    L += ["## Steps", "",
          "1. Read before writing: `messages` with limit 10. Find your last briefing (sender Claude). Carry over its unticked `- [ ]` tasks "
          "(at most five, oldest first, marked with the day they came from); if the person replied under it (channel \"me\"), answer each reply "
          "in one line in the new note. Every briefing is a note of its own, never a reply to an earlier one: when a note of yours "
          "already exists today, the new one is short and says only what changed (its title gets the time: \"martes 20, 15:30\").",
          "2. Look at the day: `feed` with hours 36 and unseen_only true, once per kind (yt, tw, rss, arxiv; for arxiv use limit 150); "
          "`library` with limit 10 (their tags and notes show what they care about); `discussions` with limit 20.",
          "3. Choose two to five items in all, no more. A long video from a channel they follow beats ten posts; a paper in their area beats "
          "everything else; a discussion a friend opened about something in the feed is always worth a line. Skip hype. Prefer what connects "
          "to their library.",
          "4. Write the note with `send_message`: `title` is today's date in that language; `body` in Markdown with one opening line on the "
          "shape of the day, a `### para hoy` / `### for today` section with the picks as task lines `- [ ] watch … (20 min) — why`, a "
          "`### papers` section with the papers that passed (title, authors, one line why) or one line saying nothing fit, a "
          "`### con amigos` / `### with friends` section only if there are open discussions, the carried-over tasks, and one closing line if "
          "something needs a decision or a reply; `links` holds the URLs of every item mentioned, in the order mentioned.",
          "5. Never mark anything seen or saved, never add links to the inbox, never use any tool of feedel other than the ones named above."]
    if extra:
        L += ["", "## Also", "", extra]
    return "\n".join(L)

BRIEFING_TOOLS = "mcp__feedel__messages,mcp__feedel__feed,mcp__feedel__library,mcp__feedel__discussions,mcp__feedel__following,mcp__feedel__send_message"

def briefing_run(trigger="by hand"):
    """Run the briefing now: Claude Code, headless, with only feedel's tools allowed. Blocks until it is done."""
    cli = claude_cli()
    b = briefing_cfg()
    prev = b.get("last_run") or {}
    same_day = time.strftime("%Y-%m-%d", time.localtime(prev.get("at") or 0)) == time.strftime("%Y-%m-%d")
    rec = {"at": int(time.time()), "trigger": trigger, "ok": False, "output": "", "tries": (prev.get("tries") or 0) + 1 if same_day else 1}
    log(f"briefing ({trigger}): starting")
    if not cli:
        rec["output"] = "Claude Code is not installed (no `claude` program found)"
    else:
        cmd = [cli, "-p", briefing_prompt(), "--allowedTools", BRIEFING_TOOLS, "--output-format", "text"]
        if (b.get("model") or "").strip():
            cmd += ["--model", b["model"].strip()]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1500, cwd=HERE, encoding="utf-8", errors="replace", env=claude_env())
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            rec["ok"] = r.returncode == 0 and "Failed to authenticate" not in out + err
            rec["output"] = (out or err)[-1500:]
            if "Failed to authenticate" in out + err or "not logged in" in (out + err).lower():
                rec["output"] = "Claude Code is not logged in: run `claude login` in a terminal once. " + rec["output"]
        except subprocess.TimeoutExpired:
            rec["output"] = "the run took more than 25 minutes and was stopped"
        except Exception as ex:
            rec["output"] = str(ex)[:500]
    with LOCK:
        briefing_cfg()["last_run"] = rec
        save_config()
    log(f"briefing ({trigger}): {'ok' if rec['ok'] else 'failed'}: {rec['output'][:120]}")
    return rec

BRIEFING_STARTED = time.time()

def briefing_due(now=None):
    """Due once the configured time has passed today and no run has happened today, so a copy that was not running
    at that minute (the computer was off, or feedel was started later) catches up; a scheduled run that failed is
    retried an hour later, three times at most. The first two minutes after start are left to the feed's refresh."""
    b = briefing_cfg()
    if not b.get("on"):
        return False
    now = now or time.localtime()
    hh, mm = (b.get("time") or "08:00").split(":")[:2]
    if (now.tm_hour, now.tm_min) < (int(hh), int(mm)):
        return False
    if b.get("days") == "weekdays" and now.tm_wday >= 5:
        return False
    if time.time() - BRIEFING_STARTED < 120:
        return False
    last = b.get("last_run") or {}
    today = time.strftime("%Y-%m-%d", now)
    if time.strftime("%Y-%m-%d", time.localtime(last.get("at") or 0)) != today:
        return True
    return (not last.get("ok") and last.get("trigger") == "schedule" and (last.get("tries") or 1) < 3
            and time.time() - (last.get("at") or 0) > 3600)

def briefing_loop():
    """Once a minute: when it is the configured time and today's briefing has not run, run it."""
    while True:
        try:
            if briefing_due():
                threading.Thread(target=briefing_run, args=("schedule",), daemon=True).start()
        except Exception as ex:
            log("briefing loop:", ex)
        time.sleep(60)

def briefing_task_path():
    return os.path.join(claude_home(), "scheduled-tasks", "feedel-briefing", "SKILL.md")

def briefing_install_task():
    """Write the briefing as a Claude Code scheduled task (its prompt file); the time is set in Claude Code's own
    Scheduled list, which keeps the schedule in its own store."""
    path = briefing_task_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    desc = "Every morning, write the day's briefing into feedel's messages tab: picks, papers only from the person's area, friends' discussions, carried-over tasks."
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"---\nname: feedel-briefing\ndescription: {desc}\n---\n\n{briefing_prompt()}\n")
    return {"ok": True, "path": path}

def claude_status():
    installed, url = mcp_installed()
    b = briefing_cfg()
    return {"cli": claude_cli(), "mcp_installed": installed, "mcp_url": mcp_url(), "mcp_other_url": url if url and not installed else None,
            "briefing": {k: b.get(k) for k in ("on", "time", "days", "language", "name", "profile", "extra", "model")},
            "last_run": b.get("last_run"), "task_file": os.path.exists(briefing_task_path())}

def briefing_update(patch):
    with LOCK:
        b = briefing_cfg()
        if "on" in patch:
            b["on"] = bool(patch["on"])
        if "time" in patch:
            t = str(patch["time"] or "08:00").strip()
            if not re.fullmatch(r"\d{1,2}:\d{2}", t):
                raise RuntimeError("the time is HH:MM")
            hh, mm = t.split(":")
            b["time"] = f"{int(hh):02d}:{int(mm):02d}"
        if "days" in patch:
            b["days"] = "weekdays" if patch["days"] == "weekdays" else "daily"
        for k in ("language", "name", "profile", "extra", "model"):
            if k in patch:
                b[k] = str(patch[k] or "").strip()[:4000]
        save_config()
    return claude_status()

# ---------------------------------------------------------------- window (optional: the page in its own window, with a tray icon)
# `--window`, or the ui.window setting: the page opens in a native window (pywebview: WebView2 on Windows, WebKit on macOS)
# instead of a browser tab, and closing it hides it to a tray icon (pystray) whose menu is open / refresh / quit. The server
# runs in this same process as always. Both libraries are imported only in here, so the server stays standard-library only;
# without them the browser opens exactly as before and the log says what to install.
WINDOW = {}  # set by run_window(): "show" (bring the window back, optionally on a link) and "quit" (close everything)
WINDOW_SIZE = (1100, 800)
WINDOW_LIBS = ("webview", "pystray", "PIL")  # import names of pywebview, pystray and Pillow

def window_libs_missing():
    """The window libraries that are not installed (checked without importing them). Cached: packages do not appear mid-run."""
    if "missing" not in WINDOW:
        import importlib.util
        WINDOW["missing"] = [m for m in WINDOW_LIBS if importlib.util.find_spec(m) is None]
    return WINDOW["missing"]

def window_wanted():
    """--window, or the setting, unless --no-open (no page at all) or --browser (this once in the browser) says otherwise."""
    if "--no-open" in sys.argv or "--browser" in sys.argv:
        return False
    if "--window" in sys.argv:
        return True
    return bool(CONFIG["ui"].get("window"))

def window_available():
    """True when the window can be used now; otherwise logs the one line that says what to install and returns False."""
    missing = window_libs_missing()
    if not missing:
        return True
    pip = " ".join({"webview": "pywebview", "pystray": "pystray", "PIL": "pillow"}[m] for m in missing)
    log(f"window mode needs {', '.join(missing)}; run `pip install {pip}` (or build with them) — opening the browser instead")
    return False

def show_running_window(port, link=None):
    """Second launch (or the link handler) while feedel is already running: ask that instance to show its window.
    True when it did, False when it runs in a browser tab (or is an older version), so the caller opens the browser."""
    try:
        body = json.dumps({"link": link} if link else {}).encode()
        r = fetch(f"http://127.0.0.1:{port}/api/show", timeout=3, data=body, headers={"Content-Type": "application/json"})
        return bool(json.loads(r).get("shown"))
    except Exception:
        return False

def tray_image():
    """A 64x64 icon drawn here, not shipped: a dark rounded square with three pale lines, a quiet list."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 59, 59), radius=14, fill=(43, 43, 43, 255))
    for y in (20, 31, 42):
        d.rounded_rectangle((17, y, 47, y + 4), radius=2, fill=(233, 228, 216, 255))
    return img

def window_geometry():
    """Saved size and position, checked against the screens that exist now (a monitor may have gone)."""
    import webview
    g = CONFIG.get("window") or {}
    w, h = int(g.get("width") or WINDOW_SIZE[0]), int(g.get("height") or WINDOW_SIZE[1])
    x, y = g.get("x"), g.get("y")
    if x is not None and y is not None:
        try:
            on_screen = any(s.x - w + 120 <= x <= s.x + s.width - 120 and s.y - 20 <= y <= s.y + s.height - 120 for s in webview.screens)
        except Exception:
            on_screen = True
        if not on_screen:
            x = y = None
    return max(w, 480), max(h, 360), x, y

def run_window(port, path=""):
    """Blocks in the GUI loop on the main thread until the user quits; the server is already serving on another thread.
    Call window_available() first: this assumes the libraries are there."""
    import webview, pystray
    url = f"http://localhost:{port}/{path}"
    w, h, x, y = window_geometry()
    win = webview.create_window("feedel", url, width=w, height=h, x=x, y=y, min_size=(480, 360), text_select=True)
    saver = [None]
    geom = CONFIG.setdefault("window", {})

    def save_later():  # a resize or move fires many times; write the settings once it stops
        if saver[0]:
            saver[0].cancel()
        saver[0] = threading.Timer(1.0, save_config)
        saver[0].daemon = True
        saver[0].start()

    def on_resized(width, height):
        geom["width"], geom["height"] = int(width), int(height)
        save_later()

    def on_moved(mx, my):
        if mx > -30000 and my > -30000:  # Windows parks a minimized window at -32000
            geom["x"], geom["y"] = int(mx), int(my)
            save_later()

    def show(link=None):
        if link:
            try:
                inbox_take(link, "link")
            except Exception as ex:
                log(f"could not open {link}: {ex}")
            win.load_url(f"http://localhost:{port}/#inbox")
        win.show()
        try:
            win.restore()
        except Exception:
            pass

    def quit_app():
        if WINDOW.get("quitting"):
            return
        WINDOW["quitting"] = True
        try:
            if saver[0]:
                saver[0].cancel()
            save_config()
            save_cache()
        except Exception:
            pass
        try:
            if WINDOW.get("tray"):
                WINDOW["tray"].stop()
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass
        threading.Timer(1.0, lambda: os._exit(0)).start()  # whatever the GUI loop does with that, the process ends

    def on_closing():  # the close button hides the window; the tray menu is where feedel is quit
        if WINDOW.get("quitting") or not WINDOW.get("tray"):
            return True
        win.hide()
        return False

    win.events.resized += on_resized
    win.events.moved += on_moved
    win.events.closing += on_closing
    def teardown():
        """For restart_self: free the port, drop the tray icon and hide the window, all quick; os._exit does the rest.
        The port first: WebView2 takes a couple of seconds to unload when the process exits, and the restarted process
        would find the port still busy and think feedel is already running."""
        WINDOW["quitting"] = True
        for f in (lambda: WINDOW["server"].server_close() if WINDOW.get("server") else None,
                  lambda: WINDOW["tray"].stop() if WINDOW.get("tray") else None, win.hide):
            try:
                f()
            except Exception:
                pass

    WINDOW["show"], WINDOW["quit"], WINDOW["teardown"] = show, quit_app, teardown

    def start_tray():
        menu = pystray.Menu(pystray.MenuItem("open", lambda: show(), default=True),
                            pystray.MenuItem("refresh", lambda: threading.Thread(target=refresh, daemon=True).start()),
                            pystray.MenuItem("quit", lambda: quit_app()))
        tray = pystray.Icon("feedel", tray_image(), "feedel", menu=menu)
        try:
            if sys.platform == "darwin":
                from AppKit import NSApplication
                tray.run_detached(darwin_nsapplication=NSApplication.sharedApplication())
            else:
                tray.run_detached()
            WINDOW["tray"] = tray
        except Exception as ex:
            log(f"no tray icon ({ex}); closing the window quits feedel")

    log("feedel in its own window (close hides it to the tray; the tray menu quits)")
    webview.start(start_tray, private_mode=False, storage_path=os.path.join(HERE, "cache", "webview"))
    quit_app()  # the window is gone (quit from the tray, or closed with no tray): stop the server too

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

    def do_mcp(self):
        """The Model Context Protocol over plain HTTP: a JSON-RPC request (or a batch) in, a JSON answer out. Only
        pages served from this machine may call it (a browser's Origin header is checked against localhost)."""
        origin = self.headers.get("Origin") or ""
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return self._send({"error": "origin not allowed"}, code=403)
        try:
            msg = self._json()
        except Exception as ex:
            return self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(ex)}}, code=400)
        answers = [r for r in (mcp_handle(m) for m in (msg if isinstance(msg, list) else [msg])) if r is not None]
        if not answers:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Mcp-Session-Id", MCP_SESSION)
            self.end_headers()
            return
        body = json.dumps(answers if isinstance(msg, list) else answers[0], ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", MCP_SESSION)
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        if urlparse(self.path).path == "/mcp":
            return self._send({"ok": True})
        return self._send("not found", "text/plain", 404)

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        try:
            if p.path in ("/", "/index.html", "/app.js", "/style.css"):
                name = "index.html" if p.path == "/" else p.path[1:]
                with open(os.path.join(WEB, name), "rb") as f:
                    return self._send(f.read(), MIME[os.path.splitext(name)[1]])
            if p.path == "/api/items":  # ?limits=tw:240,yt:120 — how many of each kind the page is showing
                limits = {}
                for part in q.get("limits", [""])[0].split(","):
                    if ":" in part:
                        k, n = part.split(":", 1)
                        if n.isdigit():
                            limits[k] = int(n)
                return self._send(items_payload(limits))
            if p.path == "/api/subs":
                return self._send(subs_payload())
            if p.path == "/api/unfollowed":
                return self._send(unfollowed_list())
            if p.path == "/api/sync":
                return self._send(sync_payload())
            if p.path == "/api/inbox":
                return self._send(inbox_list())
            if p.path == "/api/claude/status":
                return self._send(claude_status())
            if p.path == "/api/claude/briefing/prompt":
                return self._send(briefing_prompt(), "text/plain; charset=utf-8")
            if p.path == "/api/unshorten":  # where a shortener's link leads, so the page can decide inbox or browser
                u = q.get("url", [""])[0]
                return self._send({"url": unshorten(u), "feedel": feedel_opens(u)})
            if p.path == "/api/update":
                if q.get("check", [""])[0]:
                    return self._send(update_check())
                return self._send(update_status())
            if p.path == "/api/ig/status":
                return self._send(ig_status())
            if p.path == "/api/messages":
                if q.get("refresh", [""])[0]:
                    msg_refresh_async()
                return self._send(msg_payload(bool(q.get("archived", [""])[0])))
            if p.path == "/mcp":
                return self._send({"error": "this endpoint speaks MCP over POST (Streamable HTTP); add it to Claude Code with: claude mcp add --transport http feedel " + f"http://127.0.0.1:{RUN_PORT}/mcp"}, code=405)
            if p.path == "/api/zulip/streams":
                return self._send(zulip_status())
            if p.path == "/api/zulip/topics":
                stream = q.get("stream", [""])[0] or None
                st = zulip_state(stream) if stream else {}
                return self._send({"topics": topics_with_items(zulip_topics(stream))[:200], "stream": stream, "at": st.get("at"),
                                   "error": st.get("error"), "on": zulip_ready(), "streams": zulip_status()["streams"]})
            if p.path == "/api/zulip/people":
                return self._send(zulip_people())
            if p.path == "/api/zulip/setup_code":
                return self._send({"code": zulip_setup_code(), "realm": zulip_realm_name()})
            if p.path == "/api/tw/thread":
                return self._send(tw_thread(q.get("url", [""])[0]))
            if p.path == "/api/zulip/topic":
                return self._send(zulip_topic_messages(q.get("name", [""])[0], q.get("stream", [""])[0] or None))
            if p.path == "/api/zulip/search":
                return self._send(zulip_search(q.get("stream", [""])[0] or zulip_cfg()[3], q.get("q", [""])[0]))
            if p.path == "/api/zulip/raw":
                return self._send(zulip_raw(q.get("id", ["0"])[0]))
            if p.path == "/api/source":
                return self._send(source_payload(q.get("kind", [""])[0], q.get("id", [""])[0]))
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
            if p.path == "/api/yt/search":
                s = q.get("q", [""])[0].strip()
                return self._send(yt_video_search(s) if s else [])
            if p.path == "/api/arxiv/search":
                g = lambda k: q.get(k, [""])[0]
                return self._send(arxiv_search(g("q"), g("author"), g("from"), g("to"), g("sort") or "submittedDate"))
            if p.path == "/api/inspire/related":
                aid = q.get("id", [""])[0].strip()
                if not re.fullmatch(r"[\w.\-/]+", aid):
                    return self._send({"error": "not an arXiv id"}, code=400)
                return self._send(inspire_related(aid, "citedby" if q.get("rel", [""])[0] == "citedby" else "references"))
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
            if p.path == "/zmedia":  # a file uploaded to the group's Zulip, fetched with your credentials (the browser has none)
                u = q.get("u", [""])[0]
                site, email, key, _ = zulip_cfg()
                if not (site and u.startswith(site + "/")):
                    return self._send("not a file on your Zulip", "text/plain", 403)
                try:
                    hdrs = {"Authorization": "Basic " + base64.b64encode(f"{email}:{key}".encode()).decode(), "User-Agent": "feedel/1.0"}
                    if self.headers.get("Range"):
                        hdrs["Range"] = self.headers["Range"]
                    with urllib.request.urlopen(urllib.request.Request(u, headers=hdrs), timeout=60) as r:
                        body = r.read()
                        self.send_response(r.status)
                        for h in ("Content-Type", "Content-Range", "Accept-Ranges"):
                            if r.headers.get(h):
                                self.send_header(h, r.headers[h])
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "private, max-age=86400")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                except urllib.error.HTTPError as e:
                    return self._send(f"Zulip said {e.code}", "text/plain", e.code)
                except Exception as ex:
                    return self._send(str(ex), "text/plain", 502)
            if p.path == "/img":  # avatars and video thumbnails; only the known image hosts are allowed through
                u = q.get("u", [""])[0]
                host = urlparse(u).netloc
                if u.startswith("https://") and (host.endswith("ggpht.com") or host.endswith("googleusercontent.com") or host.endswith("twimg.com")
                                                or host.endswith("cdninstagram.com") or host.endswith("fbcdn.net")):
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
        if p.path == "/mcp":
            return self.do_mcp()
        try:
            if p.path == "/api/refresh":  # ?kind=tw refreshes that kind only (the refresh button on one tab)
                kind = q.get("kind", [""])[0]
                threading.Thread(target=refresh, args=(True, [kind] if kind in KINDS else None), daemon=True).start()
                return self._send("ok", "text/plain")
            if p.path == "/api/play" and vid:
                mark([vid], "seen", True)
                return self._send(play(vid), "text/plain")
            if p.path == "/api/dl" and vid:
                return self._send(download(vid), "text/plain")
            if p.path in ("/api/play_url", "/api/dl_url"):
                u = str(self._json().get("url", "")).strip()
                if not playable_url(u):
                    return self._send({"error": "only YouTube videos and X posts can be played"}, code=400)
                if p.path == "/api/play_url":
                    mark([u], "seen", True)
                    return self._send(play_url(u), "text/plain")
                return self._send(download_url(u), "text/plain")
            if p.path == "/api/restart":
                self._send("restarting", "text/plain")
                restart_self()
                return
            if p.path == "/api/quit":
                self._send("bye", "text/plain")
                threading.Timer(0.5, WINDOW.get("quit") or (lambda: os._exit(0))).start()
                return
            if p.path == "/api/show":  # a second launch, or the link handler: show the window if there is one
                if not WINDOW.get("show"):
                    return self._send({"shown": False})
                link = str(self._json().get("link") or "").strip() or None
                threading.Thread(target=WINDOW["show"], args=(link,), daemon=True).start()
                return self._send({"shown": True})
            body = self._json()
            if p.path == "/api/mark":
                ids = body.get("ids") or [body.get("id")]
                field = "saved" if body.get("field") == "saved" else "seen"
                mark([i for i in ids if i], field, bool(body.get("on", True)), body.get("item"), body.get("patch"))
                return self._send({"ok": True})
            if p.path == "/api/settings":
                return self._send(update_settings(body))
            if p.path == "/api/inspire":  # citation count and journal for the papers on screen, several ids at once
                ids = [str(i) for i in (body.get("ids") or []) if re.fullmatch(r"[\w.\-/]+", str(i))][:150]
                return self._send(inspire_info(ids))
            if p.path == "/api/zulip/test":
                return self._send(zulip_test())
            if p.path == "/api/zulip/apply_setup":
                return self._send(zulip_apply_setup(str(body.get("code", ""))))
            if p.path == "/api/zulip/invite/link":
                return self._send(zulip_invite_link())
            if p.path == "/api/zulip/invite/email":
                return self._send(zulip_invite_emails(str(body.get("emails", ""))))
            if p.path == "/api/zulip/subscribe":
                return self._send(zulip_subscribe_all())
            if p.path == "/api/zulip/reply":
                return self._send(zulip_reply(str(body.get("topic", "")), str(body.get("content", "")), body.get("stream") or None))
            if p.path == "/api/zulip/post":  # a new topic, from scratch, in one of the shown streams
                return self._send(zulip_post(str(body.get("stream", "")), str(body.get("topic", "")), str(body.get("content", ""))))
            if p.path == "/api/zulip/read":
                return self._send(zulip_mark_read(body.get("ids") or []))
            if p.path == "/api/zulip/react":
                return self._send(zulip_react(body.get("id"), str(body.get("emoji", "")), bool(body.get("on", True)),
                                              str(body.get("code", "")), str(body.get("type", ""))))
            if p.path == "/api/zulip/edit":
                return self._send(zulip_edit(body.get("id"), str(body.get("content", "")), body.get("stream") or None))
            if p.path == "/api/zulip/refresh":
                return self._send({"ok": zulip_fetch_stream(body.get("stream") or None) is not None})
            if p.path == "/api/inbox":
                u = str(body.get("url", "")).strip()
                if not link_kind(u):
                    return self._send({"error": "that does not look like a link"}, code=400)
                return self._send(inbox_take(u, str(body.get("via") or "box")))
            if p.path == "/api/inbox/remove":
                inbox_remove(str(body.get("url", "")))
                return self._send({"ok": True})
            if p.path == "/api/yt/older":  # older videos of one channel (the channel page's "load more")
                cid = str(body.get("id", "")).strip()
                if not re.fullmatch(r"UC[\w-]{22}", cid):
                    return self._send({"error": "not a channel id"}, code=400)
                r = yt_older(cid, int(body.get("pages") or 1))
                save_cache()
                return self._send(r)
            if p.path == "/api/yt/older_all":  # one page of older videos for the most recently active channels (the videos tab's "load more")
                return self._send(yt_older_all(int(body.get("limit") or 24)))
            if p.path == "/api/claude/mcp":
                return self._send(mcp_install(bool(body.get("on", True))))
            if p.path == "/api/claude/briefing":
                return self._send(briefing_update(body))
            if p.path == "/api/claude/briefing/run":
                threading.Thread(target=briefing_run, args=("by hand",), daemon=True).start()
                return self._send({"ok": True, "started": True})
            if p.path == "/api/claude/briefing/task":
                return self._send(briefing_install_task())
            if p.path == "/api/update/download":
                return self._send(update_download())
            if p.path == "/api/update/apply":
                return self._send(update_apply())
            if p.path == "/api/claude":  # "take to claude": open a new conversation with the item's content as the prompt
                prompt = str(body.get("prompt", "")).strip()
                if not prompt:
                    return self._send({"error": "nothing to take"}, code=400)
                url = (CONFIG.get("claude_url") or "https://claude.ai/new?q={q}").replace("{q}", urllib.parse.quote(prompt[:6000], safe=""))
                b = browser_for_the_rest()
                if b:
                    subprocess.Popen([b, url])
                else:
                    import webbrowser
                    webbrowser.open(url)
                return self._send({"ok": True, "browser": os.path.basename(b) if b else "default"})
            if p.path == "/api/messages/send":
                return self._send(msg_send(str(body.get("text", "")), body.get("to") or [], body.get("reply_to"), str(body.get("title", ""))))
            if p.path == "/api/messages/update":
                if body.get("ids"):
                    return self._send(msg_update_many(body["ids"], body.get("read"), body.get("archived")))
                return self._send(msg_update(int(body.get("id")), body.get("read"), body.get("archived"), body.get("body")))
            if p.path == "/api/messages/refresh":
                n = zulip_fetch_dms() + ig_fetch_dms()
                return self._send({"ok": True, "new": n, "error": STATE.get("dm_error") or STATE.get("ig_error")})
            if p.path == "/api/ig/connect":
                return self._send(ig_connect(str(body.get("token", ""))))
            if p.path == "/api/ig/cookies":
                return self._send(ig_import_cookies(str(body.get("browser", ""))))
            if p.path == "/api/ig/disconnect":
                return self._send(ig_disconnect())
            if p.path == "/api/share":
                if not isinstance(body.get("item"), dict):
                    return self._send({"error": "nothing to share"}, code=400)
                return self._send(zulip_share(body["item"], str(body.get("note", "")), str(body.get("topic", "")), str(body.get("stream", "")),
                                              bool(body.get("recommend")), [str(x) for x in (body.get("to") or [])]))
            if p.path == "/api/recs/thanks":
                zulip_react(body.get("msg_id"), "+1")
                threading.Thread(target=zulip_fetch_stream, daemon=True).start()
                return self._send({"ok": True})
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
            if p.path == "/api/unfollowed/forget":
                unfollowed_remove(body.get("kind", ""), body.get("ref", ""))
                return self._send({"ok": True})
            if p.path == "/api/subs/update":
                return self._send({"ok": update_sub(body.get("kind"), body.get("id"), body.get("patch") or {})})
            if p.path == "/api/import":
                entries, bad = parse_import(body.get("text", ""), body.get("filename", ""), body.get("kind", "yt"))
                if not entries:
                    return self._send({"error": "nothing recognisable in that", "bad": bad[:20]}, code=400)
                r = add_entries(entries, respect_unfollowed=True)
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
                threading.Thread(target=refresh, args=(True,), daemon=True).start()
                return self._send({"ok": True})
            if p.path == "/api/import/browser":
                try:
                    chans = import_from_browser(body.get("browser", "firefox"))
                except Exception as ex:
                    raise RuntimeError(browser_sync_error(body.get("browser", "firefox"), ex)[0])
                r = add_entries([{"kind": "yt", "ref": c["id"], "name": c["name"], "avatar": c["avatar"]} for c in chans], respect_unfollowed=True)
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

USAGE = """usage: feedel.py [--port N] [--no-open] [--window | --browser] [--export] [--import FILE]
  --port N        listen on another port (default 8765)
  --no-open       do not open the page in a browser
  --window        show the page in its own window with a tray icon (needs pywebview, pystray and pillow; the browser otherwise)
  --browser       open the browser this once even when the "own window" setting is on
  --export        print everything in feedel.db (settings, follows, seen/saved, cache, items) as JSON on stdout and exit
  --import FILE   merge a JSON export, or an old config.json / state.json / cache.json, into feedel.db and exit
                  (nothing is removed; stop a running feedel first)
  --open URL                 show a video / post / paper link in feedel (anything else opens in your browser)
  --register-handler         Windows: make feedel selectable as the default web browser (links from any app go through --open)
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
        log(f"imported into feedel.db: {n['follows']} new follows, {n['seen']} seen, {n['saved']} saved, {n['items']} items")
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
    if FROZEN and os.path.exists(pending_path()):  # a build fetched last time: swap it in and start that one instead
        try:
            if swap_pending_exe():
                env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
                subprocess.Popen([sys.executable] + sys.argv[1:], env=env, close_fds=True)
                os._exit(0)
        except Exception as ex:
            log("could not apply the downloaded update:", ex)
    cleanup_old_exe()
    if FIRST_RUN:
        log("Created feedel.db — open the page and use the 'following' tab to import your subscriptions.")
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
        log(f"port {port} is busy; feedel is probably already running — opening the page")
        if "--no-open" not in sys.argv and not show_running_window(port):  # a running window comes back; else a browser tab
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        os._exit(0)  # not sys.exit: a thread already started would keep this copy alive, holding the executable file
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=clipboard_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=briefing_loop, daemon=True).start()
    remember_default_browser()
    log(f"feedel on http://localhost:{port}  (Ctrl+C to stop)")
    if STATE.get("queued_link"):
        threading.Thread(target=_safe_take, args=(STATE.pop("queued_link"), "link"), daemon=True).start()
        sys.argv.append("--inbox")
    in_window = window_wanted() and window_available()
    if "--no-open" not in sys.argv and not in_window:
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}" + ("/#inbox" if "--inbox" in sys.argv else ""))
        except Exception:
            pass
    try:
        if in_window:  # the GUI loop needs the main thread; the server moves to another one
            WINDOW["server"] = srv  # so a restart can free the port before this process is gone
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                run_window(port, "#inbox" if "--inbox" in sys.argv else "")
            except Exception as ex:  # e.g. no WebView2 runtime: the server is up, so show the page the usual way
                log(f"the window could not open ({ex}) — opening the browser instead")
                import webbrowser
                webbrowser.open(f"http://localhost:{port}" + ("/#inbox" if "--inbox" in sys.argv else ""))
                threading.Event().wait()
        else:
            srv.serve_forever()
    except KeyboardInterrupt:
        pass
