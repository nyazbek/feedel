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

- **videos** from YouTube channels, with a minimum length, played in mpv. Shorts are hidden from the feed by default (the fight is against scrolling, not against a video you chose), and a Short you open yourself through the inbox still shows.
- **tweets** from X accounts through a Nitter mirror (see the caveat at the end). A post with a video shows it with play and download: you chose to follow that person, so their videos are pre-approved. Media comes from X's public syndication endpoint, no login.
- **posts** from Substack and any RSS/Atom feed; open in the browser.
- **papers** from arXiv: categories (`math.PR`, `cs.LG`, or `math` for a whole archive), authors, or keyword watches. In the *all* view, papers fold into one line per day ("12 new papers") so they never drown the rest; the *papers* chip opens them properly.

The papers view has its own toolbar: **latest** shows the newest listing you have (arXiv dates papers by submission and lists them a day or two later, so this is keyed to the newest batch, not the clock), **3 days / week / all** count back from now. The search box asks arXiv directly: keywords (quote a phrase), author, a date range, sorted by newest, last updated or relevance. Click an author's name on any paper to follow them or list their papers; followed authors are highlighted. Every paper has abstract, abs and pdf links, copy link, and save.

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

If a browser on this machine is signed in to YouTube, pick it under *Browser session* and the program re-reads that browser's subscriptions list on every refresh using the cookies the browser already stores. The browser itself never opens youtube.com. Firefox works best; recent Chrome builds encrypt cookies in a way yt-dlp sometimes cannot read.

### X

X's API no longer offers free read access and scraping a logged-in session risks the account, so there is no live sync. The complete route is the archive: on x.com, *Settings → Your account → Download an archive of your data*, then drop `data/following.js` into the import box. It only holds numeric ids; they are resolved to handles through x.com's public intent redirect (no login), and whatever does not resolve on the spot is queued and retried every refresh.

## Links from elsewhere

A link someone sends you on WhatsApp, in mail or anywhere else can be opened in quietfeed instead of a browser where the site is blocked. Three ways in, from easiest to most automatic:

- **+ link** above the feed: paste any YouTube, X, arXiv or article link. quietfeed fetches it itself and shows it as an item in the **inbox**, with play, save and share like everything else, plus a *follow* button for the channel, account, author or newsletter it came from. Channel and profile links become a follow suggestion directly.
- **Clipboard watcher** (settings): with it on, every link you *copy* that quietfeed can show lands in the inbox by itself. Copy a link in WhatsApp, switch to quietfeed, it is there. A second checkbox widens it from YouTube, X and arXiv links to every link. Only the clipboard text is read, only links are looked at, and nothing leaves the machine except the fetch of that link's own page.
- **Make quietfeed the link handler** (Windows): run `quietfeed --register-handler` (or `python quietfeed.py --register-handler`), then pick *quietfeed* under *Settings → Apps → Default apps → Web browser*. From then on a click on any link in any app goes through quietfeed: videos, posts and papers open in the inbox, everything else is handed to your normal browser: whatever your default was when you registered (quietfeed remembers it, since afterwards the default is quietfeed itself), shown and changeable under the gear as *send the links it cannot show to*. `--unregister-handler` undoes it. `quietfeed --open URL` is the same thing from a terminal.

Tweets are read through X's public syndication endpoint, which needs no login and works for public posts, videos included. A reply shows the post it answers right above it, a quote-post shows the quoted post, and a post continuing the author's own thread says so; without that context half of X reads like half a sentence. Every post has a **thread** action: it shows what the post replies to, the author's own continuation numbered 2/, 3/…, and other people's replies folded away, read from a Nitter conversation page (parents only, through the syndication endpoint, when no instance answers). Videos inside a thread are playable too.

## Source pages

Click any author's name in the feed, the inbox or the following list and you land on that source's page: avatar, handle, the **follow** or **unfollow** button, **mute**, and its tags, minimum length and "why" note, followed by its recent uploads or posts. A source you do not follow yet (a channel from an inbox link, an account a friend mentioned) is fetched on the spot so you can look before following.

## Discussions, through Zulip

