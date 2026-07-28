# Turning "Recent Albums" into a Self-Updating Spotify Playlist

Based on your answers: **all tracks** from every recent album, **default Spotify ordering** (no manual sort), and **auto-remove** tracks once an album ages out of the 1-year window.

This doc lays out the approach — not full code — so you can decide before we touch the script.

---

## 1. The core problem

Your current script rebuilds the "recent albums" list from scratch every run and throws it away at the end (the progress cache is deleted on success). To sync a playlist, you need something the current script doesn't have:

1. **Track-level data.** You only fetch albums, not tracks. To add songs to a playlist you need track URIs, which means calling the album's tracks endpoint for every recent album.
2. **Persistent state across runs.** To know what to *remove*, you need to remember what was in the playlist last time and diff it against what's recent now. Right now nothing survives between successful runs.

Everything else is mostly bookkeeping around those two gaps.

---

## 2. Data model to add

For each album in `recent`, fetch its tracks and store URIs alongside the metadata you already collect:

```
GET /v1/albums/{album_id}/tracks   (paginate, limit=50)
```

Each recent album record becomes:

```json
{
  "album_id": "...",
  "artist": "...",
  "name": "...",
  "release_date": "...",
  "track_uris": ["spotify:track:...", "spotify:track:...", ...]
}
```

This is one extra API call per recent album (not per followed artist), so the volume is small — you're already filtering down to just the albums that passed the cutoff before you'd fetch tracks.

---

## 3. Persistent state file

Add a new file, e.g. `.spotify_playlist_state.json`, that survives **every** run (unlike the resume cache, which is deleted on success). It should store:

```json
{
  "playlist_id": "37i9...",
  "albums": {
    "album_id_1": ["spotify:track:...", "spotify:track:..."],
    "album_id_2": ["spotify:track:..."]
  }
}
```

This is the "what's currently in the playlist, and why" record. Two options for where it lives:

- **Committed to the repo** (recommended). After a successful run, `git commit` the updated state file back to the branch. Simple, durable, versioned, easy to inspect in PRs/history. No expiry risk.
- **GitHub Actions cache with a permanent key.** Reuses the pattern you already have for `.spotify_progress.json`, but caches are evicted after 7 days of no access and there's no guarantee of retention — riskier for something you want to treat as source of truth long-term.

Given this needs to persist indefinitely (some albums may sit in the window for a full year), committing it to the repo is the safer choice. The progress-cache mechanism you already have is fine to keep as-is for mid-run resume; this is a separate, longer-lived file.

---

## 4. The sync algorithm

Each run, after computing the new `recent` list (with track URIs attached):

```
new_state  = { album_id: track_uris for each album in recent }
old_state  = load .spotify_playlist_state.json (or empty if first run)

to_add     = tracks in new_state not present in old_state
to_remove  = tracks in old_state not present in new_state
```

Compute this as **sets of track URIs**, not album IDs, so:
- An album re-appearing with a deluxe/reissue that adds tracks only adds the *new* tracks.
- A track that happens to appear on two different recent albums (e.g. a reissue) is never removed while at least one qualifying album still contains it.

Then:

```
POST   /v1/playlists/{playlist_id}/tracks   (add to_add, chunks of 100 URIs)
DELETE /v1/playlists/{playlist_id}/tracks   (remove to_remove, chunks of 100 URIs)
```

Finally, write `new_state` over the old state file and commit it.

---

## 5. Ordering (per your answer: default order)

Since you don't need a specific sort:

- **Adding**: new tracks just get appended to the end of the playlist in whatever order you iterate albums/tracks. Simplest possible path — no re-sorting step needed.
- **Removing**: use the playlist-tracks-remove endpoint with just `{"uri": "..."}` objects (no `positions`). This removes **all occurrences** of that URI regardless of where it sits, which is simpler and avoids needing to track exact positions as the playlist shifts over time.

If you ever want ordering later (e.g. newest-first), that's a bigger lift — it means periodically fully reordering the playlist via `PUT /v1/playlists/{playlist_id}/tracks` with a complete ordered URI list, since Spotify has no "insert at position" for arbitrary sorting. Worth calling out now so it's a known tradeoff, not a surprise later.

---

## 6. Playlist creation (one-time)

```
POST /v1/users/{user_id}/playlists
{
  "name": "Recently Released Albums",
  "public": false,
  "description": "Auto-updated: full albums released by followed artists in the last year"
}
```

Do this once, by hand or via a `--create-playlist` flag on the script, and save the returned `playlist_id` into the state file. Everything after that is idempotent against the same playlist.

Your existing OAuth scopes (`playlist-modify-public playlist-modify-private`) already cover this — no re-auth needed.

