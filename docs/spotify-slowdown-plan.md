# Implementation Plan: Drastically Slow Down `spotify-recent-albums.py`

## Context / why this exists

On a real run, the script processed 53/82 artists in **22 seconds** and then
hit a 429 with a **24-hour** `Retry-After`. Two things point to this being a
**daily request-count quota** (Spotify dev-mode apps get a low daily cap),
not a per-second/per-minute rate:

- 22 seconds for ~53-100 requests is nowhere near the existing
  `MAX_REQUESTS_PER_MINUTE = 120` burst ceiling — the in-script rate limiter
  never engaged at all.
- The 429 came back with `Retry-After: 86400` (24h), which is the signature
  of a daily quota reset, not a short per-minute cooldown.

**Implication:** slowing down the *pace* of requests (adding delays) is good
insurance but will **not by itself** prevent tripping a daily total-count
quota. The primary fix has to be **sending fewer total requests per run**.
This plan does both:

1. **(Primary fix) Cap how many artists are processed per run**, so a full
   82-artist cycle is spread across many runs instead of attempted in one.
2. **(Secondary/insurance) Add a flat minimum delay between every API call**,
   in case a per-minute/per-second limit is also a factor.

Both are exposed as CLI flags with conservative defaults, so the user can
turn things back up gradually later without touching code.

Do not change the existing `RateLimiter` per-minute burst logic, the
`spotify_request` 429/long-wait/short-wait branching, or the
`LONG_WAIT_THRESHOLD_SECONDS` / `sys.exit(2)` behavior. Those already work
correctly (see `spotify/docs/spotify-staggered-plan.md`, already
implemented) — this plan only adds an additional slowdown layer on top.

## Already done (do not redo)

The following env-var overrides were added to `spotify-recent-albums.py` to
support the test harness in `spotify/tests/` and should be left as-is:

```python
SPOTIFY_AUTH_URL = os.environ.get("SPOTIFY_AUTH_URL_OVERRIDE", "https://accounts.spotify.com/authorize")
SPOTIFY_TOKEN_URL = os.environ.get("SPOTIFY_TOKEN_URL_OVERRIDE", "https://accounts.spotify.com/api/token")
SPOTIFY_API_BASE = os.environ.get("SPOTIFY_API_BASE_OVERRIDE", "https://api.spotify.com/v1")
```

These default to the real Spotify endpoints, so production behavior is
unchanged unless the override env vars are explicitly set (which only the
test harness does).

---

## 1. New config constants

```python
DEFAULT_MAX_ARTISTS_PER_RUN = 5          # artists processed per invocation
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 20  # flat floor between any two Spotify API calls
```

## 2. New CLI flags (in `main()`'s `argparse` setup)

```python
parser.add_argument("--batch-size", type=int, default=DEFAULT_MAX_ARTISTS_PER_RUN,
                    help=f"Max artists to process in a single run (default: {DEFAULT_MAX_ARTISTS_PER_RUN}). "
                         f"Remaining due artists roll over to the next run automatically.")
parser.add_argument("--min-request-interval", type=float, default=DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
                    help=f"Minimum seconds between any two Spotify API calls (default: {DEFAULT_MIN_REQUEST_INTERVAL_SECONDS}). "
                         f"Set to 0 to disable.")
```

## 3. `RateLimiter`: add a flat minimum-interval floor

Replace the class with a version that enforces a minimum gap between
*every* request, in addition to the existing rolling-window burst check:

```python
class RateLimiter:
    def __init__(self, max_requests, min_interval_seconds=0):
        self.max_requests = max_requests
        self.min_interval_seconds = min_interval_seconds
        self.timestamps = []
        self.last_request_time = None

    def wait_if_needed(self):
        now = time.time()

        if self.min_interval_seconds and self.last_request_time is not None:
            elapsed = now - self.last_request_time
            if elapsed < self.min_interval_seconds:
                sleep_time = self.min_interval_seconds - elapsed
                print(f"  Rate limiter: waiting {sleep_time:.1f}s (min {self.min_interval_seconds}s between requests)")
                time.sleep(sleep_time)
                now = time.time()

        self.timestamps = [t for t in self.timestamps if now - t < 60]
        if len(self.timestamps) >= self.max_requests:
            sleep_time = 60 - (now - self.timestamps[0]) + 0.1
            if sleep_time > 0:
                print(f"  Rate limiter: waiting {sleep_time:.1f}s (hit {self.max_requests}/min limit)")
                time.sleep(sleep_time)

        self.last_request_time = time.time()
        self.timestamps.append(self.last_request_time)
```

The module-level `rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE)`
instantiation needs `min_interval_seconds` set before use. Since it's
currently created at import time (before `args` is parsed), the simplest
correct fix is to set the attribute directly in `main()` right after parsing
args, rather than trying to pass it through the constructor at import time:

```python
def main():
    parser = argparse.ArgumentParser(...)
    ...
    args = parser.parse_args()
    rate_limiter.min_interval_seconds = args.min_request_interval
    ...
```

(Leave the module-level `rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE)`
line as-is; just set `.min_interval_seconds` after parsing args, before any
requests are made.)

## 4. Cap `due_artists` to `--batch-size` in `main()`

