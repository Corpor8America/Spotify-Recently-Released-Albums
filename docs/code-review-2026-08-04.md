# Code Review — 2026-08-04

Scope: current Flask application, Spotify integration, state persistence, and deployment configuration. This document records review findings; application changes made later for a separate test-stability fix are outside its scope.

## Resolution summary

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | Management routes have no access control or CSRF protection | High | Deferred |
| 2 | Concurrent reads/writes can lose state or fail | High | Fixed |
| 3 | Scan and reorder can modify the same playlist concurrently | High | Fixed |
| 4 | Outbound Spotify calls have no timeout | Medium | Fixed |
| 5 | Settings input can cause 500s or stop startup | Medium | Fixed |
| 6 | OAuth redirect origin trusts client-controlled forwarded host headers | Medium | Deferred |
| 7 | Test command reports success while most web-route tests do not run | Low | Deferred |

Fixes for findings 2–5 are implemented in `spotify_core.py` and `app.py` (see per-finding notes below). Findings 1, 6, and 7 are deferred and need decisions/design before implementation.

## Findings

### 1. High — management routes have no access control or CSRF protection

`app.py` exposes settings changes, scans, cancellation, playlist creation/reordering, and album overrides to any caller. The app is published on a host port in `docker-compose.yml`, while routes such as `POST /settings`, `/run`, `/cancel`, `/reorder`, and `/albums/<id>/override` do not authenticate callers or validate a CSRF token. A malicious page visited by an authenticated browser (or any network peer when the service is exposed) can change configuration or modify the Spotify playlist.

Suggested fix:

- Put the app behind an authentication boundary (for example, a reverse-proxy identity layer or app-level login) and restrict `/status` to a minimal health response.
- Add CSRF protection to every state-changing HTML form and use secure session-cookie settings (`Secure`, `HttpOnly`, and an appropriate `SameSite` policy).
- Bind the published port to localhost when it is intended only for local use, or document and enforce the expected trusted-network deployment.

**Status: Deferred.** No code change. The cheapest near-term mitigation is binding the published port to localhost (`127.0.0.1:8081:8080` in `docker-compose.yml`) plus `SameSite=Lax`/secure session-cookie settings; a full app-login or reverse-proxy identity layer is a larger feature and needs a deployment-intent decision.

### 2. High — concurrent reads/writes can lose state or fail

The scan thread, dashboard/status routes, cancellation, and override requests all load and write the same JSON state file. `save_state` uses one fixed temporary path (`spotify-state.tmp`) and has no process-local lock or version check. Two writers can overwrite each other’s changes; one writer can also replace the temporary file before the other calls `replace`, causing a request/thread failure. Atomic rename prevents a torn final file, but it does not make the read-modify-write sequence atomic.

Suggested fix:

- Centralize state access behind a lock and expose an `update_state(mutator)` operation that holds it across load, mutation, and save.
- Use a unique temporary file per write and flush/fsync before replacement.
- If multi-process or multi-container deployment becomes supported, use a transactional store (such as SQLite/PostgreSQL) or an inter-process file lock. Add concurrency tests that run scan progress, cancellation, and an override together.

**Status: Fixed (partial).** Implemented:

- `spotify_core.py`: added `_state_lock` (process-local `threading.RLock`) guarding `load_state`/`save_state`, plus `update_state(mutator)` for atomic read-modify-write. `cancel_scan` and the `/albums/<id>/override` route (`app.py`) now go through `update_state`.
- `save_state` now writes via a unique `tempfile.mkstemp` file, flushes and `fsync`s, then `os.replace`s, and unlinks the temp file on failure — the fixed-`.tmp` collision is gone.

Remaining gap: the scan thread still mutates its own long-lived in-memory state object and periodically saves it, so an override/cancel saved mid-scan can still be overwritten by the scan's next save. Fully closing this requires refactoring the scan loop to route mutations through `update_state` (or moving to a transactional store). The lock is process-local only; multi-process/multi-container deployment still needs SQLite/PostgreSQL or an inter-process lock. No concurrency tests were added.

### 3. High — scan and reorder can modify the same playlist concurrently

Scans are protected by `run_lock` and reorder is protected by a separate `reorder_lock`, so they can run at the same time. Reorder reads all playlist tracks, deletes them, then re-adds the state snapshot. A scan that adds tracks during that window can have its new tracks removed while its state still records `added_to_playlist=True`, leaving the playlist and persisted state inconsistent.

Suggested fix:

- Use one shared playlist-mutation lock for scan additions/pruning and reorder, or make reorder wait for an active scan and prevent starting one while it runs.
- After any destructive reorder, reconcile the playlist against persisted state and report failures clearly.
- Add an integration test that overlaps a scan add with a reorder.

**Status: Fixed.** `app.py:_do_reorder` now acquires `run_lock` before the destructive delete-all/re-add (blocking until any active scan finishes) and holds it for the whole reorder. Because `start_scan` acquires `run_lock` non-blocking, no new scan can start mid-reorder. Remaining: no explicit post-reorder reconcile step and no overlap integration test were added.

### 4. Medium — outbound Spotify calls have no timeout and can hold locks indefinitely