---

## 7. Where this lives in your script/workflow

Two reasonable structures:

**A. Extend the existing script** with a `sync_playlist()` step that runs after the artist/album scan completes, using the in-memory `recent` list directly. Simplest — one script, one workflow run, no risk of the two getting out of sync.

**B. Separate script** (`spotify-sync-playlist.py`) that reads a persisted "recent albums" JSON output from the scan script and does only the playlist diffing. Cleaner separation of concerns, easier to test in isolation, but means the scan script also needs to persist its final `recent` output (currently it doesn't write it anywhere durable — only prints a markdown table).

Given you already have a resumable, multi-step scan, **A is the lower-friction path**: add track-fetching and playlist-sync as a final phase after the artist loop, gated on the scan having completed fully (i.e. `PROGRESS_CACHE` was cleared) so you don't diff against a partial scan.

---

## 8. Workflow (.yml) changes needed

- Add a step after "Fetch recent albums" to commit the updated `.spotify_playlist_state.json` back to the repo (`git add/commit/push`, or a bot-commit action), guarded by `if: success() && !cancelled()` — same as your existing cache-clear step.
- No new secrets needed beyond what you have, since playlist scopes are already requested.
- Consider caching `.spotify_playlist_state.json` too if you go the cache route instead of committing it (see §3).

### Permissions for committing back to the repo

The default `GITHUB_TOKEN` may only have read access depending on your repo/org settings. Grant write access explicitly:

```yaml
permissions:
  contents: write
```

Set a bot identity for the commit so it's clearly automated:

```yaml
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
```

`actions/checkout@v4` needs `persist-credentials: true` (the default) so the token can push afterward. Since this workflow triggers on `schedule`/`workflow_dispatch` (not `push`), a bot commit won't retrigger the workflow — no loop risk.

### Queuing concurrent runs

Because the script does long, resumable work with rate-limit waits (some retries can be 22+ hours), you don't want two runs stepping on each other — especially once a run is also writing `.spotify_playlist_state.json`. Add a `concurrency` block at the top level of the workflow (alongside `on:` and `jobs:`):

```yaml
concurrency:
  group: spotify-recent-albums
  cancel-in-progress: false
```

- **`group`**: runs sharing this string queue against each other instead of running in parallel. A fixed group means a manual `workflow_dispatch` won't overlap with the scheduled run.
- **`cancel-in-progress: false`**: a new trigger waits for the current run to finish rather than killing it mid-progress. (`true` would cancel the running job instead — wrong for this use case, since you'd lose in-progress rate-limit backoff state.)

Note: GitHub only lets one run wait per group — if triggered a third time while one run is active and one is already queued, the older queued run is replaced by the newest, not stacked. Rarely an issue for a daily cron, but worth knowing if this also gets triggered manually and often.

This also closes the race-condition concern from §3/state-file writing — with `cancel-in-progress: false` and a single queue group, only one run is ever writing `.spotify_playlist_state.json` at a time.

---

## 9. Rate limiting

Track-fetch calls (§2) and the add/remove playlist calls (§4) should go through the same `spotify_get`/rate limiter pattern you already have — just add equivalent `spotify_post`/`spotify_delete` helpers that share the same `RateLimiter` instance and the same 429/Retry-After handling. Volume-wise this adds a modest number of calls (tracks per recent album, plus a couple of add/remove batches), well within your existing 150/min budget.

---

## 10. Edge cases worth deciding on later (not blocking)

- **Local files / tracks unavailable in your market**: album tracks endpoint can return tracks with no playable URI in some markets — filter these out before adding.
- **Compilations/reissues sharing tracks**: handled naturally by the URI-set diff in §4.
- **Manual edits to the playlist**: if you ever manually remove a track Claude/the script added, the next run will re-add it, since the state file still thinks it belongs. If you want manual removals to "stick," you'd need an explicit exclude-list — flag if this matters to you.
- **First-time backfill**: the very first sync run will try to add every track from every currently-recent album at once (potentially hundreds of tracks) — fine, just expect a big first pass.

---

## Summary of concrete additions

| Component | What's new |
|---|---|
| `get_album_tracks(token, album_id)` | fetch track URIs per recent album |
| `.spotify_playlist_state.json` | persistent (committed) record of playlist_id + album→tracks currently reflected |
| `sync_playlist(token, recent, old_state)` | diff old vs new track sets, add/remove via playlist endpoints |
| `spotify_post` / `spotify_delete` helpers | mirror `spotify_get`'s rate-limit + retry handling |
| Workflow step | commit updated state file after a successful run |

This keeps your existing scan logic untouched and adds a self-contained sync phase at the end.
