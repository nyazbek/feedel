# Where quietfeed is going

Today quietfeed is a single-user reader that runs on your own machine. The aim is a small shared place for a group of friends: everyone keeps their own quiet feed, and there is a common room where you put things in front of each other and talk about them. A personal reddit, hosted on something as small as a Mac mini.

This document is the plan. Argue with it in issues; change it in pull requests.

## Principles

These are the reasons the project exists. New features that break them do not go in.

1. **No engagement machinery.** No infinite scroll, no unread counters on the front page, no badges, no ranking by popularity, no autoplay. Chronological, finite, closable. "Saved by three friends" is information; a like button is a slot machine.
2. **Light.** Python standard library, SQLite, vanilla JavaScript. One process, one file to deploy, no build step, no container required. It must run comfortably on a Mac mini or a Raspberry Pi for a dozen people.
3. **The browser never touches the attention sites.** The server fetches feeds and proxies images; playback happens in the user's own player. A browser-level block on youtube.com or x.com keeps working.
4. **Everything is inspectable and reversible.** Your data is rows in one SQLite file next to the program; export and import to JSON always exist; nothing is ever deleted automatically.
5. **Deliberate.** Following, sharing and commenting are things you choose to do, with a "why". The app never nudges.

## Milestones

### 1. Collaboration (done)

GitHub repository, issues, MIT license, a release workflow that builds Windows and macOS binaries on every `v*` tag.

### 2. Storage: one SQLite file

Replace `config.json`, `state.json` and `cache.json` with `quietfeed.db`. Keep a JSON export/import. Migrate the current files on first run. This comes first because every multi-user feature is "the same thing, per user".

Sketch:

```
users    (id, name, pass_hash, invited_by, created, settings_json)
sources  (id, kind, ref, name, meta_json, last_fetched, error)        -- a channel, account, feed or arXiv query; shared by everyone who follows it
follows  (user_id, source_id, tags, muted, min_minutes, note)
items    (id, source_id, kind, ext_id, title, author, ts, payload_json, fetched)
marks    (user_id, item_id, seen_at, saved_at, tags, note)
shares   (id, user_id, item_id, note, created)
comments (id, share_id, user_id, parent_id, body, created)
```

A source followed by three people is fetched once. Items are kept for a configurable window (90 days?) unless someone saved or shared them.

### 3. Users and invites

- Invite links created by whoever runs the server; no public sign-up.
- Password login (PBKDF2 through `hashlib`, standard library) with a session cookie. Magic links by e-mail can come later if anyone wants them.
- Each user has their own follows, seen, saved, settings and library. The "following" tab and account sync work per user.
- The single-user mode stays: with no users table rows, the app behaves exactly as today.

### 4. Hosting

- `--host 0.0.0.0`, `--data-dir`, a `launchd` plist for macOS and a `systemd` unit for Linux, in `deploy/`.
- Reach it over Tailscale (simplest: nobody outside the group can even see it) or behind Caddy for HTTPS on a domain.
- Backups are a copy of the `.db` file.
- Playback when the server is remote: a web page cannot start mpv on your laptop. Plan: the quietfeed binary gets a **client mode** that registers a `quietfeed://play/<id>` URL handler and launches mpv locally; the page uses that link when the server is not on localhost. Fallback: copy link.

### 5. Sharing

- "share with the group" on any item, with a note: why this is worth someone's time.
- A **shared** view: what friends shared, newest first, who shared it, their note. Saved-by-friends is shown as names, not a number.
- Each user's shares are also an RSS feed (`/u/<name>/shared.rss`), so friends who prefer their own reader can follow along.

### 6. Discussion

- Threaded comments on shared items. Plain text with a small markdown subset (links, emphasis, code, quotes). No reactions.
- Per-thread "mark read", no global unread count.
- An optional weekly e-mail digest of what was shared and discussed.

### 7. Collections and reading groups

- Shared tag lists ("probability seminar", "things to read before the trip") that several people can add to.
- A collection page with the items in order and the notes people left.
- Paper-club mode: a date, a paper, the comments underneath.

### Later, maybe

- An RSS feed of everything a user follows, so quietfeed can be someone's backend for another reader.
- Federation between friend servers (ActivityPub) once there is more than one server to federate.
- Bluesky custom feeds as a source.

## Non-goals

Public sign-up, advertising, recommendations, real-time anything (the page polls every minute, which is plenty), native mobile apps (the page is responsive), and anything that measures attention.

## How to help

Pick a milestone issue, say you are on it, keep pull requests small. The server is one Python file on purpose until milestone 2 splits it into modules along the section headers it already has (`youtube`, `twitter`, `rss`, `arxiv`, `refresh`, `import`, `web`). The page is three files in `web/` with no framework; keep it that way until there is a reason not to.
