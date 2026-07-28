# Implementation Plan: Fix Spotify Dev-Mode Quota Lockouts via Check Staggering
### (v2 — state persisted by committing to the repo, not GitHub Actions cache)

## Goal
Stop tripping Spotify's development-mode daily quota on `/artists/{id}/albums`
by checking a rotating subset of followed artists each run instead of all 82
every run, while guaranteeing every artist — including ones with long gaps
between releases — is still checked on a fixed, predictable cadence, and while
never losing a previously-found "recent" album from the report just because it
wasn't rediscovered on a given day.

The tracking state (which artists were checked when, and every album ever
seen) is persisted by **committing a JSON file directly into the repo**, not
via GitHub Actions cache. Cache entries are best-effort and get evicted after
7 days of disuse or under storage pressure — since this whole design depends
on the state surviving indefinitely, a committed file is the correct choice.

Do not remove or replace the existing `RateLimiter` / `Retry-After` / long-wait
exit logic. That part already works. This plan only adds a scheduling layer on
top of it.

---

## 0. Preconditions / things to verify before starting

1. Confirm whether `main()` currently writes a report anywhere (file, stdout,
   GitHub Step Summary, etc.) at the end of a successful run. In the version
   reviewed, `format_markdown_table()` and `args.json` are defined/parsed but
   never called/used at the end of `main()`. If that's genuinely the current
   state, Step 6 (report output) is required, not optional.
2. Confirm the existing `SPOTIFY_REFRESH_TOKEN` was issued with the
   `playlist-modify-public` / `playlist-modify-private` scopes (the auth flow
   already requests these). If the current token predates that scope being
   added, playlist-add calls will fail with 403 — re-run `--auth` to get a
   fresh refresh token if so.
3. Confirm the repo isn't set up to block bot pushes on the target branch
   (branch protection requiring PR review would break the commit step below).
   If it is, either push to a dedicated `data` branch instead of `main`, or
   loosen protection for the bot identity used.
4. Confirm `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` /
   `SPOTIFY_REFRESH_TOKEN` / `SPOTIFY_PLAYLIST_ID` secrets are all set
   (unrelated to this fix, just a sanity check before testing).

---

## 1. The single persisted file: `spotify-state.json`

One file, committed to the repo root (not a dotfile — keep it visible and
diffable). Replaces both of the old cache files entirely.

```json
{
  "artists": {
    "<artist_id>": {
      "name": "Kreator",
      "last_checked": "2026-07-24T06:03:11+00:00"
    }
  },
  "known_albums": {
    "<album_id>": {
      "artist": "Kreator",
      "artist_id": "<artist_id>",
      "name": "Album Name",
      "type": "album",
      "release_date": "2026-06-01",
      "url": "https://open.spotify.com/album/...",
      "total_tracks": 11,
      "first_seen": "2026-07-24T06:03:11+00:00"
    }
  },
  "in_progress": {
    "due_ids": ["id1", "id2", "id3"],
    "processed_ids": ["id1"],
    "retry_after": null
  }
}
```

Each `known_albums` entry additionally gets an `"added_to_playlist": true|false`
field (see Step 9) so a retry after a partial failure doesn't double-add
tracks.

- `artists` / `known_albums`: long-lived, never cleared, grow and update over
  time. This is what makes staggering safe (see Step 4).
- `in_progress`: short-lived, mirrors what `.spotify_progress.json` used to
  do. Set to `null` when no run is currently mid-flight; populated while a run
  is either actively working through `due_ids`, or has exited early after a
  long `Retry-After` wait and is waiting to be resumed by the next scheduled
  run.

Rationale for one file instead of two: since persistence is now git commits,
not cache keys, there's no operational reason to split them — one file means
one commit per run instead of coordinating two.

---

## 2. New config constants

```python
CHECK_INTERVAL_DAYS = 7          # how often each artist gets checked
STATE_FILE = Path(__file__).parent / "spotify-state.json"
PLAYLIST_ID = os.environ.get("SPOTIFY_PLAYLIST_ID")   # required for playlist feature
```

Remove `PROGRESS_CACHE` and any references to a separate schedule cache path.

Add a new `--interval-days` CLI flag (default 7), same pattern as `--days`, so
it's tunable without a code change if 7 still trips the quota.

---

## 3. New functions

### `load_state()` / `save_state(state)`

```python
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"artists": {}, "known_albums": {}, "in_progress": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
```

