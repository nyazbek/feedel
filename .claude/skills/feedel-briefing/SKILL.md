---
name: feedel-briefing
description: Write the day's briefing into feedel's messages tab — the few things in the feed worth the person's time, what friends are discussing, and the tasks carried over — using the feedel MCP tools. Use when asked for a briefing, a daily note, "qué hay hoy", or when a scheduled run says to brief.
---

# Daily briefing for feedel

feedel runs on this machine and is an MCP server (`feedel`, http://127.0.0.1:8765/mcp). If its tools are missing, say so and stop: the app is not running, or the server is not added (`claude mcp add --transport http feedel http://127.0.0.1:8765/mcp`).

The person reads deliberately: they follow a small set of channels, accounts, blogs and arXiv queries, and the fight is against scrolling. A briefing is a short note that saves them a scroll, never a list of everything.

## Steps

1. **Read before writing.** Call `messages` with `limit` 10. Look at your last briefing: which `- [ ]` tasks are still unticked (carry them over, at most five, oldest first), and whether the person replied (`channel: "me"` under your note). Answer replies in the new note, in one line each.
2. **Look at the day.** Call `feed` with `hours: 36` and `unseen_only: true`, once per kind when the result is large. Call `library` with `limit` 10 for what they recently saved (their notes and tags say what they care about). Call `discussions` for what friends are talking about.
3. **Choose.** Pick two to five items, no more: a long video from a channel they follow is worth more than ten posts; a paper matching a saved paper's topic is worth more than a new category listing; a discussion a friend opened about something in the feed is always worth mentioning. Prefer what connects to their library. Skip anything that is only hype.
4. **Write the note** with `send_message`:
   - `title`: the date, e.g. `Sunday 18 Oct` (or in the person's language if their replies are in Spanish).
   - `### papers` (only when at least one paper passed the rule above): title, authors, one line why.
   - `body` in Markdown, under 200 words:
     - one opening line with the shape of the day (how much is new, in which kinds);
     - `### para hoy` / `### for today`: the picks as task lines `- [ ] watch … (20 min) — why`, each with its link in the `links` field, not inline;
     - `### con amigos` / `### with friends`: one or two lines on open discussions, only if there are any;
     - carried-over tasks, marked `(from Tuesday)`;
     - one closing line if there is something to decide or reply to.
   - `links`: the URLs of every item you mention, in the order mentioned, so the app shows them as cards.
5. **Do not** mark anything seen or saved, and do not add links to the inbox, unless the person asked for exactly that.

## Papers: only the person's area

A category watch like `cat:hep-th` is far too wide to recommend from. Before choosing papers, build the person's profile from three places: `following` with kind `arxiv` (their author and keyword watches), `library` (saved papers and their notes), and `discussions` (what friends are talking about). Recommend a paper only when it sits in that profile, at most three per briefing, each with one line on which of their topics it touches; when nothing fits, say so in one line rather than padding.

The profile for this repo's owner (adjust yours in the scheduled task's prompt): dualities (T, U, O(d,d), β symmetry), double and exceptional field theory, generalized and exceptional generalized geometry, α' corrections, F-theory and M-theory compactifications, symmetries of 10d/11d supergravity, AdS3 supergravity and BPS solutions. Out unless a friend discusses it or it is in the library: amplitudes, generic holography, cosmology, condensed-matter-flavoured hep-th, machine learning.

## When run on a schedule

A scheduled run has no one to ask: write the note anyway, say in its last line what you were unsure about, and keep it shorter. Never post twice for the same day: if `messages` shows a note from you with today's date, reply to it (`reply_to`) with what changed instead of writing a new one.