With Zulip set up (below), quietfeed reads your group's streams back too, and the **talk** tab is where the group uses Zulip from inside quietfeed: enough to read and discuss, not a second chat client.

- **Streams.** Every stream shares go to (the default `#quietfeed` and any per-kind ones) plus any others you list in settings, as text chips at the top of the tab. Each lists its topics newest first: who is in it, how many messages, when, and the item it is about when quietfeed can tell (the video, post or paper is shown right there). A topic with messages you have not read says so in muted text (`3 new`), using Zulip's own read flags, so opening the thread here marks it read in Zulip too and vice versa. There is no counter anywhere else, nothing in the nav.
- **Threads.** *open discussion* unfolds the topic in place, LaTeX rendered, oldest first, with a reply box that posts as you. Each message carries its reactions (click one to add yours or take it back, `+` offers a few more) and, on your own messages, an **edit** link that opens the markdown source. The same thread appears under any feed, inbox or library item that a topic links to, where a quiet line says who is talking about it.
- **New topic.** *+ topic* at the top of the tab starts a topic from scratch in the selected stream: a name and a first message, markdown and `$LaTeX$`.
- **Search.** The box asks Zulip's own search within the selected stream and shows the matches grouped by topic, Zulip's highlighting kept, each openable in place.

Everything beyond that (people, private messages, stream settings, long history) is *in Zulip ↗*, one click away on every topic and thread.

## Recommendations, from friends only

The only recommender in quietfeed is a person who chose you. In the share box, *recommend to someone instead* lets you pick friends (or everyone) and write why; it posts to the recommendations stream in Zulip as "Nahuel recommends this to @Juan", so Juan is notified the Zulip way too. On the other side, everything recommended to you, by name or to everyone, collects under a **from friends** chip in your feed: the item itself with play, save and all the usual actions, who recommended it and their note, a *thanks* that reacts on their message, and the discussion one click away. A friend's recommendation of something already in your feed also shows as a line under that item. Nothing else is ever recommended.

## Friends: getting people into the group

The **friends** tab is the whole onboarding, for both sides.

- **Organizer**: once your quietfeed is connected to the group's Zulip, the tab shows who is there, lets you create a reusable invitation link (or paste one from Zulip), send e-mail invitations through Zulip to specific addresses, and copy a **setup code**: one pasteable string carrying the site, the streams, the invitation link and your contact, with no keys in it.
- **Friend**: paste the setup code, and three steps appear: open the invitation and create the Zulip account (or ask the organizer for access when the code has no link), open the Zulip account settings to copy the API key, paste it with the e-mail used. Connecting also subscribes them to the group's streams. From then on their feed shows the group's discussions and recommendations.
- **No group yet?** Create a free Zulip organization at zulip.com/new, make the streams, connect under settings, and the tab starts handing out codes.

## Sharing to Zulip

The conversation lives in Zulip, not in quietfeed (see [ROADMAP.md](ROADMAP.md)). Every item also has **copy link** for sending it to a friend any other way. **share** opens a note box and posts the item and your note to a Zulip stream, as you, with one topic per item, so the group can discuss it there with threads, search and LaTeX.

Setup, once, under the gear: your Zulip site (`yourgroup.zulipchat.com`), your e-mail, your personal API key (in Zulip: *Settings → Account & privacy → API key*), and the stream to post to (default `quietfeed`; create it in Zulip first). *Test connection* confirms it. Below that, each kind can go to its own stream, videos to `#vids`, papers to `#papers` and so on; the share box shows the target and lets you pick another. quietfeed reads every stream named there, so discussions are found wherever they live, and the **talk** tab can show further streams you list, comma-separated. The key is stored in your `quietfeed.db` and never shown back on the page.

## Library

*Save* on anything puts it in the library, where it stays after it has left the feed. Each saved item takes **tags** and a **note**; the library filters by kind and tag and searches titles, authors, abstracts and notes. Seen and saved state live in `quietfeed.db`.

## Settings (the gear)

Hide thumbnails, calm titles (ALL CAPS → sentence case, `!!!` → `!`), hide seen, hide Shorts from the feed, minimum video length, a per-source cap so one prolific channel cannot drown the rest, and block words (`/regex/` allowed). The theme button cycles auto / light / dark.

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
