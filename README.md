# Recent Albums — self-contained web app

A single Docker container that replaces the GitHub Actions + git-branch
setup with:

- A web dashboard (report, exclude/include toggles, "Run scan now", live log tail)
- An in-container APScheduler cron job (default `0 6 * * *` UTC) instead of GH Actions `schedule:`
- A web-based OAuth flow (`/login` → Spotify → `/callback`) instead of the CLI's `--auth` flow
- State persisted to a Docker volume (`/data/spotify-state.json`) instead of committed to a `data` branch

## 1. Spotify app setup

In the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard):

1. Create an app (or reuse the existing one).
2. Add a **Redirect URI** that exactly matches `PUBLIC_BASE_URL` + `/callback`,
   e.g. `http://localhost:8080/callback` for local use, or
   `https://albums.yourdomain.com/callback` if it's exposed publicly.
3. Grab the Client ID / Client Secret.
4. Create the playlist you want auto-synced (or leave `SPOTIFY_PLAYLIST_ID`
   unset to get report-only mode with no playlist writes).

## 2. Configure

```bash
cp .env.example .env
# edit .env with your client id/secret, playlist id, and PUBLIC_BASE_URL
```

`FLASK_SECRET_KEY`: generate one with `python -c "import secrets; print(secrets.token_hex(32))"`.
If left blank, a random one is generated at container startup — fine for a
single long-running container, but means OAuth `state` won't survive a
container restart mid-login (just retry `/login`).

## 3. Run

```bash
docker compose up -d --build
```

Then open `PUBLIC_BASE_URL` in a browser (e.g. http://localhost:8080),
click **Connect Spotify account**, and authorize. The refresh token is
saved to the `spotify_data` volume (`/data/spotify-token.json`) so you
only do this once — it survives container restarts/rebuilds as long as
the volume isn't deleted.

From then on:
- The scheduler runs a scan automatically per `CRON_SCHEDULE`.
- **Run scan now** on the dashboard triggers one immediately (won't double-run
  if the scheduled job is already mid-scan — both share the same lock).
- Exclude/Include buttons write `manual_override` directly into
  `spotify-state.json`, same semantics as hand-editing the JSON in the
  original CLI-based setup.

## 4. Backing up / migrating state

Everything that matters lives in the `spotify_data` volume:

```bash
docker run --rm -v spotify-webapp_spotify_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/spotify-data-backup.tar.gz -C /data .
```

If you're migrating from the old GitHub Actions setup, copy your existing
`spotify-state.json` into the volume before first run (or `docker cp` it
into the running container at `/data/spotify-state.json`), and set
`SPOTIFY_REFRESH_TOKEN` as an env var for the first boot only — it'll be
picked up as a fallback and then you can immediately re-auth via `/login`
to have it persisted properly into the volume going forward.

## 5. What's intentionally not carried over

- The `data`-branch git-commit dance (`docs/spotify-data-branch-plan.md`) —
  replaced entirely by the Docker volume; no git operations happen at
  runtime anymore.
- `--batch-size` / per-run artist caps — the per-category rate-limit
  isolation (`endpoint_category`, `LongRateLimitBlock`) is preserved as-is,
  so this still isn't needed.
- The one-off scripts (`backfill_exclusions_once.py`, `add_missing_albums.py`,
  `reorder_playlist_once.py`) — these were meant to run once and be deleted;
  if you need one of them again, `docker exec` into the container and run
  it directly against `/data/spotify-state.json`, or port it into a new
  dashboard route the same way `/albums/<id>/override` was added.

## Environment variables

| Var | Required | Default | Notes |
|---|---|---|---|
| `SPOTIFY_CLIENT_ID` | yes | — | |
| `SPOTIFY_CLIENT_SECRET` | yes | — | |
| `SPOTIFY_PLAYLIST_ID` | no | unset | report-only mode if unset |
| `PUBLIC_BASE_URL` | yes | `http://localhost:8080` | must match the Redirect URI registered with Spotify |
| `CRON_SCHEDULE` | no | `0 6 * * *` | 5-field cron, evaluated in UTC |
| `MIN_REQUEST_INTERVAL` | no | `20` | seconds between Spotify API calls |
| `INTERVAL_DAYS` | no | `7` | how often each artist is checked |
| `DAYS_LOOKBACK` | no | `365` | report/prune window |
| `FLASK_SECRET_KEY` | no | random per boot | set explicitly for OAuth to survive a mid-login restart |
