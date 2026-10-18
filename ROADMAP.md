# Where quietfeed is going

quietfeed is a quiet, chronological reader: your YouTube channels, X accounts, Substack newsletters and arXiv categories in one list, with a library for what you keep and an inbox for links that reach you from elsewhere. The conversation about those things lives in **Zulip**, which already does threads, search and LaTeX well; quietfeed reads it back in, so discussions and recommendations between friends appear right under the things they are about. quietfeed's job is to be the calm place things arrive in and to hand them to the group with one click.

This document is the plan. Argue with it in issues; change it in pull requests.

## The split: quietfeed + Zulip

- **quietfeed** fetches, filters and keeps. Each person runs their own copy today; one small machine can run it for everyone once identity exists.
- **Zulip** is where things get discussed. Streams per kind (`#vids`, `#papers`, …), a `#recommendations` stream, one topic per item. Zulip renders LaTeX, keeps every thread findable, notifies people, and has a free hosted plan for small groups.
- The bridges are **share** (post an item and a note to its stream), **recommend** (post it to friends by name), and the read-back: topics are matched to items by the links in them, so "Ana and Juan talk about this" and "Juan recommends this" show up under the item in each person's own feed.

## Principles

1. **No engagement machinery.** No infinite scroll, no unread counters on the front page, no badges, no ranking by popularity, no autoplay. Chronological, finite, closable.
2. **Pre-approved, never pushed.** Everything in the feed is from a person you chose, or recommended by a friend who chose you. A video from a channel you follow is allowed to be a video, and a Short you open yourself is yours to open; the fight is against scrolling, not against content. Nothing is ever recommended by a machine.
3. **Light.** Python standard library, SQLite, vanilla JavaScript. One process, one file to deploy, no build step. It must run comfortably on a Mac mini or a Raspberry Pi.
4. **The browser never touches the attention sites.** The server fetches feeds and proxies images; playback happens in your own player. A browser-level block on youtube.com or x.com keeps working.
5. **Everything is inspectable and reversible.** Your data is rows in one SQLite file next to the program, with JSON export and import. Nothing is deleted automatically, and an unfollow is remembered so a sync cannot undo it.
6. **Deliberate.** Following, saving, sharing, recommending and searching are things you do on purpose, with a "why". The app never nudges.

## What exists (October 2026)

- Sources: YouTube channels (durations and Shorts from the channel's own pages, no API key), X accounts through Nitter with videos from X's public syndication endpoint, Substack and any RSS/Atom feed, arXiv categories, authors and keyword watches with a papers toolbar and search.
- Import: Google Takeout, OPML, the X archive, pasted lists with channel names resolved by search; YouTube account sync through the official API or a signed-in browser's cookies; remembered unfollows.
- Inbox: links from WhatsApp or anywhere, by paste, by an opt-in clipboard watcher, or by making quietfeed the system link handler on Windows (other links go to your normal browser).
- Source pages, a tagged library with notes, search over what you follow with an explicit YouTube search behind it, calm titles, block words, per-source minimum length and cap.
- Zulip: share per kind, recommend to friends, discussion lines with inline threads and replies, a talk view of topics. The **talk** tab: the group's streams read and discussed from inside quietfeed, new topics, search, quiet read state, reactions, editing your own messages.
- One SQLite file, a single-file executable for Windows and macOS built on every tag.

## Milestones

### 1. Collaboration — done
### 2. One SQLite file — done
### 3. Share to Zulip — done
### 4. Read Zulip back: discussions, recommendations, the talk tab — done

### 5. Identity through Zulip
Every member already has a Zulip account, so quietfeed does not need its own user system. Logging into a hosted quietfeed is "paste your Zulip API key": Zulip is the identity, quietfeed keeps per-person follows, settings, marks and inbox keyed by that account. Sources are shared and fetched once. With no accounts configured the app behaves exactly as today. This replaces the earlier invite-links plan.

Since milestone 4, everyone in the group already has a Zulip account and a personal API key that quietfeed holds. Identity can therefore be "sign in with your Zulip API key": the server asks Zulip who the key belongs to (`/users/me`) and that is the user, with no separate password system, no invite tokens and no second place where accounts live. Whoever is in the Zulip organisation is in; whoever leaves it is out. The PBKDF2 route stays as the fallback for a group without Zulip.

### 6. Hosting
`--host`, `--data-dir`, a launchd plist and a systemd unit under `deploy/`, and two documented ways to keep it private: Tailscale (only your own devices can see it) or Cloudflare Tunnel with Access (a login wall on your own domain). Remote playback: a `quietfeed://play/<id>` URL handler registered by the binary in client mode that opens mpv locally; fallback copy link.

### 7. Collections and reading groups
Shared tag lists several people can add to, a collection page with items and notes in order, and a paper-club mode: a date, a paper, and the Zulip topic for it created automatically.

### 8. Polish that is overdue
A test suite (issue #7) around the pure functions (`parse_ref`, `parse_import`, `arxiv_ref`, `parse_feed`, `canon_key`, `visible_items`) and the Zulip stub driver, plus a GitHub Actions job that runs it. Zulip's events API for live updates instead of the minute poll. A real-Zulip test, since everything Zulip-side has so far been verified against a stub.

### Later, maybe
An RSS feed of everything a user follows, so quietfeed can be someone's backend for another reader. Bluesky custom feeds as a source.

## Non-goals

Public sign-up, advertising, algorithmic recommendations, real-time anything beyond Zulip's own events, native mobile apps, comments or reactions stored inside quietfeed (that is what Zulip is for), and anything that measures attention.

## How to help

Pick a milestone issue, say you are on it, keep pull requests small. The server is one Python file on purpose, in sections (`storage`, `youtube`, `twitter`, `rss`, `arxiv`, `refresh`, `import`, `source pages`, `inbox`, `zulip`, `web`). The page is three files in `web/` with no framework; keep it that way until there is a reason not to. Test against a stub of the Zulip API before claiming something works; see CONTRIBUTING.md.
