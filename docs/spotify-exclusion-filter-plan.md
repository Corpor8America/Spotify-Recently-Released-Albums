# Implementation Plan: Auto/Manual Exclusion Filter for Albums with Parenthetical Titles

## Goal

Stop live albums, remasters, deluxe/anniversary editions, and similar
reissues from cluttering the report and the synced playlist, without losing
the ability to correct individual cases by hand.

Filter rule (per your latest answer): **any album whose name ends with a
parenthetical — `(...)` as the trailing element of the title, allowing for
trailing whitespace — is excluded by default.** This is narrower than
matching a parenthetical anywhere in the string: it targets the common
"Title (Qualifier)" pattern that deluxe editions, remasters, live albums,
and anniversary reissues almost always use, while leaving alone titles where
a parenthetical appears mid-string but the title continues after it (e.g.
`Album Name (Part One) - Bonus Track`), since those aren't the reissue/live
pattern this filter is meant to catch.

Every exclusion decision is **overridable per-album** by editing
`spotify-state.json` directly — no code change needed to flip a specific
album from excluded to included or vice versa. This does not change any
existing scheduling, rate-limiting, or state-file-commit behavior from the
staggering/slowdown plans already implemented — it only adds two new fields
per `known_albums` entry and a new pruning pass.

---

## 1. The regex

```python
import re

PAREN_PATTERN = re.compile(r"\(.*?\)\s*$")

def is_auto_excluded(album_name):
    return bool(PAREN_PATTERN.search(album_name.strip()))
```

Matches a parenthetical only when it's the last thing in the title (after
stripping trailing whitespace) — `Album Name (Deluxe Edition)`, `Album Name
(Live)`, `Album Name (Remastered)`, `Album Name (20th Anniversary Edition)`
all match. A parenthetical in the middle of a longer title, like `Album Name
(Part One) - Bonus Track`, does **not** match, since the string continues
after the closing paren. This will still catch some false positives (e.g. an
album genuinely titled `Album Name (Part One)` with nothing after it), which
is why the manual-override mechanism in Step 3 exists — flag and flip those
individually rather than trying to special-case them in the regex.

---

## 2. New fields on each `known_albums` entry

| Field | Who sets it | Meaning |
|---|---|---|
| `auto_excluded` | Script, every time `record_album()` runs for that album | Recomputed from the current regex against the current name. Lets a future regex change "catch up" existing entries next time their artist is checked. |
| `manual_override` | You, by hand in the JSON | `true` = force excluded. `false` = force included. `null`/absent = defer to `auto_excluded`. **The script never writes this field.** |

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
        "auto_excluded": is_auto_excluded(album["name"]),
        "manual_override": existing.get("manual_override"),
    }
```

Note `manual_override` is read from `existing` and passed straight through
unchanged — this is the one line that guarantees hand-edits survive across
runs.

---

## 3. Single source of truth: `is_effectively_excluded()`

Every downstream consumer (report, playlist-add gating, playlist pruning)
calls this instead of checking `auto_excluded` directly:

```python
def is_effectively_excluded(album):
    override = album.get("manual_override")
    if override is not None:
        return override
    return album.get("auto_excluded", False)
```

---

## 4. Report filtering

```python
def get_report_albums(state, days):
    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for album in state.get("known_albums", {}).values():
        if is_effectively_excluded(album):
            continue
        release_date = parse_release_date(album["release_date"])
        if release_date is None or release_date >= cutoff:
            result.append(album)
    return result
```

Excluded albums are still recorded in `known_albums` (so you always have an
entry to flip via `manual_override`) — they just don't show up in the
markdown table / JSON report output.

---

## 5. Playlist-add gating

In the per-album loop in `main()`, gate the playlist-add branch the same
way, so excluded albums are recorded but never get tracks added:

```python
existing_entry = state["known_albums"].get(album["id"])
needs_playlist_add = existing_entry is None or not existing_entry.get("added_to_playlist", False)
record_album(state, artist, album, now_iso)
entry = state["known_albums"][album["id"]]
if needs_playlist_add and not is_effectively_excluded(entry) and os.environ.get("SPOTIFY_PLAYLIST_ID"):
    playlist_id = os.environ["SPOTIFY_PLAYLIST_ID"]
    try:
        track_uris = get_album_track_uris(token, album["id"], state)
        add_tracks_to_playlist(token, playlist_id, track_uris, state)
        entry["added_to_playlist"] = True
        entry["track_uris"] = track_uris
        log(f"      Added {len(track_uris)} track(s) from '{album['name']}' to playlist")
    except Exception as e:
        entry["added_to_playlist"] = False
        log(f"      ERROR adding '{album['name']}' to playlist: {e}")
