# Feedel

A local, distraction-free reader for the YouTube channels, X accounts, Substack newsletters and arXiv categories you actually care about. One chronological list, no recommendations, no Shorts, no autoplay, no counters. Lives at `http://localhost:8765`.

**How it gets around Cold Turkey without touching it:** Cold Turkey's browser extension blocks `youtube.com` / `x.com` *in the browser*. Nothing here ever asks the browser to load those domains. The program fetches the feeds, searches, thumbnails and avatars itself, and videos play in **mpv** (a desktop player), not in a tab. Your block stays on, and the places you'd get sucked in (home feed, sidebar, Shorts, the X timeline, comments) stay unreachable. Substack posts and arXiv papers open in the browser normally, since those sites are the calm kind.

## Run it

Either double-click `feedel.exe` (see *Building the executable*), or:

```
winget install -e --id shinchiro.mpv   # installs to C:\Program Files\MPV Player
pip install yt-dlp                     # streaming in mpv, downloads, browser-session sync
python feedel.py
```

First run creates `feedel.db` next to the program and opens the page. Standard library only. The program finds mpv and yt-dlp on its own even when they are not on your PATH, and tells mpv where yt-dlp is. `--no-open` skips opening the browser, `--port N` changes the port. Starting it twice just re-opens the page. *Settings → quit feedel* stops it.

## What is in the feed

- **videos** from YouTube channels, with a minimum length, played in mpv. Shorts are hidden from the feed by default (the fight is against scrolling, not against a video you chose), and a Short you open yourself through the inbox still shows.
- **tweets** from X accounts through a Nitter mirror (see the caveat at the end). A post with a video shows it with play and download: you chose to follow that person, so their videos are pre-approved. Media comes from X's public syndication endpoint, no login.
- **posts** from Substack and any RSS/Atom feed; open in the browser.
- **papers** from arXiv: categories (`math.PR`, `cs.LG`, or `math` for a whole archive), authors, or keyword watches. A category is read from arXiv's own daily announcement (`rss.arxiv.org`, with the listing page as the fallback on weekends and holidays), so each paper carries the day it was announced and whether it was **new**, a **cross-list** from another category or a **replacement** of an older paper, exactly as the listing says. Authors and keyword watches still come through the API. In the *all* view, papers fold into one line per day ("12 new papers") so they never drown the rest; the *papers* chip opens them properly.

Everything is refetched every 30 minutes (`refresh_minutes`) and when you press **refresh**. Sources are fetched sixteen at a time and each one's items land in the feed the moment it answers, so after a refresh starts the feed fills within seconds instead of waiting for the slowest source; while the server says *refreshing…* the page re-reads the feed every few seconds. YouTube channels that have gone quiet are polled less often: no video in the last 7 days, at most every 2 hours; none in 30 days, at most every 6 hours. **refresh** ignores that and fetches everything. The log shows one line per refresh with how many sources were fetched, how many quiet channels were skipped and how long each kind took.

The papers view has its own toolbar. **today's listing** is the most recent announcement day you have (Friday's on a weekend), **3 days / week / all** count back from now, and the chips next to them pick what arXiv announced: new submissions and cross-lists are shown, replacements are off until you ask for them. A paper you already saw as new that arXiv later re-announces as a replacement keeps its place and date, with a quiet "replaced" note. The first time a category is followed this way, the previous week of listings is read too, so the week chip has something to show. The search box asks arXiv directly: keywords (quote a phrase), author, a date range, sorted by newest, last updated or relevance. Click an author's name on any paper to follow them or list their papers; followed authors are highlighted. A keyword watch also highlights its words inside titles and abstracts, and the papers that match come first within their day, with the matched words in muted text; the order stays chronological otherwise. Every paper has abstract, abs and pdf links, copy link, and save, plus **cited by** and **references** from [INSPIRE-HEP](https://inspirehep.net): a flat list with title, authors, year, journal and citation count, each with "open on arXiv" when it has an arXiv id. When INSPIRE knows the paper itself, its journal and citation count sit in the by-line (cached a day). INSPIRE covers high-energy physics and its neighbours; a paper outside that simply gets no by-line.