This is the primary fix. Wherever `due_artists` is computed (both the fresh
run branch and the `--resume` branch), slice it down to at most
`args.batch_size` **new** artists this run — but be careful with the resume
case, since `due_artists` there already represents a frozen subset from a
previous run's `in_progress.due_ids`.

**Fresh run branch** — slice right after computing due artists, *before*
writing `in_progress` to state, so `in_progress.due_ids` reflects only what
this run will actually attempt:

```python
else:
    all_due_artists = get_due_artists(artists, state, args.interval_days)
    due_artists = all_due_artists[:args.batch_size]
    processed_ids = set()
    state["in_progress"] = {
        "due_ids": [a["id"] for a in due_artists],
        "processed_ids": [],
        "retry_after": None,
    }
    save_state(state)
    log(f"{len(due_artists)}/{len(all_due_artists)} due artists selected for this run "
        f"(batch size: {args.batch_size}, {len(all_due_artists) - len(due_artists)} remaining artists "
        f"will be picked up by a future run)")
```

Important: artists left out of this batch are **not** marked as checked
(their `state["artists"][id]["last_checked"]` is untouched), so
`get_due_artists()` will naturally still consider them due on the very next
run — no extra bookkeeping needed to "carry over" the remainder. Over
several runs, the full rotation still completes; it just takes
`ceil(len(all_due_artists) / batch_size)` runs instead of 1.

**Resume branch**: leave as-is. `in_progress.due_ids` was already frozen at
batch size (or smaller) by the run that created it, so resuming naturally
continues that same bounded batch — do not re-slice it again here.

## 5. Logging: make the batching visible

In the per-artist loop, existing logs already show `[i/len(due_artists)]`.
Add one line right after computing/logging `due_artists` (already shown in
step 4's example) so a glance at Actions logs answers "how much of the full
rotation got done, and how much is left."

## 6. Update the workflow YAML

`.github/workflows/spotify-recent-albums.yml`, in the "Fetch recent albums"
step, pass conservative defaults explicitly (don't just rely on script
defaults, so the intent is visible in the workflow file itself):

```yaml
      - name: Fetch recent albums
        env:
          SPOTIFY_CLIENT_ID: ${{ secrets.SPOTIFY_CLIENT_ID }}
          SPOTIFY_CLIENT_SECRET: ${{ secrets.SPOTIFY_CLIENT_SECRET }}
          SPOTIFY_REFRESH_TOKEN: ${{ secrets.SPOTIFY_REFRESH_TOKEN }}
          SPOTIFY_PLAYLIST_ID: ${{ secrets.SPOTIFY_PLAYLIST_ID }}
        run: |
          DAYS=${{ github.event.inputs.days || '365' }}
          PYTHONUNBUFFERED=1 python -u spotify/spotify-recent-albums.py --resume --days "$DAYS" \
            --batch-size 5 --min-request-interval 20
```

Optionally also add `workflow_dispatch.inputs.batch_size` /
`min_request_interval` so these can be overridden per manual run without
editing the file — mirror the existing `days` input pattern. Not required
for the fix to work, just a convenience.

## 7. Testing this change

Use the mock server + harness already built in `spotify/tests/`:

```bash
# Reproduce today's failure shape first (no batching, big daily quota trip):
python spotify/tests/simulate_workflow_harness.py \
  --num-artists 82 --daily-quota 60 --days-to-simulate 3 --fresh-state

# Then test the fix: small batch size should mean the quota is never even
# approached, and the full rotation completes over several simulated days
# instead of stalling on a 24h retry:
python spotify/tests/simulate_workflow_harness.py \
  --num-artists 82 --daily-quota 60 --days-to-simulate 20 --fresh-state \
  --script-args "--batch-size 5 --min-request-interval 0"
```

(`--min-request-interval 0` in the harness call above is just to keep the
simulated test fast — set it to a real value like 20 for the actual
production workflow args in step 6.)

Confirm in the harness's final "OVERALL SUMMARY":
- No day shows `RATE-LIMITED EXIT` when `--batch-size` keeps requests/run
  comfortably under `--daily-quota`.
- `All N artists checked at least once: YES` is eventually reached (after
  `ceil(N / batch_size)` simulated days), proving the rotation still
  completes correctly under batching.

## 8. Existing unit tests

`spotify/tests/test_spotify_recent_albums.py` should continue to pass
unmodified — none of the functions it tests (`get_rate_limit_wait`,
`wait_with_progress`, `spotify_request`, `get_due_artists`, `record_album`,
`get_report_albums`) change signature or behavior in this plan. Add new unit
tests alongside them for:

- A `due_artists` list longer than `--batch-size` gets truncated, and
  `state["in_progress"]["due_ids"]` reflects only the truncated subset.
- `RateLimiter.wait_if_needed()` sleeps at least `min_interval_seconds`
  between two calls when they'd otherwise be back-to-back (mock `time.sleep`
  and `time.time` similar to the existing rate-limit tests).

## 9. Rollback / turning it back up later

Both knobs are plain CLI flags with no state-file impact, so the user can
raise them at any time without a code change or data migration:

- Confirmed stable for a few days → raise `--batch-size` (e.g. 5 → 15 → 41 →
  82) to shorten how many days a full rotation takes.
- Confirmed the daily quota isn't the bottleneck → lower or zero out
  `--min-request-interval`.

No other component (state file schema, workflow permissions, commit step)
needs to change to support this tuning.