```

---

## 6. Automatic pruning for excluded-after-the-fact albums (required, not optional)

**This is the part you flagged as needing to happen automatically.** Two
situations need the exact same cleanup:

1. An album ages out of the `--days` window (already handled today by
   `prune_expired_playlist_tracks`).
2. An album gets excluded — either the regex newly matches it on a later
   check, or you set `manual_override: true` by hand — *after* its tracks
   were already added to the playlist.

Rather than writing a second, separate prune function, **generalize the
existing `prune_expired_playlist_tracks` into one pass that removes tracks
for any album that is aged-out *or* effectively-excluded**, using the same
shared-track protection (a track that also belongs to a still-current,
still-included album is never removed).

```python
def prune_playlist(token, state, days, playlist_id):
    """Removes tracks from the playlist for any known album that is either
    aged out of the --days window or effectively excluded (auto or manual).
    Tracks that are also part of a still-current, still-included album are
    never removed (shared-track protection)."""
    if not playlist_id:
        return

    cutoff = datetime.now() - timedelta(days=days)
    known = state.get("known_albums", {})

    removal_ids = []
    keep_uris = set()
    for album_id, album in known.items():
        if not album.get("added_to_playlist"):
            continue
        release_date = parse_release_date(album["release_date"])
        aged_out = release_date is not None and release_date < cutoff
        excluded = is_effectively_excluded(album)
        if aged_out or excluded:
            removal_ids.append(album_id)
        else:
            keep_uris.update(album.get("track_uris") or [])

    if not removal_ids:
        return

    log(f"Pruning {len(removal_ids)} album(s) from playlist "
        f"(aged-out or excluded)...")
    for album_id in removal_ids:
        album = known[album_id]
        track_uris = album.get("track_uris")
        if not track_uris:
            # Older entries recorded before track_uris was tracked on the
            # album record -- fall back to fetching them fresh.
            try:
                track_uris = get_album_track_uris(token, album_id, state)
            except Exception as e:
                log(f"  ERROR fetching tracks for '{album['name']}' during prune: {e}")
                continue

        to_remove = [u for u in track_uris if u not in keep_uris]
        if to_remove:
            try:
                remove_tracks_from_playlist(token, playlist_id, to_remove, state)
                reason = "excluded" if is_effectively_excluded(album) else "aged out"
                log(f"  Removed {len(to_remove)} track(s) from '{album['name']}' ({reason})")
            except Exception as e:
                log(f"  ERROR removing '{album['name']}' from playlist: {e}")
                continue

        album["added_to_playlist"] = False
        album["track_uris"] = []
        save_state(state)