Keyboard, on the feed, the papers, the library and a source's page: `j` / `k` move a highlight between items, `o` (or Enter) opens the highlighted one (a paper's abstract, play for a video, open for a post), `s` saves, `m` marks seen, `a` / `p` open a paper's abs page or pdf, `c` / `r` its citations or references, `/` goes to the search box, Escape clears, and `?` shows the list.

## Search

The box in the feed bar filters what you follow as you type: titles, authors, descriptions, tweets and paper abstracts, within the chip you are on. If nothing you follow matches, one more click (or Enter) asks YouTube for that phrase and shows a flat list of videos with play, save and the channel's page, nothing recommended around it. Searching is a thing you do on purpose; the feed never suggests.

## Following

The *following* tab. Type a channel name and it searches YouTube channels; paste a `youtube.com` or `x.com` link, an `@handle` or a channel id and it is added directly. For Substack / RSS paste the site or feed link. For arXiv type a category, `au:Name Surname`, or keywords.

Importing: Google Takeout `subscriptions.csv` (Takeout → deselect all → *YouTube and YouTube Music* → *all YouTube data included* → keep only *subscriptions*), OPML files from any reader, the X archive's `data/following.js`, or a pasted list with one entry per line. A line can be a link, a handle, a channel id, or a channel **name as it appears on YouTube** (`Andrew Steele`), which is matched through channel search; the import report says what each name matched and flags it when several small channels share the name. Imports never unfollow anything. **An unfollow is remembered**: syncs and imports leave previously unfollowed sources alone and say so in their report; the *previously unfollowed* list at the bottom of the following tab lets you follow one again or forget it so a later import may add it. Following something by hand always works and clears the memory.

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

If a browser on this machine is signed in to YouTube, pick it under *Browser session* and the program re-reads that browser's subscriptions list on every refresh using the cookies the browser already stores. The browser itself never opens youtube.com. Firefox works best. Chromium browsers (Chrome, Vivaldi, Brave, Edge, Opera) keep their cookie database locked while they are open, so the sync fails with *keeps its cookie database locked while it is open*: close that browser during a sync (then press *sync now*), or use the Google account sync instead. After that error the program waits an hour before trying on its own again; *sync now* and *refresh* try right away. Recent Chrome builds also encrypt cookies in a way yt-dlp sometimes cannot read.

### X

X's API no longer offers free read access and scraping a logged-in session risks the account, so there is no live sync. The complete route is the archive: on x.com, *Settings → Your account → Download an archive of your data*, then drop `data/following.js` into the import box. It only holds numeric ids; they are resolved to handles through x.com's public intent redirect (no login), and whatever does not resolve on the spot is queued and retried every refresh.

## Links from elsewhere

A link someone sends you on WhatsApp, in mail or anywhere else can be opened in feedel instead of a browser where the site is blocked. Three ways in, from easiest to most automatic:

- **+ link** above the feed: paste any YouTube, X, Instagram reel, arXiv or article link. feedel fetches it itself and shows it as an item in the **inbox**, with play, save and share like everything else, plus a *follow* button for the channel, account, author or newsletter it came from. Channel and profile links become a follow suggestion directly.
- **Clipboard watcher** (settings): with it on, every link you *copy* that feedel can show lands in the inbox by itself. Copy a link in WhatsApp, switch to feedel, it is there. A second checkbox widens it from YouTube, X and arXiv links to every link. Only the clipboard text is read, only links are looked at, and nothing leaves the machine except the fetch of that link's own page.
- **Make feedel the link handler** (Windows): run `feedel --register-handler` (or `python feedel.py --register-handler`), then pick *feedel* under *Settings → Apps → Default apps → Web browser*. From then on a click on any link in any app goes through feedel: YouTube, X, arXiv, Instagram reels, Substack and posts on the site of any newsletter you follow open in the inbox, everything else is handed to your normal browser: whatever your default was when you registered (feedel remembers it, since afterwards the default is feedel itself), shown and changeable under the gear as *send the links it cannot show to*. `--unregister-handler` undoes it. `feedel --open URL` is the same thing from a terminal.

