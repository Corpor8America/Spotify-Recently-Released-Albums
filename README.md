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
2. Add a **Redirect URI** that exactly matches the **Public base URL** (set on
   the Settings page) + `/callback`, e.g. `http://127.0.0.1:8080/callback` for
   the local compose or `http://127.0.0.1:8081/callback` for production on the
   same machine (Spotify rejects `localhost`, use `127.0.0.1`), or
   `https://albums.yourdomain.com/callback` if it's exposed publicly.
3. Grab the Client ID / Client Secret.
4. Create the playlist you want auto-synced (or leave the Playlist ID blank in
   Settings to get report-only mode with no playlist writes).

## 2. Configure

Pick a compose profile:

- **Production** (default, host port `8081`, pulls the published image from
  GHCR — no local build): `docker compose up -d`
- **Local** (host port `8080`, separate `spotify_local_data` volume, so its
  state never collides with production):
  `docker compose -f docker-compose.local.yml up -d --build`
- **Dev / mock Spotify** (no real API calls, seeds from `tests/seed`):
  `docker compose -f docker-compose.dev.yml up -d --build`

No environment variables are needed — everything is configured on the Settings
page. Then open the **Settings** page (`/settings`) and fill in:

- **Spotify Client ID / Client Secret** (from the developer dashboard)
- **Spotify Playlist ID** (optional — blank = report-only mode, or click
  **Create playlist** to have one made and saved for you)
- **Cron schedule** (5-field, UTC), **check each artist every N days**,
  **days lookback**, **min seconds between API requests**
- **Public base URL** — must match the Redirect URI registered with Spotify
  (`/callback` is appended automatically). It defaults to the URL you're
  accessing the app from, so it only needs editing when you move the app or
  access it via a different host/domain.

Settings are persisted to the `spotify_data` volume
(`/data/app-config.json`), so they survive restarts. The Flask session
secret is auto-generated on first boot and persisted the same way —
nothing else to configure.

## 3. Connect

Open the app in a browser (http://127.0.0.1:8081 for the production compose,
http://127.0.0.1:8080 for the local one), click **Connect Spotify account**, and
authorize. The refresh token is saved to the same volume as the app state
(`/data/spotify-token.json`) so you only do this once — it survives container
restarts/rebuilds as long as the volume isn't deleted.

From then on:
- The scheduler runs a scan automatically per the cron schedule set in Settings.
- **Run scan now** on the dashboard triggers one immediately (won't double-run
  if the scheduled job is already mid-scan — both share the same lock).
- Exclude/Include buttons write `manual_override` directly into
  `spotify-state.json`, same semantics as hand-editing the JSON in the
  original CLI-based setup.

## 4. Backing up / migrating state

Everything that matters lives in the app's data volume:
`spotify-recently-released-albums_spotify_data` (production) or
`spotify-recently-released-albums_spotify_local_data` (local):

```bash
docker run --rm -v spotify-recently-released-albums_spotify_data:/data -v $(pwd):/backup \
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

## Configuration

All configuration is done from the **Settings** page and persisted to
`/data/app-config.json` in the `spotify_data` volume. No environment
variables are required to run the container.
