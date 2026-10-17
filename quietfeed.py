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

import csv, hashlib, html, io, json, os, re, shutil, subprocess, threading, time
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
WEB = os.path.join(getattr(sys, "_MEIPASS", HERE), "web")
CONFIG_PATH = os.path.join(HERE, "config.json")
CACHE_PATH = os.path.join(HERE, "cache.json")
STATE_PATH = os.path.join(HERE, "state.json")
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
    "ui": {"hide_thumbnails": False, "calm_titles": True, "hide_seen": False},
    # "browser": name of a browser signed in to YouTube whose subscriptions are re-read on every refresh (needs yt-dlp)
    "sync": {"browser": ""},
    "player": ["mpv", "--ytdl-format=bestvideo[height<=1080]+bestaudio/best", "{url}"],
    "download_dir": os.path.join(HERE, "downloads"),
    "refresh_minutes": 30,
    "max_items": 400,
    "max_per_channel": 8,
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
        print(line, flush=True)

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

# ---------------------------------------------------------------- state

CONFIG = load_json(CONFIG_PATH, None)
FIRST_RUN = CONFIG is None
if FIRST_RUN:
    CONFIG = json.loads(json.dumps(DEFAULT_CONFIG))
for k, v in DEFAULT_CONFIG.items():  # fill in keys added by newer versions
    if k not in CONFIG:
        CONFIG[k] = json.loads(json.dumps(v))
    elif isinstance(v, dict):
        for kk, vv in v.items():
            CONFIG[k].setdefault(kk, json.loads(json.dumps(vv)))

CACHE = load_json(CACHE_PATH, {})
for k in ("durations", "shorts"):
    CACHE.setdefault(k, {})
SEEN_STATE = load_json(STATE_PATH, {})
for k in ("seen", "saved"):
    SEEN_STATE.setdefault(k, {})

LOCK = threading.RLock()
STATE = {"items": [], "errors": [], "last_refresh": None, "refreshing": False}
AVATARS = {}  # in-memory bytes cache

def save_config():
    with LOCK:
        save_json(CONFIG_PATH, CONFIG)

def save_state():
    with LOCK:
        save_json(STATE_PATH, SEEN_STATE)

def save_cache():
    with LOCK:
        for k in ("durations", "shorts"):
            d = CACHE[k]
            if len(d) > 20000:  # keep the newest entries (dicts keep insertion order)
                CACHE[k] = dict(list(d.items())[-10000:])
        save_json(CACHE_PATH, CACHE)

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
    CONFIG["youtube"]["channels"], CONFIG["twitter"]["users"] = yt, tw
    CONFIG["rss"]["feeds"], CONFIG["arxiv"]["queries"] = rss, ax
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
        with LOCK:
            for v, s in durs.items():
                CACHE["shorts"][v] = False
                if s is not None:
                    CACHE["durations"][v] = s
            for v in shorts:
                CACHE["shorts"][v] = True
        for v in need:  # neither tab listed it (live stream, premiere, old): ask YouTube directly
            if v not in CACHE["shorts"]:
                code, _ = head_no_redirect(f"https://www.youtube.com/shorts/{v}")
                if code:
                    CACHE["shorts"][v] = (code == 200)
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
ARXIV_LAST = [0.0]
ARXIV_LOCK = threading.Lock()

def arxiv_api(search_query, max_results=50, sort="submittedDate"):
    """One arXiv API call, politely spaced (they ask for 3 s between requests)."""
    with ARXIV_LOCK:
        wait = ARXIV_LAST[0] + 3 - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            data = fetch("https://export.arxiv.org/api/query?" + urlencode(
                {"search_query": search_query, "sortBy": sort, "sortOrder": "descending", "max_results": max_results}), timeout=40)
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
        items.sort(key=lambda x: -x["ts"])
        with LOCK:
            STATE["items"], STATE["errors"] = items, errors
            STATE["last_refresh"] = time.time()
        save_cache()
        save_config()
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
    return {"hide_shorts": CONFIG["youtube"].get("hide_shorts", True), "min_minutes": CONFIG["youtube"].get("min_minutes", 0),
            "max_per_channel": CONFIG.get("max_per_channel", 0), "block_words": CONFIG["filters"].get("block_words", []),
            **CONFIG["ui"]}

def items_payload():
    with LOCK:
        tags = sorted({t for _, s in all_subs() for t in s.get("tags", [])})
        return {"items": visible_items(), "errors": STATE["errors"], "last_refresh": STATE["last_refresh"],
                "refreshing": STATE["refreshing"], "seen": list(SEEN_STATE["seen"]), "saved": list(SEEN_STATE["saved"]),
                "saved_items": [v for v in SEEN_STATE["saved"].values() if isinstance(v, dict)],
                "tags": tags, "counts": {k: len(sub_list(k)) for k in KINDS},
                "arxiv_authors": [s["name"] for s in sub_list("arxiv") if s.get("qtype") == "author"],
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
        for k in ("hide_thumbnails", "calm_titles", "hide_seen"):
            if k in patch:
                CONFIG["ui"][k] = bool(patch[k])
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
        if field == "seen" and len(d) > 5000:
            SEEN_STATE["seen"] = dict(sorted(d.items(), key=lambda kv: kv[1])[-4000:])
        save_state()

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
    if "://" in tok or re.match(r"^(www\.|m\.|mobile\.)?(youtube\.com|youtu\.be|x\.com|twitter\.com|xcancel\.com|nitter\.[\w.]+|arxiv\.org)/", tok, re.I) \
            or (default_kind == "rss" and re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", tok, re.I)):
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
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

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

normalize_subscriptions()

if __name__ == "__main__":
    if FIRST_RUN:
        log("Wrote config.json — open the page and use the 'following' tab to import your subscriptions.")
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
    log(f"quietfeed on http://localhost:{port}  (Ctrl+C to stop)")
    if "--no-open" not in sys.argv:
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