Call `save_state()` after every artist is processed — same crash-safety
reasoning as the old `save_progress()` calls. This only writes to the local
checkout; the workflow's commit step (Step 7) is what makes it durable in git.

### `get_due_artists(artists, state, interval_days)`

```python
def get_due_artists(artists, state, interval_days):
    now = datetime.now(timezone.utc)
    due = []
    for artist in artists:
        entry = state["artists"].get(artist["id"])
        if entry is None:
            due.append(artist)
            continue
        last_checked = datetime.fromisoformat(entry["last_checked"])
        if now - last_checked >= timedelta(days=interval_days):
            due.append(artist)
    return due
```

A newly-followed artist (no entry at all) is always immediately due — same as
"never checked."

### `record_album(state, artist, album, now_iso)`

```python
def record_album(state, artist, album, now_iso):
    existing = state["known_albums"].get(album["id"], {})
    state["known_albums"][album["id"]] = {
        "artist": artist["name"],
        "artist_id": artist["id"],
        "name": album["name"],
        "type": album["album_type"],
        "release_date": album["release_date"],
        "url": album["external_urls"]["spotify"],
        "total_tracks": album["total_tracks"],
        "first_seen": existing.get("first_seen", now_iso),
    }
```

Dedup by `album_id`; `first_seen` is preserved across updates rather than
overwritten, in case you later want to distinguish "newly discovered this
run" from "released this week."

### `get_report_albums(state, days)`

```python
def get_report_albums(state, days):
    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for album in state["known_albums"].values():
        release_date = parse_release_date(album["release_date"])
        if release_date is None or release_date >= cutoff:
            result.append(album)
    return result
```

Filters by release date against the *entire accumulated history*, independent
of which run discovered which album. This is what makes staggering safe: an
artist doesn't need to be checked today for their album to still show up in
today's report, as long as it was found on some past run and its release date
is still inside the window.

---

## 3.5 Required refactor: unify 429 handling across GET and POST

**This is required, not optional.** As drafted below, `add_tracks_to_playlist`
uses a separate raw `requests.post()` call with no Retry-After handling and no
long-wait-exit behavior. If the playlist-add endpoint gets a long 429, the
script would just log an exception and keep processing — continuing to hit
other endpoints while still inside a lockout window. Per the earlier research,
that's the exact pattern ("didn't have mechanisms to prevent requesting after
a 429... skyrocketed the rate limit to almost 48 hours") that turns a normal
lockout into a much longer one. Every code path that hits Spotify — GET or
POST — needs to share the same handling.

Refactor `spotify_get(token, url, params)` into a general
`spotify_request(token, method, url, state, params=None, json_body=None,
retries=5, backoff=1)`. `spotify_get` becomes a thin wrapper:

```python
def spotify_get(token, url, state, params=None):
    return spotify_request(token, "GET", url, state, params=params)
```

`spotify_request` keeps the existing GET logic (rate limiter, retries,
debug logging on ≥400) but adds a `json=json_body` body for POST, and — this
is the important part — the 429 branch now decides between a short in-process
sleep-and-retry (same as today) and a long-wait exit based on a threshold:

```python
LONG_WAIT_THRESHOLD_SECONDS = 300  # tune as needed; anything longer isn't worth blocking a job for

def spotify_request(token, method, url, state, params=None, json_body=None, retries=5, backoff=1):
    rate_limiter.wait_if_needed()
    headers = {"Authorization": f"Bearer {token}"}
    if method == "GET":
        resp = requests.get(url, headers=headers, params=params)
    else:
        resp = requests.post(url, headers=headers, json=json_body)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", backoff))
        if retry_after > LONG_WAIT_THRESHOLD_SECONDS:
            retry_until = int(time.time()) + retry_after
            if state.get("in_progress") is not None:
                state["in_progress"]["retry_after"] = retry_until
            save_state(state)
            hours, minutes = retry_after // 3600, (retry_after % 3600) // 60
            log(f"  Rate limited on {method} {url}. Saving state and exiting "
                f"after {hours}h {minutes}m ({retry_after}s).")
            sys.exit(2)
        if retries <= 0:
            raise Exception("Rate limited - max retries exceeded")
        wait = max(retry_after, backoff)
        log(f"  Rate limited on {method} {url}. Waiting {wait}s...")
        time.sleep(wait)
        return spotify_request(token, method, url, state, params, json_body, retries - 1, wait * 2)

    if resp.status_code >= 400:
        print(f"  [DEBUG] {resp.status_code} response headers: {dict(resp.headers)}")
        print(f"  [DEBUG] {resp.status_code} response body: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json()
```

