# Implementation Plan: Commit State to a Separate `data` Branch

## Goal

`main` has repository rules (Rulesets) requiring changes via PR
("Cannot update this protected ref" / GH013). The workflow's
`git push` to `main` for `spotify-state.json` fails as a result. Rather
than weakening protection on `main`, push the state file to a dedicated
`data` branch that has no such rules. Code (script, workflow YAML, tests)
stays on `main` and goes through normal PRs; only the bot-written state
file lives on `data`.

Do not change any scheduling, rate-limiting, or state-file *contents*
logic — this plan only changes *where* `spotify-state.json` is committed.

---

## 0. Preconditions / things to verify before starting

1. Confirm you have push access to create a new branch on the repo
   (`data` doesn't exist yet).
2. Confirm the `GITHUB_TOKEN` (or whatever token the workflow uses) has
   `contents: write` — already true today per the existing
   `permissions:` block, and a new unprotected branch needs nothing extra
   beyond that.
3. Decide where the workflow YAML itself lives. **Keep it on `main`.**
   GitHub only picks up `schedule:` triggers from the workflow file on the
   repo's **default branch**, regardless of which branch a given run later
   checks out for its steps. If the `.yml` only existed on `data`, the cron
   would silently stop firing.

---

## 1. Create the `data` branch (one-time, by hand)

```bash
git checkout main
git pull
git checkout -b data
git push -u origin data
git checkout main
```

This can start as an exact copy of `main` — nothing on `data` needs to
differ except that going forward, only `spotify-state.json` gets updated
on it. It's fine (and expected) for `data` to drift out of sync with
`main`'s code over time; the workflow only ever reads/writes one file on
that branch.

No ruleset should be added to `data` — that's the entire point of moving
the commit target here.

---

## 2. Update the workflow to check out `data` for the state file

The key change: **two checkouts**. One (implicit, via the default
`schedule`/`workflow_dispatch` trigger) supplies the workflow file and
script from `main`. A second explicit `actions/checkout@v4` step pulls
`data` into a subdirectory so the script can read/write the real
`spotify-state.json` there, and the commit step pushes back to `data`
instead of `main`.

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
      min_request_interval:
        description: "Minimum seconds between API calls"
        required: false
        default: "10"

permissions:
  contents: write

concurrency:
  group: spotify-recent-albums
  cancel-in-progress: false

jobs:
  fetch-albums:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout main (script + workflow)
        uses: actions/checkout@v4
        with:
          path: main

      - name: Checkout data (state file)
        uses: actions/checkout@v4
        with:
          ref: data
          path: data

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: python -m pip install requests

      - name: Sync state file into script's working directory
        run: |
          mkdir -p main/spotify
          if [ -f data/spotify/spotify-state.json ]; then
            cp data/spotify/spotify-state.json main/spotify/spotify-state.json
          fi

      - name: Fetch recent albums
        working-directory: main
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

      - name: Commit updated state to data branch
        if: always()
        run: |
          mkdir -p data/spotify
          cp main/spotify/spotify-state.json data/spotify/spotify-state.json
          cd data
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add spotify/spotify-state.json
          git diff --staged --quiet || git commit -m "Update Spotify tracking state [skip ci]"
          git pull --rebase origin data
          git push origin HEAD:data
```

Notes on the structure:

- `path: main` / `path: data` check both branches out into sibling
  subdirectories of the job's workspace, so nothing overwrites the other.
- The **"Sync state file into script's working directory"** step copies
  `data`'s copy of `spotify-state.json` into where the script expects it
  (next to `spotify-recent-albums.py` on `main`) before the script runs,
  since `STATE_FILE = Path(__file__).parent / "spotify-state.json"` is
  fixed relative to the script's own location.
- After the script runs (even on a rate-limit exit — hence
  `if: always()`), the **commit step** copies the file back the other
  direction and commits/pushes it to `data` only.
- Adjust the `spotify/` path segments above if your actual repo layout
  differs (i.e. if the script and state file aren't inside a `spotify/`
  subdirectory — the plan docs above sometimes show the file at repo root
  and sometimes under `spotify/`; match whatever your `STATE_FILE` path
  and existing workflow actually use).

---

## 3. Alternative, simpler structure: skip the copy steps

If you don't need the state file to also exist in a human-browsable spot
on `main`, an easier layout is to **only ever check out `data`**, and put
the script there too as a symlink-free convenience — but this couples your
code to a branch you don't want polluted with automated commits, and
every code change would need to be manually synced onto `data`. **Not
recommended.** The two-checkout approach in Step 2 keeps code and bot
data cleanly separated, matching why you wanted a separate branch in the
first place.

---

## 4. `.gitignore` consideration

Since `main`'s working copy of `spotify-state.json` (under `main/`) is
just a scratch copy for the script to read/write during the job — not
something you want accidentally committed back to `main` by some future
change to the workflow — nothing extra is required today (the workflow
never runs `git add`/`git commit` from inside `main/`), but it's worth
adding a note or `.gitignore` entry in the repo so a future edit to this
workflow doesn't accidentally introduce a commit step that pushes the
state file back to `main`.

---

## 5. Testing plan

1. **Manual dispatch smoke test**: trigger `workflow_dispatch` once,
   confirm in the Actions log that:
   - "Checkout data" succeeds even the very first time (empty/no state
     file yet — the sync step's `if [ -f ... ]` guard handles this).
   - The script runs and produces a report.
   - The commit step's `git push origin HEAD:data` succeeds with no
     `GH013` error.
2. **Confirm `main` is untouched**: after the run, check `main`'s commit
   history — there should be no new commits there from this workflow.
3. **Confirm `data` has exactly one new commit**, touching only
   `spotify-state.json` (or `spotify/spotify-state.json`, per your path).
4. **Second consecutive run**: confirm it picks up the state committed by
   the first run (i.e. `last_checked` timestamps and `known_albums` persist
   across runs) — this proves the round-trip copy in Steps 2's sync step
   and commit step are both wired correctly.
5. **Rate-limit-exit test**: if feasible, force a long 429 (e.g. via the
   mock server / harness pointed at this workflow logic) and confirm
   `if: always()` still commits the in-progress state to `data` even
   though the script step exited non-zero.

---

## 6. Rollback

- Revert the workflow YAML to check out and push directly to `main` (the
  version you had before this change).
- The `data` branch can be deleted or left alone — it's harmless either
  way since nothing else depends on it once the workflow stops targeting
  it.
- No changes to `spotify-recent-albums.py` itself are needed for either
  direction, since the script only ever cares about a local file path, not
  which git branch it's sitting on.

---

## Summary of concrete changes

| Component | Change |
|---|---|
| `data` branch (new) | Created once, holds only bot-committed `spotify-state.json` going forward, no ruleset |
| `.github/workflows/spotify-recent-albums.yml` | Two `actions/checkout@v4` steps (`main`, `data`); new sync-in step before the script runs; commit step now operates inside `data/` and pushes `HEAD:data` instead of `main` |
| `spotify-recent-albums.py` | No changes |
| `main` branch protections / rulesets | No changes — stay exactly as strict as they are today |