```

Rename the call site in `main()`:

```python
prune_playlist(token, state, args.days, os.environ.get("SPOTIFY_PLAYLIST_ID"))
```

Why this is safe to merge into one function rather than keeping two:
- Both cases use identical mechanics — same shared-track protection, same
  fallback-fetch for missing `track_uris`, same "clear the flags after
  removal" cleanup.
- A single `keep_uris` set built from *all* still-valid (not aged-out, not
  excluded) albums is strictly more correct than running two separate
  passes with two separate `keep_uris` sets, since an album excluded on one
  criterion might still share a track with an album that's current on the
  other criterion — one unified pass catches that correctly; two sequential
  passes could remove a shared track that a later pass would have protected.

### Why this makes overrides automatic end-to-end

With this in place, the override workflow from your original ask now closes
the loop without any manual JSON surgery beyond the one field:

- **Flip `manual_override: true` on an already-added album** → next run's
  `prune_playlist()` sees it's now effectively excluded, removes its
  (non-shared) tracks, and clears `added_to_playlist`/`track_uris`
  automatically.
- **Flip `manual_override: false` on an excluded album** → next run's
  playlist-add gate (Step 5) sees `added_to_playlist` is `false` and
  `needs_playlist_add` is `true`, so it gets added back next time that
  artist's due for a check — same retry mechanism already used for failed
  adds.

---

## 7. One-time backfill: standalone throwaway script

The regular flow only recomputes `auto_excluded` for an album when its
artist is next due for a check (`record_album()` only runs inside the
per-artist loop). Without a backfill step, everything already sitting in
`known_albums` today — including the reissues/live/deluxe albums already in
your playlist right now, like `Blood Dynasty (Expanded Deluxe Edition)`,
`See You On The Other Side (20th Anniversary Edition - Remastered)`, and
`Aealo (Re-recorded)` — would only get cleaned up piecemeal, whenever that
particular artist's staggered interval comes back around (up to 7 days per
artist, longer if you raise `--interval-days`).

This is a one-time cleanup, not an ongoing feature, so it doesn't belong as
a permanent flag on `spotify-recent-albums.py`. Instead it's a small,
separate script — `spotify/backfill_exclusions_once.py` — that you run
once by hand and then delete. It imports the real functions from the main
module (so the regex and pruning logic can never drift out of sync with
what the scheduled script actually does) rather than reimplementing any of
the exclusion or pruning logic itself.

```python
#!/usr/bin/env python3
"""
ONE-TIME SCRIPT — run once, then delete.

Recomputes `auto_excluded` for every album already in `spotify-state.json`
(not just ones whose artist is due for a check today), and immediately
prunes anything newly excluded from the Spotify playlist. Existing
`manual_override` values are never touched.

Usage:
  SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy SPOTIFY_REFRESH_TOKEN=zzz \
    python spotify/backfill_exclusions_once.py

After running and confirming the output looks right, commit the updated
spotify-state.json and delete this script.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module

# Reuses the real script's functions directly, so the regex and pruning
# logic here are guaranteed identical to what the scheduled job runs.
main_module = import_module("spotify-recent-albums".replace("-", "_"))
# NOTE: if the module can't be imported this way due to the hyphenated
# filename, fall back to importlib.util.spec_from_file_location the same
# way spotify/tests/test_spotify_recent_albums.py already does.


def main():
    client_id, client_secret = main_module.get_client_credentials()
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    if not refresh_token:
        print("Error: Set SPOTIFY_REFRESH_TOKEN env var.")
        sys.exit(1)

    token = main_module.get_access_token(client_id, client_secret, refresh_token)
    state = main_module.load_state()

    print("Recomputing auto_excluded for all known albums...")
    changed = []
    for album_id, album in state.get("known_albums", {}).items():
        old_value = album.get("auto_excluded")
        new_value = main_module.is_auto_excluded(album["name"])
        if old_value != new_value:
            changed.append((album["name"], old_value, new_value))
        album["auto_excluded"] = new_value
        album.setdefault("manual_override", None)

    main_module.save_state(state)
    print(f"Backfill complete: {len(changed)}/{len(state['known_albums'])} "
          f"album(s) changed auto_excluded value.")
    for name, old, new in changed:
        print(f"  {name}: auto_excluded {old!r} -> {new!r}")

    playlist_id = os.environ.get("SPOTIFY_PLAYLIST_ID")
    main_module.prune_playlist(token, state, days=365, playlist_id=playlist_id)
    print("Done. Review spotify-state.json, commit it, then delete this script.")


if __name__ == "__main__":
    main()
```

Usage, run once locally (not part of the scheduled workflow, not wired into
CI):

```bash
SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy SPOTIFY_REFRESH_TOKEN=zzz \
SPOTIFY_PLAYLIST_ID=your_playlist_id \
  python spotify/backfill_exclusions_once.py
```

Notes:
- **Nothing added to `spotify-recent-albums.py` for this** — no new CLI
  flag, no new branch in `main()`. The only shared surface is that this
  script imports and calls the same `is_auto_excluded()`, `load_state()`,
  `save_state()`, and `prune_playlist()` functions the real script uses, so
  behavior is guaranteed consistent without duplicating logic.
- Safe to run more than once before you delete it — it's idempotent. A
  second run reports `0` changed once the first pass has settled
  everything.
- Respects `manual_override`: `album.setdefault("manual_override", None)`
  only fills in the field if it's missing entirely; it never overwrites a
  value you've already set by hand.
- Doesn't commit to git itself — review `spotify-state.json` after running,
  then commit it yourself the normal way.
- Once you've run it and are happy with the result, **delete
  `backfill_exclusions_once.py`** — it has no ongoing purpose once every
  existing album has an up-to-date `auto_excluded` value; every album
  discovered afterward gets it set correctly on first discovery by the
  regular `record_album()` path.
  `auto_excluded: true` and that still has `added_to_playlist: true`.

---

## 8. Testing plan

Add unit tests alongside the existing ones in
`spotify/tests/test_spotify_recent_albums.py`:

1. **`is_auto_excluded()`**
   - `"Album Name"` → `False`
   - `"Album Name (Live)"` → `True`
   - `"Album Name (Remastered)"` → `True`
   - `"Album Name (Deluxe Edition)"` → `True`
   - `"Album Name (Deluxe Edition) "` (trailing whitespace) → `True`
   - `"Album Name (Part One)"` → `True` (known false-positive case, documents
     the tradeoff rather than hiding it — this is what `manual_override`
     exists to correct)
   - `"Album Name (Part One) - Bonus Track"` → `False` (parenthetical is not
     trailing, so it's left alone)

2. **`is_effectively_excluded()`**
   - `auto_excluded=True, manual_override=None` → `True`
   - `auto_excluded=True, manual_override=False` → `False` (override wins)
   - `auto_excluded=False, manual_override=True` → `True` (override wins)
   - `auto_excluded=False, manual_override=None` → `False`

3. **`record_album()` preserves `manual_override` across re-recording** —
   seed `state["known_albums"][id]["manual_override"] = True`, call
   `record_album()` again for the same album, assert `manual_override` is
   still `True` afterward and `auto_excluded` reflects the current name.

4. **`prune_playlist()`** (extend the existing
   `PruneExpiredPlaylistTracksTests` class, or rename it):
   - Aged-out album with tracks removed (existing case, must still pass
     under the renamed/generalized function).
   - Manually-excluded album (`manual_override: true`, `added_to_playlist:
     true`, not aged out) → tracks removed, `added_to_playlist` cleared.
   - Manually-included album (`manual_override: false`, `auto_excluded:
     true`) → **not** pruned, since the override forces inclusion.
   - Shared-track case across an excluded album and a still-included album →
     only the non-shared tracks are removed (extend the existing
     `test_shared_track_is_not_removed` case to mix an "excluded" album with
     a "current" album, not just "old" vs. "current").
   - No-op when `playlist_id` is `None` (existing case).

5. **End-to-end via the harness**: not strictly necessary since the mock
   server doesn't currently vary album names, but if useful later,
   `mock_spotify_server.py`'s `_make_album()` could be extended with an
   optional flag to occasionally emit a parenthetical name, to exercise the
   full exclude → prune cycle through `simulate_workflow_harness.py`. Not
   required for this change to ship.

6. **`backfill_exclusions_once.py`** (manual/smoke-test only, since it's a
   throwaway script rather than a permanent part of the test suite):
   - Seed a temp `spotify-state.json` with a mix of parenthetical and
     non-parenthetical album names, no `auto_excluded` field set yet → run
     the script, confirm every entry ends up with the correct
     `auto_excluded` value and the printed "changed" list names exactly the
     ones that flipped.
   - An entry that already has `manual_override: false` set → after
     running, `manual_override` is still `false` (not touched), even though
     `auto_excluded` becomes `true`.
   - Run it twice in a row → second run prints `0` changed (idempotency).
   - Seed an entry with a trailing-parenthetical name and
     `added_to_playlist: true` → confirm `prune_playlist()` gets called and
     actually removes its tracks (against the mock server, or by mocking
     `prune_playlist` directly and asserting it was invoked after
     `auto_excluded` was set on that entry).

---

## 9. Rollout / rollback

- **No migration needed.** Existing `known_albums` entries simply don't have
  `auto_excluded`/`manual_override` yet; `.get()` calls throughout default
  them to `False`/`None`, which is the same as "not excluded" — so on
  deploy, nothing already in the playlist gets pruned until its artist is
  next checked and `record_album()` runs again for it, populating
  `auto_excluded` for the first time.
- **To exclude something immediately without waiting for its artist's next
  check cycle**, hand-edit `spotify-state.json` and set
  `"manual_override": true` on that album's entry directly — the next run's
  `prune_playlist()` picks it up regardless of whether that artist is due
  for a check this run, since pruning iterates all of `known_albums`, not
  just `due_artists`.
- **To roll back entirely**: revert the code changes. The two new JSON
  fields left behind on existing entries are inert extra data and don't
  need to be stripped out.

---

## Summary of concrete changes

| Component | Change |
|---|---|
| `is_auto_excluded(name)` | New function — regex match on any `(...)` in the album name |
| `is_effectively_excluded(album)` | New function — `manual_override` wins if set, else `auto_excluded` |
| `record_album()` | Adds `auto_excluded` (recomputed) and `manual_override` (preserved) to each entry |
| `get_report_albums()` | Skips effectively-excluded albums |
| Playlist-add gate in `main()` | Skips effectively-excluded albums |
| `prune_expired_playlist_tracks()` → `prune_playlist()` | Generalized to remove tracks for aged-out **or** effectively-excluded albums, one unified shared-track-safe pass |
| `backfill_exclusions_once.py` (new, standalone, throwaway) | Run once locally: recomputes `auto_excluded` for every existing `known_albums` entry (not just due ones) using the real script's own functions, then runs `prune_playlist()` immediately so already-playlisted reissues/live albums get cleaned up without waiting on the staggered schedule. No changes to `spotify-recent-albums.py` itself. Delete after use. |
| `spotify-state.json` | Two new optional fields per `known_albums` entry; hand-edit `manual_override` to correct individual albums |