# Contributing

## Run it from source

```
git clone https://github.com/nyazbek/quietfeed
cd quietfeed
python quietfeed.py
```

Python 3.10 or newer, nothing to install. `pip install yt-dlp` and an mpv install give you playback and downloads; see the README.

Your own data (`config.json`, `state.json`, `cache.json`, `downloads/`) is written next to the script and is ignored by git. Never commit it.

## Where things are

- `quietfeed.py` — the whole server, in sections: helpers, state, subscriptions, youtube, twitter, rss, arxiv, refresh, import/follow, account sync, playback, web (the HTTP handler at the bottom).
- `web/index.html`, `web/app.js`, `web/style.css` — the page. Vanilla JS, no build step. The page only ever talks to `localhost`; images from Google go through the server's proxy.
- `ROADMAP.md` — where this is going and why. Read it before proposing a feature.

## Conventions

- Standard library only in the server. If something truly needs a dependency, open an issue first.
- Nothing in the browser ever requests youtube.com, x.com or their image hosts. The server fetches, the page shows.
- Every network fetch is done by the server and cached where it makes sense (`cache.json`).
- Filters that change what the user sees are applied at request time (`visible_items`), not at fetch time, so settings take effect immediately.
- Keep the look quiet: one column, one serif, no counters, no badges.

## Testing a change

There is no test suite yet (a good first contribution). Run the server on another port so it does not clash with your own instance, and drive it with curl:

```
python quietfeed.py --no-open --port 8766
curl localhost:8766/api/items
curl -H Content-Type:application/json -d '{"ref":"math.PR","kind":"arxiv"}' localhost:8766/api/subs/add
```

## Releases

Tag a version and push the tag; GitHub Actions builds `quietfeed-windows.exe` and `quietfeed-macos` and attaches them to the release.

```
git tag v0.2
git push --tags
```