Every existing caller (`get_followed_artists`, `get_artist_albums`,
`search_artist_albums`) needs `state` added to its signature and threaded
through to `spotify_get`/`spotify_request` — not just the new playlist
functions. This is the one place where the plan touches code paths that
predate this whole feature; treat it as a required, low-risk mechanical change
(add a parameter, pass it through) rather than a behavior change to those
functions.

---

## 3.6 Playlist functions

### `get_album_track_uris(token, album_id, state)`

Paginates `GET /albums/{id}/tracks` (max 50 per page) and returns Spotify
track URIs, which is what the playlist-items endpoint requires (album URIs
don't work there — it needs individual tracks).

```python
def get_album_track_uris(token, album_id, state):
    uris = []
    url = f"{SPOTIFY_API_BASE}/albums/{album_id}/tracks"
    limit = 50
    offset = 0
    while True:
        data = spotify_get(token, url, state, {"limit": limit, "offset": offset})
        items = data.get("items", [])
        if not items:
            break
        uris.extend(item["uri"] for item in items)
        if len(items) < limit:
            break
        offset += limit
    return uris
```

### `add_tracks_to_playlist(token, playlist_id, track_uris, state)`

Spotify caps `POST /playlists/{id}/tracks` at 100 URIs per call, so chunk it.
Routes through `spotify_request` (see 3.5) instead of a raw `requests.post`,
so a 429 here gets the exact same short-wait-retry vs. long-wait-exit
treatment as every other call in the script.

```python
def add_tracks_to_playlist(token, playlist_id, track_uris, state):
    url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks"
    for i in range(0, len(track_uris), 100):
        chunk = track_uris[i:i + 100]
        spotify_request(token, "POST", url, state, json_body={"uris": chunk})
```

---

## 4. Changes to `main()`

```
state = load_state()

if args.resume and state["in_progress"] is not None:
    ip = state["in_progress"]
    processed_ids = set(ip["processed_ids"])
    due_ids = ip["due_ids"]
    due_artists = [a for a in artists if a["id"] in due_ids]
    retry_after = ip["retry_after"]
    # existing retry_after-in-the-future exit check: unchanged in spirit,
    # just reads/writes state["in_progress"] instead of the old progress file
else:
    due_artists = get_due_artists(artists, state, args.interval_days)
    processed_ids = set()
    state["in_progress"] = {
        "due_ids": [a["id"] for a in due_artists],
        "processed_ids": [],
        "retry_after": None,
    }
    save_state(state)

log(f"{len(due_artists)}/{len(artists)} artists due for a check "
    f"(interval: {args.interval_days}d)")

now_iso = datetime.now(timezone.utc).isoformat()

for i, artist in enumerate(due_artists, 1):
    if artist["id"] in processed_ids:
        continue
    ... fetch albums exactly as today ...
    for album in albums:
        ... same album_type / artist_id filtering as today ...
        if (release_date and release_date >= cutoff) or not release_date:
            is_new = album["id"] not in state["known_albums"]
            record_album(state, artist, album, now_iso)
            if is_new and PLAYLIST_ID:
                try:
                    track_uris = get_album_track_uris(token, album["id"], state)
                    add_tracks_to_playlist(token, PLAYLIST_ID, track_uris, state)
                    state["known_albums"][album["id"]]["added_to_playlist"] = True
                    log(f"      Added {len(track_uris)} track(s) from "
                        f"'{album['name']}' to playlist")
                except Exception as e:
                    state["known_albums"][album["id"]]["added_to_playlist"] = False
                    log(f"      ERROR adding '{album['name']}' to playlist: {e}")
                    # don't abort the run over a playlist failure — album stays
                    # recorded either way; see Step 9 for retry handling
                    #
                    # Note: this only catches short-retry-exhausted errors and
                    # other genuine exceptions. A long-wait 429 inside
                    # get_album_track_uris or add_tracks_to_playlist calls
                    # sys.exit(2), which raises SystemExit — not a subclass of
                    # Exception — so it correctly propagates past this
                    # try/except and terminates the run, same as any other
                    # long-wait exit. Do not change this to `except
                    # BaseException` or add a `SystemExit` handler here.

    state["artists"][artist["id"]] = {"name": artist["name"], "last_checked": now_iso}
    processed_ids.add(artist["id"])
    state["in_progress"]["processed_ids"] = list(processed_ids)
    save_state(state)

state["in_progress"] = None
save_state(state)

report_albums = get_report_albums(state, args.days)
# see Step 6
```

Key points:
- `cutoff` (the `--days` release-date window) only decides whether an album is
  worth recording into `known_albums` at all — it is *not* what decides which
  artists get checked. Interval-based due-ness and release-date-based
  reporting stay fully decoupled.
- The 429/long-wait path: when `spotify_get()` hits a 429 with a long
  `Retry-After`, it should now set `state["in_progress"]["retry_after"]`,
  `save_state(state)`, then exit — same idea as before, just against the new
  single state object instead of a separate progress cache.
- `state["in_progress"] = None` only gets set once the whole due-list for the
  day finishes cleanly. If the run exits early (long wait, crash, cancelled
  job), `in_progress` stays populated with the frozen `due_ids`, so the next
  `--resume` run continues the exact same subset rather than recomputing a
  different one mid-week.

---

## 5. Handling unfollowed artists (optional cleanup)

`state["artists"]` will accumulate entries for artists you've since
unfollowed. Harmless, just grows the file slightly. Safe to skip initially;
if you want it, run once per successful full pass (not per-artist):

```python
current_ids = {a["id"] for a in artists}
state["artists"] = {aid: e for aid, e in state["artists"].items() if aid in current_ids}
```

---

## 6. Report output

Confirmed missing per Step 0.1 → add at the end of `main()`:

```python
report_albums = get_report_albums(state, args.days)

if args.json:
    print(json.dumps(report_albums, indent=2))
else:
    print(format_markdown_table(report_albums))

summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
if summary_path:
    with open(summary_path, "a") as f:
        f.write(format_markdown_table(report_albums))
```

**Design assumption (flag if wrong):** this plan keeps the human-readable
report as console/step-summary output only, separate from `spotify-state.json`
(which is machine state, not meant to be read directly). If you'd rather also
commit a persistent `RECENT_ALBUMS.md` file to the repo so the report itself
has history too, that's a small addition to Step 7's commit step — say the
word and I'll fold it in.

---

## 7. Workflow YAML changes (`.github/workflows/spotify-recent-albums.yml`)

Remove the cache restore/save steps entirely — `actions/checkout` already
pulls `spotify-state.json` from the branch, and a commit+push step at the end
replaces both the "save progress cache" and "clear progress cache on success"
steps.

```yaml
name: Spotify Recent Albums

on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:
    inputs:
      days:
        description: "Look back N days"
        required: false
        default: "365"

permissions:
  contents: write

concurrency:
  group: spotify-recent-albums
  cancel-in-progress: false

jobs:
  fetch-albums:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: python -m pip install requests

      - name: Fetch recent albums
        env:
          SPOTIFY_CLIENT_ID: ${{ secrets.SPOTIFY_CLIENT_ID }}
          SPOTIFY_CLIENT_SECRET: ${{ secrets.SPOTIFY_CLIENT_SECRET }}
          SPOTIFY_REFRESH_TOKEN: ${{ secrets.SPOTIFY_REFRESH_TOKEN }}
          SPOTIFY_PLAYLIST_ID: ${{ secrets.SPOTIFY_PLAYLIST_ID }}
        run: |
          DAYS=${{ github.event.inputs.days || '365' }}
          PYTHONUNBUFFERED=1 python -u spotify-recent-albums.py --resume --days "$DAYS"

      - name: Commit updated state
        if: always()
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add spotify-state.json
          git diff --staged --quiet || git commit -m "Update Spotify tracking state [skip ci]"
          git pull --rebase
          git push
```

Notes:
- `permissions: contents: write` is required for the default `GITHUB_TOKEN` to
  push. If branch protection blocks even that, use a PAT stored as a secret
  and check it out / push with that instead.
- `concurrency` prevents a manual `workflow_dispatch` run from racing a
  scheduled run and creating a push conflict; `cancel-in-progress: false`
  means a second trigger queues instead of killing an in-flight run (you
  don't want to cancel a run that might be mid-way through committing state).
- `if: always()` on the commit step is what makes the early-exit-on-long-wait
  path durable — whatever's on disk (including a freshly-set `in_progress`
  with a future `retry_after`) gets committed even though the script itself
  exited non-zero.
- `git pull --rebase` before push guards against the rare case where another
  commit landed on the branch between checkout and push (e.g. an unrelated
  human commit during the job). Given the concurrency group, this should
  rarely trigger for the workflow's own runs.
- `[skip ci]` in the commit message avoids retriggering other workflows that
  might listen for pushes to this branch; drop it if not needed.

---

## 8. Testing plan

1. **Unit-level, no network**: fake `artists` list + fake `state` dict,
   assert `get_due_artists()` returns:
   - everything, when `state["artists"]` is empty (first-ever run)
   - only artists past the interval, when some have recent `last_checked`
   - a newly-followed artist (no entry) even when others are fresh
2. **`--test-id` sanity check**: unaffected by this change, confirms
   `get_artist_albums` still works standalone.
3. **Interval smoke test**: run locally with a tiny `--interval-days`,
   confirm every artist is due; run again immediately after, confirm the
   second run's due count is ~0.
4. **Resume test**: kill the script mid-loop, confirm `spotify-state.json` has
   `in_progress.processed_ids` and `in_progress.due_ids` populated on disk,
   rerun with `--resume`, confirm it continues the same subset.
5. **Report correctness test**: manually seed `known_albums` with an album
   inside the `--days` window but whose artist's `last_checked` is old
   (simulating "found on a past run, not rechecked today") — confirm it still
   appears in `get_report_albums()`. This is the core case the redesign
   exists to protect.
6. **Commit step test**: run the workflow once via `workflow_dispatch` on a
   branch, confirm exactly one new commit appears touching only
   `spotify-state.json`, and that a second immediate manual run produces a
   commit with a much smaller diff (only newly-due artists' `last_checked`
   values changed, plus any new albums).
7. **Real quota test**: watch 2-3 consecutive scheduled runs in Actions,
   confirm the `X/82 artists due for a check` log line stays in the target
   range (e.g. ~12 with a 7-day interval) and no 429/quota lockout recurs. If
   it still trips, lower `--interval-days` further.

---

## 9. Playlist integration notes

### First-run backfill risk
`known_albums` starts empty, so the very first run with playlist-adding
enabled will treat *every* album inside the `--days` window as "new" across
all 82 artists — potentially dozens of albums, each triggering a track-lookup
call plus a playlist-add call, all in one run. That's a lot of extra request
volume concentrated in a single run, right at rollout, against endpoints that
haven't been exercised before (so their quota behavior is untested).

Recommended rollout order:
1. Deploy the staggering + state-file changes **without** `SPOTIFY_PLAYLIST_ID`
   set (playlist code no-ops when `PLAYLIST_ID` is `None`). Let it run for a
   full cycle (`CHECK_INTERVAL_DAYS`, e.g. 7 days) so `known_albums` gets
   fully populated with your current backlog, with zero playlist calls.
2. Once `known_albums` reflects your real current library, set
   `SPOTIFY_PLAYLIST_ID`. From that point on, "new" genuinely means
   newly-released, so playlist-add volume drops to whatever your artists
   actually release — a handful a week, not a backlog dump.

### Idempotency / retry handling
Because `record_album()` runs before the playlist-add attempt, a failed
playlist-add still leaves the album in `known_albums` with
`added_to_playlist: false` — which means on the *next* time that same album is
encountered, `is_new` will be `False` (it's already in `known_albums`) and the
retry branch above will never re-fire as written. To actually retry failed
adds, change the gating condition from `is_new` to:

```python
existing_entry = state["known_albums"].get(album["id"])
needs_playlist_add = existing_entry is None or not existing_entry.get("added_to_playlist", False)
record_album(state, artist, album, now_iso)
if needs_playlist_add and PLAYLIST_ID:
    ...
```

This way, an album that was recorded but never successfully added (e.g. the
run got rate-limited on the playlist endpoint right after) gets picked back up
the next time its artist happens to be checked again — not lost silently.

---

## 10. Rollback plan

If this needs to be reverted: delete `spotify-state.json` from the repo (or
just stop reading it — `load_state()` degrades to an empty state, which is
equivalent to "every artist due," i.e. the old always-check-everyone
behavior, not a crash) and revert the workflow file to restore the old cache
steps if desired. No destructive migration is required either direction since
the new state format doesn't overwrite or depend on the old cache files.