Instagram reels and video posts someone sends you work the same way: feedel asks yt-dlp about the link (public ones need no login) and shows the reel with its caption, thumbnail and a **play** button that opens it in your player, never on Instagram. A private account's reel, or a photo-only post, is refused with a note saying so.

Tweets are read through X's public syndication endpoint, which needs no login and works for public posts, videos included. A reply shows the post it answers right above it, a quote-post shows the quoted post, and a post continuing the author's own thread says so; without that context half of X reads like half a sentence. Every post has a **thread** action: it shows what the post replies to, the author's own continuation numbered 2/, 3/…, and other people's replies folded away, read from a Nitter conversation page (parents only, through the syndication endpoint, when no instance answers). Videos inside a thread are playable too, photos are shown, and every post the box shows, as well as the post a reply answers or a quote-post quotes, has an *open* / *see it* action that walks into that post's own thread, with *back* to return: reposts, replies and quotes can be followed without leaving feedel.

## Source pages

Click any author's name in the feed, the inbox or the following list and you land on that source's page: avatar, handle, the **follow** or **unfollow** button, **mute**, and its tags, minimum length and "why" note, followed by its recent uploads or posts. A source you do not follow yet (a channel from an inbox link, an account a friend mentioned) is fetched on the spot so you can look before following.

## Discussions, through Zulip

With Zulip set up (below), feedel reads your group's streams back too, and the **talk** tab is where the group uses Zulip from inside feedel: enough to read and discuss, not a second chat client.

- **Streams.** Every stream shares go to (the default `#feedel` and any per-kind ones) plus any others you list in settings, as text chips at the top of the tab. Each lists its topics newest first: who is in it, how many messages, when, and the item it is about when feedel can tell (the video, post or paper is shown right there). A topic with messages you have not read says so in muted text (`3 new`), using Zulip's own read flags, so opening the thread here marks it read in Zulip too and vice versa. There is no counter anywhere else, nothing in the nav.
- **Threads.** *open discussion* unfolds the topic in place, LaTeX rendered, oldest first, with a reply box that posts as you. Each message carries its reactions (click one to add yours or take it back, `+` offers a few more) and, on your own messages, an **edit** link that opens the markdown source. The same thread appears under any feed, inbox or library item that a topic links to, where a quiet line says who is talking about it.
- **New topic.** *+ topic* at the top of the tab starts a topic from scratch in the selected stream: a name and a first message, markdown and `$LaTeX$`.
- **Search.** The box asks Zulip's own search within the selected stream and shows the matches grouped by topic, Zulip's highlighting kept, each openable in place.

Everything beyond that (people, private messages, stream settings, long history) is *in Zulip ↗*, one click away on every topic and thread.

## Recommendations, from friends only

The only recommender in feedel is a person who chose you. In the share box, *recommend to someone instead* lets you pick friends (or everyone) and write why; it posts to the recommendations stream in Zulip as "Nahuel recommends this to @Juan", so Juan is notified the Zulip way too. On the other side, everything recommended to you, by name or to everyone, collects under a **from friends** chip in your feed: the item itself with play, save and all the usual actions, who recommended it and their note, a *thanks* that reacts on their message, and the discussion one click away. A friend's recommendation of something already in your feed also shows as a line under that item. Nothing else is ever recommended.

## Friends: getting people into the group

The **friends** tab is the whole onboarding, for both sides.

- **Organizer**: once your feedel is connected to the group's Zulip, the tab shows who is there, lets you create a reusable invitation link (or paste one from Zulip), send e-mail invitations through Zulip to specific addresses, and copy a **setup code**: one pasteable string carrying the site, the streams, the invitation link and your contact, with no keys in it.
- **Friend**: paste the setup code, and three steps appear: open the invitation and create the Zulip account (or ask the organizer for access when the code has no link), open the Zulip account settings to copy the API key, paste it with the e-mail used. Connecting also subscribes them to the group's streams. From then on their feed shows the group's discussions and recommendations.
- **No group yet?** Create a free Zulip organization at zulip.com/new, make the streams, connect under settings, and the tab starts handing out codes.

## Sharing to Zulip

