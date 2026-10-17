# quietfeed

A local, distraction-free reader for the YouTube channels, X accounts, Substack newsletters and arXiv categories you actually care about. One chronological list, no recommendations, no Shorts, no autoplay, no counters. Lives at `http://localhost:8765`.

**How it gets around Cold Turkey without touching it:** Cold Turkey's browser extension blocks `youtube.com` / `x.com` *in the browser*. Nothing here ever asks the browser to load those domains. The program fetches the feeds, searches, thumbnails and avatars itself, and videos play in **mpv** (a desktop player), not in a tab. Your block stays on, and the places you'd get sucked in (home feed, sidebar, Shorts, the X timeline, comments) stay unreachable. Substack posts and arXiv papers open in the browser normally, since those sites are the calm kind.

## Run it

Either double-click `quietfeed.exe` (see *Building the executable*), or:

```
winget install -e --id shinchiro.mpv   # installs to C:\Program Files\MPV Player
pip install yt-dlp                     # streaming in mpv, downloads, browser-session sync
python quietfeed.py
```

First run creates `quietfeed.db` next to the program and opens the page. Standard library only. The program finds mpv and yt-dlp on its own even when they are not on your PATH, and tells mpv where yt-dlp is. `--no-open` skips opening the browser, `--port N` changes the port. Starting it twice just re-opens the page. *Settings → quit quietfeed* stops it.

## What is in the feed

- **videos** from YouTube channels, with Shorts dropped and a minimum length, played in mpv.
- **tweets** from X accounts, text only, through a Nitter mirror (see the caveat at the end).
- **posts** from Substack and any RSS/Atom feed; open in the browser.
- **papers** from arXiv: categories (`math.PR`, `cs.LG`, or `math` for a whole archive), authors, or keyword watches. In the *all* view, papers fold into one line per day ("12 new papers") so they never drown the rest; the *papers* chip opens them properly.

The papers view has its own toolbar: **latest** shows the newest listing you have (arXiv dates papers by submission and lists them a day or two later, so this is keyed to the newest batch, not the clock), **3 days / week / all** count back from now. The search box asks arXiv directly: keywords (quote a phrase), author, a date range, sorted by newest, last updated or relevance. Click an author's name on any paper to follow them or list their papers; followed authors are highlighted. Every paper has abstract, abs and pdf links, copy link, and save.

## Following

The *following* tab. Type a channel name and it searches YouTube channels; paste a `youtube.com` or `x.com` link, an `@handle` or a channel id and it is added directly. For Substack / RSS paste the site or feed link. For arXiv type a category, `au:Name Surname`, or keywords.

Importing: Google Takeout `subscriptions.csv` (Takeout → deselect all → *YouTube and YouTube Music* → *all YouTube data included* → keep only *subscriptions*), OPML files from any reader, the X archive's `data/following.js`, or a pasted list with one entry per line. A line can be a link, a handle, a channel id, or a channel **name as it appears on YouTube** (`Andrew Steele`), which is matched through channel search; the import report says what each name matched and flags it when several small channels share the name. Imports never unfollow anything.

Per source: **tags** (become filter chips), **mute**, a per-channel **min** length, and a **why** note.

## Keeping it in sync with your accounts

The app never signs in as you and never has your password. It can *read* what you follow, re-reads it at the start of every refresh, and follows anything new. It only ever adds.

### YouTube account sync (official API, read-only)

A one-time, ten-minute setup, because Google requires each app that reads your data to have its own credentials:

1. Go to [console.cloud.google.com](https://console.cloud.google.com/), create a project (any name).
2. *APIs & Services → Library*: enable **YouTube Data API v3**.
3. *APIs & Services → OAuth consent screen*: external, fill in the app name and your email, add the scope `.../auth/youtube.readonly`, add yourself as a test user. Then **publish** the app (*Publishing status → In production*; no verification needed for personal use, you just click through an "unverified app" warning once). Leaving it in *Testing* makes Google expire the connection every seven days.
4. *APIs & Services → Credentials → Create credentials → OAuth client ID*, type **Desktop app**. Download the JSON and save it as `client_secret.json` next to the program.
5. In the page, *following → keep in sync → connect Google account*. The consent screen opens on accounts.google.com (not youtube.com, so a browser block does not interfere) and sends you back to localhost.

The token is stored in `google_token.json`; delete it or click *disconnect* to revoke.

### Browser session sync

If a browser on this machine is signed in to YouTube, pick it under *Browser session* and the program re-reads that browser's subscriptions list on every refresh using the cookies the browser already stores. The browser itself never opens youtube.com. Firefox works best; recent Chrome builds encrypt cookies in a way yt-dlp sometimes cannot read.

### X

X's API no longer offers free read access and scraping a logged-in session risks the account, so there is no live sync. The complete route is the archive: on x.com, *Settings → Your account → Download an archive of your data*, then drop `data/following.js` into the import box. It only holds numeric ids; they are resolved to handles through x.com's public intent redirect (no login), and whatever does not resolve on the spot is queued and retried every refresh.

## Links from elsewhere

A link someone sends you on WhatsApp, in mail or anywhere else can be opened in quietfeed instead of a browser where the site is blocked. Three ways in, from easiest to most automatic:

- **+ link** above the feed: paste any YouTube, X, arXiv or article link. quietfeed fetches it itself and shows it as an item in the **inbox**, with play, save and share like everything else, plus a *follow* button for the channel, account, author or newsletter it came from. Channel and profile links become a follow suggestion directly.
- **Clipboard watcher** (settings): with it on, every link you *copy* that quietfeed can show lands in the inbox by itself. Copy a link in WhatsApp, switch to quietfeed, it is there. A second checkbox widens it from YouTube, X and arXiv links to every link. Only the clipboard text is read, only links are looked at, and nothing leaves the machine except the fetch of that link's own page.
- **Make quietfeed the link handler** (Windows): run `quietfeed --register-handler` (or `python quietfeed.py --register-handler`), then pick *quietfeed* under *Settings → Apps → Default apps → Web browser*. From then on a click on any link in any app goes through quietfeed: videos, posts and papers open in the inbox, everything else is handed to your normal browser: whatever your default was when you registered (quietfeed remembers it, since afterwards the default is quietfeed itself), shown and changeable under the gear as *send the links it cannot show to*. `--unregister-handler` undoes it. `quietfeed --open URL` is the same thing from a terminal.

Tweets are read through X's public embed endpoint, which needs no login and works for public posts.

## Sharing to Zulip

The conversation lives in Zulip, not in quietfeed (see [ROADMAP.md](ROADMAP.md)). **share** on any item opens a note box and posts the item and your note to a Zulip stream, as you, with one topic per item, so the group can discuss it there with threads, search and LaTeX.

Setup, once, under the gear: your Zulip site (`yourgroup.zulipchat.com`), your e-mail, your personal API key (in Zulip: *Settings → Account & privacy → API key*), and the stream to post to (default `quietfeed`; create it in Zulip first). *Test connection* confirms it. The key is stored in your `quietfeed.db` and never shown back on the page.

## Library

*Save* on anything puts it in the library, where it stays after it has left the feed. Each saved item takes **tags** and a **note**; the library filters by kind and tag and searches titles, authors, abstracts and notes. Seen and saved state live in `quietfeed.db`.

## Settings (the gear)

Hide thumbnails, calm titles (ALL CAPS → sentence case, `!!!` → `!`), hide seen, hide Shorts, minimum video length, a per-source cap so one prolific channel cannot drown the rest, and block words (`/regex/` allowed). The theme button cycles auto / light / dark.

## Building the executable

```
pip install pyinstaller yt-dlp
python -m PyInstaller --noconfirm --clean --onefile --windowed --name quietfeed --add-data "web;web" --collect-submodules yt_dlp quietfeed.py
```

`dist\quietfeed.exe` is self-contained: the page, yt-dlp and everything else are inside. It writes `quietfeed.db` and `quietfeed.log` next to itself, so put it in its own folder. It has no console window; *Settings → quit quietfeed* stops it. If no `yt-dlp.exe` is installed, mpv streams through the exe itself, which answers as yt-dlp when asked. Windows SmartScreen will warn once about an unsigned program; some antivirus products dislike single-file PyInstaller builds, in which case drop `--onefile` and ship the `dist\quietfeed` folder instead.

## Files

- `quietfeed.db` — one SQLite file with everything: settings, what you follow, seen and saved items with your tags and notes, the items themselves (kept 90 days after they leave their feed; saved ones forever) and the duration / Shorts cache. Back it up by copying it. `python quietfeed.py --export > backup.json` dumps it all as JSON; `python quietfeed.py --import backup.json` merges a dump (or an old `config.json` / `state.json` / `cache.json`) back in, adding and never removing. Open it with any SQLite tool to look around.
- `config.json`, `state.json`, `cache.json` — the files older versions used. They are migrated into `quietfeed.db` the first time this version runs and renamed `*.json.migrated`.
- `cache/avatars/` — avatar images. Safe to delete.
- `downloads/` — what the **download** button saves.

## Twitter caveat

X has no RSS and shut down most Nitter mirrors in August 2026. The program tries each instance in `nitter_instances` in order and uses the first that answers. When none answer you see a collapsed "N sources could not be fetched" line at the top of the page, and everything else keeps working.

## Contributing

This is a project between friends. [ROADMAP.md](ROADMAP.md) says where it is going (a small shared server: share videos, posts and papers with each other, discuss them, host it on a Mac mini) and [CONTRIBUTING.md](CONTRIBUTING.md) says how to run it from source and where things are. Issues are the place to argue.
