# Implementation Plan: Per-Endpoint-Category Rate-Limit Isolation

## The problem

The script currently treats a rate-limit lockout as a single global flag:
`state["in_progress"]["retry_after"]`, one timestamp, checked/set in exactly
one place. In practice this means:

- If `GET /artists/{id}/albums` trips a long (>300s) 429 partway through the
  artist-scan loop, `spotify_request()` calls `sys.exit(2)` **immediately**.
- That kills the whole process before `prune_playlist()` (which uses
  `DELETE /playlists/{id}/items`) gets a chance to run *at all* in that
  invocation — even though removing tracks from a playlist is a completely
  different operation against a completely different endpoint.

This is overly conservative, and per Spotify's own docs, it isn't how their
quota system actually works:

> Note that this is different from rate limits. Endpoints are grouped into
> quota buckets and requests to endpoints in the same bucket count toward a
> shared limit. The specific groupings and limits are subject to change.
> — [Quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)

So being locked out on the bucket containing `GET /artists/{id}/albums`
(reads) does not necessarily mean the bucket containing
`DELETE /playlists/{id}/items` (playlist removal) is also locked out. Right
now the script can't tell the difference, because it only ever tracks one
retry deadline for the entire app.

**Important caveat:** Spotify does not publish which endpoints share a
bucket, and says the groupings are "subject to change." This plan does not
assume a specific bucket layout. Instead it treats **every distinct
(HTTP method, endpoint pattern) pair as its own independent rate-limit
category** — a safe, conservative default. Worst case, two endpoints that
actually share a real Spotify bucket get tracked separately in our state,
so we occasionally attempt a call that's still blocked and get another 429
in response (no harm — it's still respecting `Retry-After` when that
happens). We never do the opposite (assume something is safe when it
isn't), so this errs in the safe direction.

---

## 1. `endpoint_category()` — the categorization key

```python
import re as _re

_ID_SEGMENT = _re.compile(r"/[A-Za-z0-9]{15,}(?=/|$)")

def endpoint_category(method, url):
    """Normalizes a request into a stable category key, e.g.
    'GET /artists/{id}/albums', 'POST /playlists/{id}/items',
    'DELETE /playlists/{id}/items'. IDs (Spotify IDs are 22 chars, but this
    is loose on purpose) are collapsed to {id} so the category doesn't
    depend on which specific artist/album/playlist was being requested."""
    path = url.split("?", 1)[0]
    if path.startswith(SPOTIFY_API_BASE):
        path = path[len(SPOTIFY_API_BASE):]
    normalized = _ID_SEGMENT.sub("/{id}", path)
    return f"{method} {normalized}"
```

Examples this produces against the script's actual call sites:

| Call | Category |
|---|---|
| `GET /me/following` | `GET /me/following` |
| `GET /artists/{id}/albums` | `GET /artists/{id}/albums` |
| `GET /albums/{id}/tracks` | `GET /albums/{id}/tracks` |
| `POST /playlists/{id}/items` | `POST /playlists/{id}/items` |
| `DELETE /playlists/{id}/items` | `DELETE /playlists/{id}/items` |
| `GET /search` | `GET /search` |

---

## 2. State schema change: `rate_limits` becomes a top-level dict

Move rate-limit tracking out of `in_progress` (which gets cleared to `None`
at the end of every successful scan) and into its own top-level field that
persists regardless of whether a scan is mid-flight:

```json
{
  "artists": { ... },
  "known_albums": { ... },
  "in_progress": null,
  "rate_limits": {
    "GET /artists/{id}/albums": 1785282345,
    "POST /playlists/{id}/items": null
  }
}
```

- Key: category string from `endpoint_category()`.
- Value: unix timestamp the category is blocked until, or absent/`None` if
  not currently blocked.
- `load_state()` defaults this to `{}` for old state files (no migration
  needed — same pattern as every other field added so far).

```python
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)
            state.setdefault("rate_limits", {})
            return state
    return {"artists": {}, "known_albums": {}, "in_progress": None, "rate_limits": {}}
```

---

## 3. `spotify_request()`: check before calling, record per-category on 429

Two changes to the existing function:

**(a) Before making the network call**, check whether this category is
already known to be blocked. If so, skip the network round-trip entirely —
this saves quota and matches exactly what would happen if we'd made the
call anyway.

**(b) On a 429**, record the retry deadline under this category's key
instead of the old single `in_progress.retry_after` field. For a long wait,
raise a catchable exception instead of calling `sys.exit(2)` directly, so
`main()` can decide what to do next (see Step 4) instead of the process
dying unconditionally.

```python
class LongRateLimitBlock(Exception):
    """Raised when a request's category is rate-limited past
    LONG_WAIT_THRESHOLD_SECONDS. Callers decide whether to abort the whole
    run or just skip this phase and continue with unrelated work."""
    def __init__(self, category, retry_until):
        self.category = category
        self.retry_until = retry_until
        super().__init__(f"{category} blocked until {retry_until}")


def spotify_request(method, token, url, state, params=None, json_data=None, retries=5, backoff=1):
    category = endpoint_category(method, url)

    # Already known to be blocked from an earlier call this run (or a
    # previous run, since rate_limits persists) -- don't even try.
    blocked_until = state.get("rate_limits", {}).get(category)
    if blocked_until and int(time.time()) < blocked_until:
        raise LongRateLimitBlock(category, blocked_until)

    rate_limiter.wait_if_needed()
    headers = {"Authorization": f"Bearer {token}"}
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, params=params, json=json_data)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", backoff))
        if retry_after > LONG_WAIT_THRESHOLD_SECONDS:
            retry_until = int(time.time()) + retry_after
            state.setdefault("rate_limits", {})[category] = retry_until
            save_state(state)
            hours, minutes = retry_after // 3600, (retry_after % 3600) // 60
            log(f"  Rate limited on {method} {url} (category: {category}). "
                f"Saving state; blocked for {hours}h {minutes}m ({retry_after}s).")
            raise LongRateLimitBlock(category, retry_until)
        if retries <= 0:
            raise Exception("Rate limited - max retries exceeded")
        wait = max(retry_after, backoff)
        log(f"  Rate limited on {method} {url}. Waiting {wait}s...")
        time.sleep(wait)
        return spotify_request(method, token, url, state, params, json_data, retries - 1, wait * 2)

    if resp.status_code in (502, 503, 504):
        if retries <= 0:
            raise RuntimeError(f"Server error {resp.status_code} - max retries exceeded")
        wait = backoff
        log(f"  Server error {resp.status_code} on {method} {url}. Waiting {wait}s...")
        time.sleep(wait)
        return spotify_request(method, token, url, state, params, json_data, retries - 1, wait * 2)

    if resp.status_code >= 400:
        print(f"  [DEBUG] {resp.status_code} response headers: {dict(resp.headers)}")
        print(f"  [DEBUG] {resp.status_code} response body: {resp.text[:500]}")
        if is_non_retryable_spotify_error(resp):
            raise RuntimeError(f"Spotify API request failed with non-retryable error: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()

    if resp.status_code == 204:
        return {}
    return resp.json()
```

Short-wait 429s (≤300s) keep the existing in-process sleep-and-retry
behavior unchanged — no category tracking needed there, since it never
propagates past this one call site anyway.

No changes needed to `spotify_get` / `spotify_post` / `spotify_delete` — they
already just thread `state` through to `spotify_request`, and the category
key is derived automatically inside it.

---

## 4. `main()`: phases can fail independently

Restructure the tail of `main()` into distinct phases, each wrapped so a
`LongRateLimitBlock` in one doesn't prevent the others from running:

```python
blocked_categories = []

# --- Phase 1: fetch followed artists ---
try:
    log("Fetching followed artists...")
    artists = get_followed_artists(token, state)
    log(f"Found {len(artists)} followed artists.")
except LongRateLimitBlock as e:
    log(f"Skipping artist scan this run -- {e.category} is rate-limited "
        f"until {datetime.fromtimestamp(e.retry_until, tz=timezone.utc)}.")
    blocked_categories.append(e.category)
    artists = list(state.get("artists", {}).values())  # best-effort fallback for reporting only

# --- Phase 2: due-artist scan loop (only if phase 1 succeeded) ---
if artists and not blocked_categories:
    # ... existing due_artists / resume / per-artist loop, unchanged in
    # spirit, except every spotify_request call can now raise
    # LongRateLimitBlock. Catch it around the *whole loop*, not per-artist:
    try:
        for i, artist in enumerate(due_artists, 1):
            ... # unchanged per-artist body
    except LongRateLimitBlock as e:
        log(f"Stopping artist scan -- {e.category} is rate-limited until "
            f"{datetime.fromtimestamp(e.retry_until, tz=timezone.utc)}. "
            f"Progress so far is saved; will resume next run.")
        blocked_categories.append(e.category)
        # in_progress stays populated (already saved incrementally per
        # artist, same as today) so --resume picks up where this left off

if not blocked_categories:
    state["in_progress"] = None
    save_state(state)

# --- Phase 3: prune playlist (independent category: DELETE .../items) ---
try:
    prune_playlist(token, state, args.days, os.environ.get("SPOTIFY_PLAYLIST_ID"))
except LongRateLimitBlock as e:
    log(f"Skipping playlist prune this run -- {e.category} is rate-limited "
        f"until {datetime.fromtimestamp(e.retry_until, tz=timezone.utc)}.")
    blocked_categories.append(e.category)

# --- Phase 4: report (no network calls, always runs) ---
report_albums = get_report_albums(state, args.days)
if args.json:
    print(json.dumps(report_albums, indent=2))
else:
    print(format_markdown_table(report_albums))
summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
if summary_path:
    with open(summary_path, "a") as f:
        f.write(format_markdown_table(report_albums))

if blocked_categories:
    log(f"Run finished with {len(blocked_categories)} categor{'y' if len(blocked_categories)==1 else 'ies'} "
        f"still rate-limited: {', '.join(blocked_categories)}. Exiting 2 for CI visibility.")
    sys.exit(2)
```

Key behavior change: **the process no longer dies the instant any one
category gets rate-limited.** Each phase gets its own chance to run, using
whatever categories aren't currently blocked. The run still exits non-zero
(`2`) at the end if *anything* was blocked, so CI/monitoring still sees it
as "not fully healthy" — it just doesn't throw away unrelated work to get
there.

### Also update the `--resume` fast-path

The very first thing `main()` does today under `--resume` is check a single
`retry_after` before fetching anything. That check should move to be
**category-specific**, checked at the point each phase actually runs (which
Step 3's `spotify_request()` already does automatically via the
before-the-call check) rather than as one gate at the very top of `main()`.
This also fixes the exact inefficiency flagged earlier — `get_followed_artists()`
no longer burns a real request just to find out the category was already
known to be blocked, since `spotify_request()` now checks `rate_limits`
before touching the network at all.

---

## 5. Mock server: support per-category quotas for testing

`mock_spotify_server.py`'s `_State` currently tracks one global
`daily_quota` counter shared across every endpoint
(`_check_quota_and_rate_limit()`), which doesn't reflect Spotify's real
bucket-based behavior. Extend it to optionally track quota per path
prefix, defaulting to the current global behavior when not configured (so
existing tests/harness runs are unaffected):

```python
def __init__(self, ..., daily_quota=None, per_category_quota=None, ...):
    ...
    self.daily_quota = daily_quota              # existing global behavior
    self.per_category_quota = per_category_quota or {}  # e.g. {"GET /v1/artists": 40, "DELETE /v1/playlists": 200}
    self.category_request_counts = {}            # category -> count since reset
```

```python
def _check_quota_and_rate_limit(self, method, path):
    ...
    # existing global daily_quota check stays as-is

    if self.per_category_quota:
        category_prefix = self._match_category(method, path)
        if category_prefix:
            count = self.category_request_counts.get(category_prefix, 0) + 1
            self.category_request_counts[category_prefix] = count
            limit = self.per_category_quota.get(category_prefix)
            if limit is not None and count > limit:
                return (429, {"Retry-After": "86400"},
                        {"error": {"status": 429, "message": f"Category quota exceeded: {category_prefix}"}})
    ...
```

This lets the harness reproduce the specific scenario that motivated this
plan: configure a tiny quota on the read-side category and an untouched
(or much larger) quota on the delete-side category, run a simulated day,
and confirm pruning still completes even while the artist scan is blocked.

---

## 6. Remove the `--batch-size` artist cap — run until actually rate-limited

`--batch-size` (from the earlier slowdown plan) was a guess at a safe
number of artists per run, chosen because at the time a rate-limit hit
meant losing the *whole run* — so capping conservatively was the only way
to make partial progress reliably. With per-category tracking (Steps 1–4)
and phases that no longer die on the first block, that reasoning no longer
holds: the script now finds out the real limit by hitting it, saves
progress up to that exact point, and picks up the remainder next run —
which is both simpler and more accurate than pre-guessing a batch size.

**Change:** drop the artificial slice and process every due artist,
letting `LongRateLimitBlock` end the loop naturally when the real quota is
hit.

```python
# Before (slowdown plan):
all_due_artists = get_due_artists(artists, state, args.interval_days)
due_artists = all_due_artists[:args.batch_size]

# After:
due_artists = get_due_artists(artists, state, args.interval_days)
```

Remove `--batch-size` and `DEFAULT_MAX_ARTISTS_PER_RUN` entirely — there's
nothing left for them to control once nothing pre-slices `due_artists`.

The per-artist loop itself doesn't need to change: it already saves
`state["artists"][id]["last_checked"]` and calls `save_state()` after every
artist, and (per Step 4) a `LongRateLimitBlock` raised mid-loop is caught
around the whole loop, leaving `in_progress.processed_ids` /
`in_progress.due_ids` exactly where they were — so `--resume` continues
from the exact artist that was in flight when the block hit, same as
today.

**Keep `--min-request-interval`.** That flag paces individual requests
(insurance against a per-window rate limit), which is a completely
different concern from batch-size (an artificial cap on total work per
run). Removing batch-size doesn't change the reasoning for keeping the
pacing flag.

### What changes in practice

- A run now processes as many due artists as the real quota allows, found
  out empirically rather than guessed — likely far more than 5 per run on
  a day the quota resets fresh, and fewer if something else already ate
  into it earlier that day.
- Because the check happens *before* each network call (Step 3), the loop
  stops the instant the category is blocked rather than partway through
  wasting further requests that would just 429 anyway.
- The full 82(+)-artist rotation likely completes faster on average, since
  it's no longer bottlenecked by an artificially small fixed batch size
  when the actual daily quota has more headroom than that.

### Workflow YAML cleanup

Remove `batch_size` / `--batch-size` from
`.github/workflows/spotify-recent-albums.yml`'s `workflow_dispatch.inputs`
and the `run:` step:

```yaml
      - name: Fetch recent albums
        env:
          SPOTIFY_CLIENT_ID: ${{ secrets.SPOTIFY_CLIENT_ID }}
          SPOTIFY_CLIENT_SECRET: ${{ secrets.SPOTIFY_CLIENT_SECRET }}
          SPOTIFY_REFRESH_TOKEN: ${{ secrets.SPOTIFY_REFRESH_TOKEN }}
          SPOTIFY_PLAYLIST_ID: ${{ secrets.SPOTIFY_PLAYLIST_ID }}
        run: |
          DAYS=${{ github.event.inputs.days || '365' }}
          MIN_INTERVAL=${{ github.event.inputs.min_request_interval || '20' }}
          PYTHONUNBUFFERED=1 python -u spotify/spotify-recent-albums.py --resume --days "$DAYS" \
            --min-request-interval "$MIN_INTERVAL"
```

### `simulate_workflow_harness.py`

No changes required to the harness itself — `--script-args` still passes
whatever flags exist through verbatim. Just stop passing `--batch-size` in
example invocations in the README (Step 8 below covers docs).

---

## 7. Cron timing: can the workflow trigger itself right when the block expires?

**Short answer: not with GitHub Actions' native `schedule` trigger, no —
but you don't need exact timing, because of what Steps 1–6 already give
you.** Here's the reasoning and the practical recommendation.

### Why "reschedule the cron to fire exactly at expiry" isn't really available

- `schedule: cron` in workflow YAML is a static expression evaluated by
  GitHub's own scheduler against the **default branch's committed YAML**.
  A workflow run can't reach out and change its own future trigger time —
  there's no API to say "run me again in exactly 6h14m."
- You *could* have the workflow itself `git commit` a modified cron
  expression into the YAML file, but that's fragile: scheduled-workflow
  changes only take effect once merged to the default branch, GitHub
  documents a delay of up to several minutes after a cron-expression change
  before it's picked up, and self-modifying workflow files is generally
  considered an anti-pattern (a bad commit could wedge the schedule
  entirely, and it muddies the git history of the workflow file with
  automated commits).
- GitHub Actions has no built-in "run once, at this specific future
  timestamp" trigger. `repository_dispatch` and `workflow_dispatch` are
  both immediate — something has to be alive at the right moment to fire
  them, which just moves the "how do I wake up at the right time" problem
  somewhere else (e.g. a long-sleeping job, which risks the 6-hour
  `GITHUB_TOKEN`/job timeout and burns Actions minutes for no reason).

### The recommended approach: run more often, let the script self-skip

Because of Step 3's before-the-call check, a run that starts while a
category is still blocked now costs **almost nothing** — `spotify_request()`
sees `rate_limits[category]` is still in the future and raises
`LongRateLimitBlock` without making any network call at all. So instead of
trying to time a single daily run precisely, tighten the schedule and let
most invocations be cheap no-ops until the block actually clears:

```yaml
on:
  schedule:
    - cron: "*/15 * * * *"   # every 15 minutes
  workflow_dispatch:
    ...
```

What this gets you:
- The very first run after a block's `retry_until` passes will actually do
  work — effectively "resuming right when the limit expires," accurate to
  within the cron interval (worst case ~15 minutes late, not a fixed daily
  slot).
- Every run in between exits in a few seconds (Python startup + one
  category check that returns "still blocked") — negligible Actions
  minutes, since 429-avoidance means zero real Spotify requests happen
  during the wait.
- No self-modifying workflow file, no long-sleeping job, no extra
  infrastructure.

### Caveats to know about

- **GitHub doesn't guarantee cron precision.** Their docs note scheduled
  workflows can be delayed during periods of high platform load, and the
  shortest supported interval is every 5 minutes — but a 5-minute cron on
  a low-traffic public repo has been reported to slip by 10–15+ minutes in
  practice. 15 minutes is a reasonable balance of responsiveness vs. not
  fighting the scheduler; go tighter only if you've confirmed your repo's
  actual scheduled-run latency is good.
- **Concurrency already protects you.** The existing
  `concurrency: group: spotify-recent-albums` /
  `cancel-in-progress: false` block means a 15-minute cron firing while a
  previous run is still mid-flight (e.g. slow-but-not-blocked artist scan)
  queues instead of overlapping — no changes needed there.
- **GitHub Actions minutes**: for a public repo, scheduled minutes are
  free/unmetered the same as any other Actions usage; for a private repo
  they count against your plan's included minutes. A 15-minute cron is
  ~96 runs/day; at a few seconds each for the "still blocked, exit" case,
  this is a small, predictable addition — worth glancing at your account's
  Actions usage after a week to confirm it's negligible for your plan.
- **Report/playlist noise**: with Step 4's phased `main()`, even a
  "mostly blocked" run still executes the report phase (no network calls)
  and prints/writes a step summary every 15 minutes. If that's noisy, gate
  the step-summary write specifically to runs where at least one phase
  actually did work (e.g. only write it if `blocked_categories` is empty
  or `processed_ids` grew), rather than suppressing it from the schedule
  itself.
- **If you want closer-to-exact timing without the polling**, the only
  real way is external infrastructure that isn't GitHub Actions at all
  (e.g. a small serverless function or cheap always-on host with its own
  precise scheduler, reading `rate_limits` out of the committed state file
  via the GitHub API and firing `workflow_dispatch` right at expiry). This
  is a legitimate option if the extra precision genuinely matters, but for
  a personal daily-ish digest script, it's very likely not worth the added
  moving parts compared to a 15-minute cron with cheap self-skipping.

---

## 8. Testing plan

1. **`endpoint_category()`** — table-driven test confirming each real call
   site in the script normalizes to a stable, distinct category (the table
   in Step 1), and that two different IDs on the same endpoint pattern
   produce the *same* category (`/artists/AAA.../albums` and
   `/artists/BBB.../albums` → same key).
2. **`spotify_request()` pre-check** — seed `state["rate_limits"]` with a
   future timestamp for a category, call `spotify_request` for that
   category, assert it raises `LongRateLimitBlock` *without* `requests.get`
   / `requests.post` being called at all (mock `requests.request` and
   assert `assert_not_called()`).
3. **`spotify_request()` records on 429** — mock a response with
   `Retry-After: 86400`, assert `state["rate_limits"][category]` gets set
   and `LongRateLimitBlock` is raised (not `SystemExit`).
4. **Phase isolation in `main()`** — the core scenario from this plan:
   mock the artist-scan phase to raise `LongRateLimitBlock` for
   `GET /artists/{id}/albums`, confirm `prune_playlist()` still gets called
   and completes, and the process exits with code `2` (not before pruning
   ran).
5. **Harness-level**: using the extended mock server (Step 5), configure a
   tiny quota on `GET /v1/artists` and a much larger one on
   `DELETE /v1/playlists`, run `simulate_workflow_harness.py` for a day
   where the artist scan is expected to get blocked, and confirm the day's
   output log shows the prune phase still ran (via a log line or a mock
   server request-log assertion that at least one `DELETE` request landed
   after the last blocked `GET`).
6. **No batch-size cap** — run the harness with a small mock `daily_quota`
   set well above what 82 artists would need in one pass, confirm every
   due artist gets processed in a single run (no artificial rollover to a
   second simulated day just because of a batch-size slice that no longer
   exists).
7. **Natural rate-limit stop mid-loop** — run the harness with a mock
   `daily_quota` deliberately smaller than what 82 artists need, confirm
   the loop stops exactly at the artist that triggered the block (not
   earlier, not later), `in_progress.processed_ids` reflects everything
   before it, and a `--resume` run continues from there.
8. **Cheap self-skip when blocked** — with a category already blocked in
   `state["rate_limits"]`, run the script and assert (via the mock
   server's request log) that **zero** requests were made to Spotify for
   that category, while confirming the report phase (no network calls)
   still printed output.

---

## 9. Rollout / rollback

- **No destructive migration.** `rate_limits` defaults to `{}` on load for
  any existing state file; the old `in_progress.retry_after` field simply
  stops being written going forward (existing `None` value is harmless if
  still present from before).
- **Safe to deploy mid-cycle.** Worst case on the very first run after
  deploy, if `in_progress` was left with a stale `retry_after` from before
  this change, it's just inert data now — the new per-category checks in
  `spotify_request()` are what actually gate behavior.
- **To roll back**: revert the code. Nothing about the new `rate_limits`
  field conflicts with the old single-`retry_after` logic if you needed to
  temporarily run both versions in sequence (e.g. mid-deploy) — the old
  code simply ignores the new field, and the new code ignores a leftover
  `retry_after` value.

---

## Summary of concrete changes

| Component | Change |
|---|---|
| `endpoint_category(method, url)` | New — normalizes a request into a stable category key by method + ID-stripped path |
| `LongRateLimitBlock` | New exception, replaces the unconditional `sys.exit(2)` on a long 429 |
| `state["rate_limits"]` | New top-level dict: category → blocked-until timestamp, persists independent of `in_progress` |
| `spotify_request()` | Checks `rate_limits` before calling (skip network if already known-blocked); records per-category on a long 429 instead of a single global value |
| `main()` | Restructured into independent phases (fetch artists / scan / prune / report), each catching `LongRateLimitBlock` separately so one blocked category doesn't prevent unrelated phases from running |
| `mock_spotify_server.py` | Optional `per_category_quota` dict, alongside the existing global `daily_quota`, to let tests simulate bucket-isolated lockouts |