`requests.request` in `spotify_request`, plus the OAuth token `requests.post` calls, omit `timeout`. A stalled network connection can therefore block the background scan/reorder thread indefinitely. The corresponding lock remains held, preventing all later work; retries only handle responses that arrive.

Suggested fix:

- Define explicit connect/read timeouts and pass them to every `requests` call (for example, `timeout=(5, 30)`).
- Catch `requests.Timeout` and `requests.ConnectionError` at the scan/reorder boundary, log a concise error, retain resumable progress, and release the lock.
- Test timeout behavior with a deliberately non-responsive mock endpoint.

**Status: Fixed.** `timeout=(5, 30)` added to `spotify_request`, `exchange_code_for_token`, and `get_access_token` in `spotify_core.py`. Lock release on failure was already guaranteed by `try/finally` in `run_scan`/`_do_reorder`. No dedicated timeout test was added.

### 5. Medium — settings input can cause 500s or stop startup

`POST /settings` directly casts user input with `int()`/`float()` and accepts unconstrained values. Invalid numeric text raises an unhandled `ValueError`; negative/zero values have undefined scheduling/rate-limit behavior. The cron code only catches an incorrect field count: a five-field but semantically invalid expression can still make `CronTrigger(...)` raise during startup.

Suggested fix:

- Parse and validate all fields before saving: positive bounded integers for day intervals, non-negative bounded request spacing, and a playlist-ID format check.
- Construct/validate `CronTrigger` during settings submission; return a field-level 400 error rather than saving invalid configuration.
- Wrap scheduler registration so a bad persisted setting falls back safely instead of preventing the web app from booting.

**Status: Fixed.** `app.py` now validates on POST: bounded `_parse_int_field`/`_parse_float_field` helpers, cron validated by actually constructing a `CronTrigger` via `_validate_cron_schedule`, and a lenient playlist-ID format check (`[A-Za-z0-9]{15,}`; empty allowed). Invalid input returns a 400 instead of a 500 or bad persisted config. `_start_scheduler` reuses `_validate_cron_schedule` and falls back to the default `0 6 * * *` on any invalid persisted value, so the app still boots.

### 6. Medium — OAuth redirect origin trusts client-controlled forwarded host headers

The application enables `ProxyFix(..., x_host=1, x_proto=1)` for every request and falls back to `request.host_url` when `public_base_url` is empty. If the app is reachable directly rather than only through one trusted proxy, a caller can supply `X-Forwarded-Host`/`X-Forwarded-Proto` and influence the OAuth `redirect_uri`. Even where Spotify rejects an unregistered URI, this creates confusing failed logins and is unsafe proxy configuration.

Suggested fix:

- Require a validated HTTPS `PUBLIC_BASE_URL` in non-local deployments and use it exclusively for OAuth redirects.
- Configure the reverse proxy to remove external forwarded headers; enable `ProxyFix` only when traffic is guaranteed to arrive through that proxy.
- Validate hostname/scheme against an allowlist at startup.

**Status: Deferred.** No code change. Needs a deployment decision (is the app ever reachable directly?) before picking one of the mitigations.

### 7. Low — test command reports success while most web-route tests do not run

`python -m unittest discover -s tests -p "test_*.py" -v` completed with 111 tests, but 44 tests were skipped in this environment: all Flask route tests because Flask was not installed, and Docker integration tests because `INTEGRATION_TEST=1` was not set. This can obscure regressions in routes, OAuth wiring, and the container image.

Suggested fix:

- Document a single developer/CI test command that installs `requirements.txt` before discovery.
- Make unit-test dependency installation a CI prerequisite and publish skipped-test counts as a failure or explicit separate job.
- Keep Docker integration tests in a scheduled or required pipeline job using `INTEGRATION_TEST=1`.

**Status: Deferred.** No code change. Note: `ci.yml` already installs `requirements.txt` before discovery and runs the Docker integration tests in a separate `INTEGRATION_TEST=1` job, so the practical gap is developer-local: add the install step to the README/runbook (or switch discovery to pytest), and optionally fail CI on skips.

## Other things of note

- `update_state` contract: the mutator must return the mutated state dict for the change to be persisted; returning `None` skips the write (used by `/albums/<id>/override` for the "unknown album" case, avoiding a pointless save).
- Reorder's blocking `run_lock.acquire` means a hung scan would make reorder wait indefinitely; finding 4's timeouts bound how long any single scan call can stall, but a long-rate-limited scan can still occupy `run_lock` for an extended period (by design, to keep playlist writes consistent).
- The playlist-ID validation in finding 5 is deliberately lenient; Spotify playlist IDs are 22 base62 characters, so a stricter check could be adopted if desired.
- A stale `spotify-state.tmp`-style file is no longer produced (unique temp names are always cleaned up or atomically replaced), but old `.tmp` artifacts from previous versions are not migrated.

## Verification performed

- Implemented findings 2–5 in `spotify_core.py` and `app.py` (2026-08-04).
- Re-ran `python -m unittest discover -s tests -p "test_*.py" -v`.
- Result: 111 tests passed; 22 were skipped (Docker integration tests require `INTEGRATION_TEST=1`). Flask route tests, including the updated settings, override, cancel, and reorder paths, ran and passed.
- No runtime data files were modified.
