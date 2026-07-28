# Fix-up steps: repo move out of `spotify/`

Everything except the GitHub Actions workflow is already consistent with the
new flat (repo-root) layout. Two concrete bugs need fixing, plus one doc
cleanup.

## 1. Fix `.github/workflows/spotify-recent-albums.yml`

Two stale `spotify/` path references will make the workflow fail outright.

**a) The run step points at the old script path:**

```yaml
# current (broken)
run: |
  DAYS=${{ github.event.inputs.days || '365' }}
  MIN_INTERVAL=${{ github.event.inputs.min_request_interval || '20' }}
  PYTHONUNBUFFERED=1 python -u spotify/spotify-recent-albums.py --resume --days "$DAYS" \
    --min-request-interval "$MIN_INTERVAL"
```

Change to:

```yaml
run: |
  DAYS=${{ github.event.inputs.days || '365' }}
  MIN_INTERVAL=${{ github.event.inputs.min_request_interval || '20' }}
  PYTHONUNBUFFERED=1 python -u spotify-recent-albums.py --resume --days "$DAYS" \
    --min-request-interval "$MIN_INTERVAL"
```

**b) The commit step stages the old state-file path:**

```yaml
# current (broken)
git add spotify/spotify-state.json
```

Change to:

```yaml
git add spotify-state.json
```

Note: this step has `if: always()`, so even though the run step would fail
first, the commit step still executes — and `git add` on a path that
doesn't exist returns a non-zero exit code, which (combined with Actions'
default `bash -eo pipefail` for `run:` blocks) fails the whole step.
Both of these need fixing together, or the commit step will still error
even after (a) is fixed, since state file won't exist at the old path yet.

## 2. Update `README.md` — remove the retired `--batch-size` flag

`--batch-size` was intentionally removed from the script (see
`docs/spotify-per-endpoint-rate-limit-plan.md`, Step 6 — the per-category
rate-limit isolation made the artificial per-run artist cap unnecessary).
The README still documents it and uses it in example commands, both of
which will now fail with an `argparse` "unrecognized arguments" error if
someone copies them verbatim:

- The "CLI Flags" table row for `--batch-size`.
- The "Conservative run with batching" example command.
- The "Testing the slowdown fix" harness example (`--script-args
  "--batch-size 5 --min-request-interval 0"`).

Simplest fix: delete the `--batch-size` table row and drop `--batch-size 5`
from both example commands (keep `--min-request-interval`, which is still a
real flag).

## 3. Nothing else needs to change

Confirmed consistent with the current flat layout, no action needed:

- `spotify-recent-albums.py`, `spotify-state.json`, `mock_spotify_server.py`,
  `simulate_workflow_harness.py`, `backfill_exclusions_once.py` — all sit at
  repo root and reference each other via `Path(__file__).parent`, which
  resolves correctly regardless of folder name.
- `tests/test_spotify_recent_albums.py` — uses
  `Path(__file__).resolve().parents[1] / "spotify-recent-albums.py"`, which
  correctly walks from `tests/` up to repo root.
- `spotify-state.json`'s schema (`rate_limits: {}` present at the top level)
  matches what the *current* script version's `load_state()` expects — this
  is the up-to-date state file, not a stale one from an earlier plan
  revision.