The conversation lives in Zulip, not in feedel (see [ROADMAP.md](ROADMAP.md)). Every item also has **copy link** for sending it to a friend any other way. **share** opens a note box and posts the item and your note to a Zulip stream, as you, with one topic per item, so the group can discuss it there with threads, search and LaTeX.

Setup, once, under the gear: your Zulip site (`yourgroup.zulipchat.com`), your e-mail, your personal API key (in Zulip: *Settings → Account & privacy → API key*), and the stream to post to (default `feedel`; create it in Zulip first). *Test connection* confirms it. Below that, each kind can go to its own stream, videos to `#vids`, papers to `#papers` and so on; the share box shows the target and lets you pick another. feedel reads every stream named there, so discussions are found wherever they live, and the **talk** tab can show further streams you list, comma-separated. The key is stored in your `feedel.db` and never shown back on the page.

## Take to Claude

Papers, posts on X and blog posts have a **take to claude** action: it opens a new conversation on claude.ai with the item as the opening prompt (title, authors, abstract and link for a paper; the text with the post it replies to or quotes for X; title, summary and link for a post), asking for a short map of it first and then a discussion. The conversation opens in your normal browser; the prompt is copied to the clipboard as well, in case the box comes up empty. `claude_url` in the settings table changes where it goes (`{q}` is the encoded prompt).

## Library

*Save* on anything puts it in the library, where it stays after it has left the feed. Each saved item takes **tags** and a **note**; the library filters by kind and tag and searches titles, authors, abstracts and notes. Seen and saved state live in `feedel.db`.

## Messages: notes from Claude, direct messages from friends

The **messages** tab is a small box with three kinds of mail:

- **Direct messages** on the group's Zulip, both ways. They are pulled on every refresh (and when you open the tab); *reply* answers as a Zulip direct message, *write to a friend* starts one. Nothing else of Zulip is read here: streams stay in **talk**.
- **Instagram direct messages**, through Meta's official *Instagram API with Instagram login*. It needs a professional (Creator) account, the "allow access to messages" switch in the Instagram app, and a token generated in a Meta app of your own left in development mode (no review for your own account); the gear has the five steps. feedel pulls the conversations on every refresh and when you open the tab, shows the last 20 messages of each (all Meta hands over) with shared media as links, and replies through the same API, which Meta allows within 24 hours of the other person's last message. The token lasts 60 days and is refreshed while it is in use. Nothing here touches instagram.com itself.
- **Notes from Claude.** feedel is an [MCP](https://modelcontextprotocol.io) server at `http://127.0.0.1:8765/mcp` (the port you run it on). The gear's *Claude Code* block has a **connect** button that registers it (through the `claude` program, or by editing `~/.claude.json` the same way); by hand it is

  ```bash
  claude mcp add --transport http feedel http://127.0.0.1:8765/mcp
  ```

  Then Claude can read your feed, library, what you follow and your friends' discussions, search arXiv through feedel, put a link in your inbox and leave you a note (`send_message`). Notes are Markdown; `- [ ] task` lines become checkboxes you can tick, and the links a note mentions show as cards with a *→ inbox* button so a video or post can be played without opening YouTube or X. A reply to a note stays in the box, where Claude reads it the next time it looks (`messages`). Claude never marks anything seen or saved unless you ask.

  The **daily briefing** is configured under the gear too: on/off, the time, every day or weekdays, your name, the language, what you work on (papers and posts are recommended only from that area) and anything else to keep in mind. feedel runs it itself at that time, headless, through Claude Code (`claude -p` with only feedel's tools allowed; Claude Code must be logged in once with `claude login`), and shows the last run's result. *See the prompt* shows exactly what Claude gets; *run now* tries it; *install it as a Claude Code scheduled task* writes the same prompt where Claude Code's own *Scheduled* list reads it, for those who prefer that scheduler (its time is set there). The repo also ships the skill `.claude/skills/feedel-briefing` for running it by hand with `/feedel-briefing`.

  The endpoint only answers requests from this machine (it listens on 127.0.0.1 and refuses other origins). Opening `/mcp` in a browser explains itself.

## Settings (the gear)

Hide thumbnails, calm titles (ALL CAPS → sentence case, `!!!` → `!`), hide seen, hide Shorts from the feed, minimum video length, a per-source cap so one prolific channel cannot drown the rest, block words (`/regex/` allowed), and whether feedel opens in its own window. The theme button cycles auto / light / dark.

## Its own window

`python feedel.py --window` (or the exe with `--window`, or the setting **open in its own window** under the gear, which applies from the next start) shows the page in a native window titled feedel instead of a browser tab: WebView2 (Edge) on Windows, WebKit on macOS. The server runs in the same process as always. Closing the window hides it to a tray icon whose menu is *open*, *refresh* and *quit*; quitting from there, or *Settings → quit feedel*, stops the server. The window remembers its size and position. Launching feedel again while it runs brings the window back, and the link handler (`--open URL`) shows the link in it.

It needs three optional packages, imported only in window mode, so the server itself stays standard-library only:

```
pip install pywebview pystray pillow
```

Without them `--window` falls back to the browser exactly as before and writes one line to the log saying what to install. `--browser` opens the browser this once when the setting is on; `--no-open` opens nothing.

## Updating itself

Every push to main is built by GitHub and published as the rolling **latest** release; tags starting with `v` get releases of their own. A running feedel asks GitHub once soon after it starts and then every six hours; when main has moved on, the executable fetches the new build next to itself (`feedel.new.exe`) and swaps it in at the next start, or right away from the notice at the top of the page. A copy run from a git checkout offers `git pull` and restart instead. The gear shows which commit this copy is and which one GitHub has, with *check now*; *fetch new builds by itself* turns the automatic download off. While the repository is private the check needs a GitHub token (a fine-grained one with *Contents: read* on the repository), pasted under the gear; a public repository needs none.

## Building the executable

```
pip install pyinstaller yt-dlp pywebview pystray pillow
python -m PyInstaller --noconfirm --clean --onefile --windowed --name feedel --add-data "web;web" --collect-submodules yt_dlp --collect-all webview --collect-submodules pystray --collect-submodules PIL feedel.py
```

`dist\feedel.exe` is self-contained: the page, yt-dlp, the window libraries and everything else are inside (`--collect-all webview` is pywebview; leave the three window packages out of both lines for a browser-only build). It writes `feedel.db` and `feedel.log` next to itself, so put it in its own folder. It has no console window; *Settings → quit feedel* stops it, as does *quit* in the tray menu when it runs in its own window. If no `yt-dlp.exe` is installed, mpv streams through the exe itself, which answers as yt-dlp when asked. Windows SmartScreen will warn once about an unsigned program; some antivirus products dislike single-file PyInstaller builds, in which case drop `--onefile` and ship the `dist\feedel` folder instead.

## Files

- `feedel.db` — one SQLite file with everything: settings, what you follow, seen and saved items with your tags and notes, the messages tab, the items themselves (kept 90 days after they leave their feed; saved ones forever) and the duration / Shorts cache. Back it up by copying it. `python feedel.py --export > backup.json` dumps it all as JSON; `python feedel.py --import backup.json` merges a dump (or an old `config.json` / `state.json` / `cache.json`) back in, adding and never removing. Open it with any SQLite tool to look around.
- `config.json`, `state.json`, `cache.json` — the files older versions used. They are migrated into `feedel.db` the first time this version runs and renamed `*.json.migrated`.
- `cache/avatars/` — avatar images. Safe to delete.
- `cache/webview/` — the window's own browser profile (theme choice and the like) when feedel runs in its own window. Safe to delete.
- `downloads/` — what the **download** button saves.

## Twitter caveat

X has no RSS and shut down most Nitter mirrors in August 2026. The program tries each instance in `nitter_instances` in order and uses the first that answers. When none answer you see a collapsed "N sources could not be fetched" line at the top of the page, and everything else keeps working.

## Contributing

This is a project between friends. [ROADMAP.md](ROADMAP.md) says where it is going (a small shared server: share videos, posts and papers with each other, discuss them, host it on a Mac mini) and [CONTRIBUTING.md](CONTRIBUTING.md) says how to run it from source and where things are. Issues are the place to argue.
