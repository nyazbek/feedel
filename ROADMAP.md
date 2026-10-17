# Where quietfeed is going

quietfeed is a quiet, chronological reader: your YouTube channels, X accounts, Substack newsletters and arXiv categories in one list, with a library for what you keep. The conversation about those things lives in **Zulip**, which already does threads, search and LaTeX well. quietfeed's job is to be the calm place things arrive in and to hand them to the group with one click.

This document is the plan. Argue with it in issues; change it in pull requests.

## The split: quietfeed + Zulip

- **quietfeed** fetches, filters and keeps. Each person runs their own copy, or one Mac mini runs it for everyone once accounts exist.
- **Zulip** is where things get discussed. A stream such as `#quietfeed`, one topic per shared item. Zulip renders `$$\LaTeX$$`, keeps every thread findable, and has a free hosted plan for small groups.
- The bridge is the **share** button: it posts the item and your note into the stream, as you, through your own Zulip API key. Nothing is built twice.

## Principles

1. **No engagement machinery.** No infinite scroll, no unread counters on the front page, no badges, no ranking by popularity, no autoplay. Chronological, finite, closable.
2. **Light.** Python standard library, SQLite, vanilla JavaScript. One process, one file to deploy, no build step. It must run comfortably on a Mac mini or a Raspberry Pi.
3. **The browser never touches the attention sites.** The server fetches feeds and proxies images; playback happens in your own player. A browser-level block on youtube.com or x.com keeps working.
4. **Everything is inspectable and reversible.** Your data is rows in one SQLite file next to the program, with JSON export and import. Nothing is deleted automatically.
5. **Deliberate.** Following, saving and sharing are things you choose to do, with a "why". The app never nudges.

## Milestones

### 1. Collaboration — done
GitHub repository, issues, MIT license, a release workflow that builds Windows and macOS binaries on every `v*` tag.

### 2. One SQLite file — done
`quietfeed.db` replaces the three JSON files, with migration, `--export` and `--import`, persisted items and 90-day pruning.

### 3. Share to Zulip — done
"share" on any item opens a note box and posts to the configured stream and topic as you. Settings hold site, e-mail, API key and stream, with a connection test.

### 4. Shared view
Show the `#quietfeed` stream inside quietfeed, newest first: who shared what, their note, and a link into the Zulip thread. Read-only, through the same API key. This is the "what my friends found" page without building a second social network.

### 5. Users and invites
Invite links created by whoever runs the server; password login with PBKDF2 (standard library) and a session cookie. Each user has their own follows, seen, saved, settings and Zulip key; sources are shared and fetched once. With no users, the app behaves exactly as today.

### 6. Hosting
`--host`, `--data-dir`, a launchd plist and a systemd unit under `deploy/`, Tailscale or Caddy notes, backups as a copy of the `.db`. Remote playback: a `quietfeed://play/<id>` URL handler registered by the binary in client mode that opens mpv locally; fallback copy link.

### 7. Collections and reading groups
Shared tag lists several people can add to, a collection page with items and notes in order, and a paper-club mode: a date, a paper, and the Zulip topic for it created automatically.

### Later, maybe
An RSS feed of everything a user follows, so quietfeed can be someone's backend for another reader. Bluesky custom feeds as a source.

## Non-goals

Public sign-up, advertising, recommendations, real-time anything, native mobile apps, comments or reactions inside quietfeed (that is what Zulip is for), and anything that measures attention.

## How to help

Pick a milestone issue, say you are on it, keep pull requests small. The server is one Python file on purpose, in sections (`storage`, `youtube`, `twitter`, `rss`, `arxiv`, `refresh`, `import`, `zulip`, `web`). The page is three files in `web/` with no framework; keep it that way until there is a reason not to.
