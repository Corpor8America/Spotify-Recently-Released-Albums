#!/usr/bin/env python3
"""
Simulated GitHub Actions workflow harness for spotify-recent-albums.py.

What this does:
  1. Starts the mock Spotify server (mock_spotify_server.py) in-process.
  2. Runs spotify-recent-albums.py as a real subprocess, once per simulated
     "day" -- exactly like the `.github/workflows/spotify-recent-albums.yml`
     cron job would, including a fresh process each time and a state file
     that persists between runs (mimicking git checkout -> commit -> push).
  3. Between simulated days, it:
       - resets the mock server's daily request quota (mirrors Spotify's
         real quota resetting after 24h)
       - if the script left `rate_limits` entries in the future, fast-
         forwards them to "now" (mirrors 24 real hours having passed between
         one cron run and the next, without the test needing to sleep for
         24 hours)
  4. Prints a per-day summary: exit code, artists processed, whether the run
     exited early on a long rate limit, and how many requests the mock
     server saw. At the end it prints an overall summary across all days.

Usage examples:

  # Reproduce the reported failure: default script behavior, 82 artists,
  # a small daily quota that gets tripped mid-run (like real dev-mode).
  python simulate_workflow_harness.py --num-artists 82 --daily-quota 60 --days-to-simulate 5

  # Test a slowed-down version (with min-request-interval flag),
  # passing extra CLI args straight through:
  python simulate_workflow_harness.py --num-artists 82 --daily-quota 60 \\
      --days-to-simulate 10 \\
      --script-args "--min-request-interval 20"

  # Point at a different copy of the script (e.g. a branch with the fix):
  python simulate_workflow_harness.py --script /path/to/modified/spotify-recent-albums.py
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_spotify_server import MockSpotifyServer

DEFAULT_SCRIPT = Path(__file__).resolve().parent / "spotify-recent-albums.py"


def _load_json(path):
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def run_one_day(day_num, script_path, state_file, mock_base_url, days_lookback,
                 interval_days, extra_args, env_extra):
    """Runs the script once as a subprocess, as a single scheduled workflow run would."""
    env = os.environ.copy()
    env.update({
        "SPOTIFY_CLIENT_ID": "mock-client-id",
        "SPOTIFY_CLIENT_SECRET": "mock-client-secret",
        "SPOTIFY_REFRESH_TOKEN": "mock-refresh-token",
        "SPOTIFY_API_BASE_OVERRIDE": mock_base_url + "/v1",
        "SPOTIFY_TOKEN_URL_OVERRIDE": mock_base_url + "/token",
        "SPOTIFY_AUTH_URL_OVERRIDE": mock_base_url + "/authorize",
        "PYTHONUNBUFFERED": "1",
    })
    env.update(env_extra)

    cmd = [sys.executable, str(script_path), "--resume", "--days", str(days_lookback),
           "--interval-days", str(interval_days)]
    if extra_args:
        cmd.extend(shlex.split(extra_args))

    print(f"\n{'=' * 70}")
    print(f"DAY {day_num}: running  {' '.join(cmd[1:])}")
    print(f"{'=' * 70}")

    start = time.time()
    proc = subprocess.run(cmd, cwd=str(script_path.parent), env=env,
                           capture_output=True, text=True, timeout=300)
    elapsed = time.time() - start

    print(proc.stdout)
    if proc.stderr:
        print("--- stderr ---")
        print(proc.stderr)

    return {
        "day": day_num,
        "exit_code": proc.returncode,
        "elapsed_seconds": round(elapsed, 2),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def summarize_day(day_result, state_before, state_after, quota_snapshot):
    processed_before = 0
    processed_after = 0
    if state_before and state_before.get("in_progress"):
        processed_before = len(state_before["in_progress"].get("processed_ids", []))
    if state_after and state_after.get("in_progress"):
        processed_after = len(state_after["in_progress"].get("processed_ids", []))

    run_completed = state_after is not None and state_after.get("in_progress") is None
    known_albums_count = len(state_after.get("known_albums", {})) if state_after else 0
    artists_checked_total = len(state_after.get("artists", {})) if state_after else 0

    rate_limited_exit = day_result["exit_code"] == 2

    print(f"--- Day {day_result['day']} summary ---")
    print(f"  exit code:            {day_result['exit_code']}"
          + ("  (rate-limit exit)" if rate_limited_exit else ""))
    print(f"  wall time:            {day_result['elapsed_seconds']}s")
    print(f"  scan fully completed: {run_completed}")
    print(f"  known_albums total:   {known_albums_count}")
    print(f"  artists w/ last_checked: {artists_checked_total}")
    print(f"  mock server requests since last quota reset: {quota_snapshot['request_count_since_reset']}")
    print(f"  mock server total requests (all time):        {quota_snapshot['total_requests']}")

    return {
        "rate_limited_exit": rate_limited_exit,
        "run_completed": run_completed,
        "known_albums_count": known_albums_count,
        "artists_checked_total": artists_checked_total,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT,
                         help="Path to spotify-recent-albums.py to test (default: the one in spotify/)")
    parser.add_argument("--num-artists", type=int, default=82, help="Number of followed artists to simulate")
    parser.add_argument("--albums-per-artist", type=int, default=1, help="Albums returned per artist")
    parser.add_argument("--daily-quota", type=int, default=None,
                         help="Requests allowed per simulated day before the mock server returns a 24h 429 "
                              "(unset = no daily quota, matches non-dev-mode Spotify)")
    parser.add_argument("--rate-limit-per-minute", type=int, default=None,
                         help="Requests allowed per rolling 60s before a short 429 (default: off)")
    parser.add_argument("--short-429-every", type=int, default=None,
                         help="Every Nth request gets a short 429 with Retry-After: 1 (exercises the retry path)")
    parser.add_argument("--days-lookback", type=int, default=365, help="--days value passed to the script")
    parser.add_argument("--interval-days", type=int, default=3, help="--interval-days value passed to the script")
    parser.add_argument("--days-to-simulate", type=int, default=5, help="How many simulated cron runs to execute")
    parser.add_argument("--script-args", type=str, default="",
                         help="Extra CLI args passed through to the script verbatim, "
                              "e.g. '--min-request-interval 20'")
    parser.add_argument("--fresh-state", action="store_true",
                         help="Start from an empty state file even if one exists next to the script")
    parser.add_argument("--keep-workdir", action="store_true",
                         help="Don't delete the temp working directory at the end (for inspection)")
    args = parser.parse_args()

    if not args.script.exists():
        print(f"Script not found: {args.script}")
        sys.exit(1)

    # Work in a temp copy of the directory so we never touch the real
    # spotify-state.json, and so each harness run starts from a known state.
    workdir = Path(tempfile.mkdtemp(prefix="spotify_harness_"))
    script_copy = workdir / args.script.name
    shutil.copy(args.script, script_copy)
    state_file = workdir / "spotify-state.json"

    existing_state = None if args.fresh_state else _load_json(args.script.parent / "spotify-state.json")
    if existing_state is not None:
        with open(state_file, "w") as f:
            json.dump(existing_state, f, indent=2)
        print(f"Seeded state file from {args.script.parent / 'spotify-state.json'}")
    else:
        print("Starting from an empty state file.")

    server = MockSpotifyServer(
        num_artists=args.num_artists,
        albums_per_artist=args.albums_per_artist,
        daily_quota=args.daily_quota,
        rate_limit_per_minute=args.rate_limit_per_minute,
        short_429_every=args.short_429_every,
    )
    server.start()
    print(f"Mock Spotify server started at {server.base_url}")
    print(f"Simulating {args.num_artists} followed artists, "
          f"daily_quota={args.daily_quota}, rate_limit_per_minute={args.rate_limit_per_minute}")

    day_summaries = []
    try:
        for day in range(1, args.days_to_simulate + 1):
            # Simulate the daily quota resetting (real Spotify quotas reset
            # after 24h; the harness compresses that into "start of day").
            server.reset_quota()

            state_before = _load_json(state_file)

            # Fast-forward any pending long rate-limit wait, simulating that
            # a full day (the cron interval) has passed since the last run.
            if state_before and state_before.get("rate_limits"):
                if day > 1:
                    expired = {k: v for k, v in state_before["rate_limits"].items()
                               if v is not None and v > int(time.time())}
                    if expired:
                        print(f"[harness] Fast-forwarding rate_limits to simulate 24h passing before day {day}'s run...")
                        for k in expired:
                            state_before["rate_limits"][k] = int(time.time()) - 1
                        with open(state_file, "w") as f:
                            json.dump(state_before, f, indent=2)

            result = run_one_day(
                day_num=day,
                script_path=script_copy,
                state_file=state_file,
                mock_base_url=server.base_url,
                days_lookback=args.days_lookback,
                interval_days=args.interval_days,
                extra_args=args.script_args,
                env_extra={},
            )

            state_after = _load_json(state_file)
            quota_snapshot = server.snapshot()
            summary = summarize_day(result, state_before, state_after, quota_snapshot)
            summary["day"] = day
            day_summaries.append(summary)

    finally:
        server.stop()

    print(f"\n{'#' * 70}")
    print("OVERALL SUMMARY")
    print(f"{'#' * 70}")
    for s in day_summaries:
        flag = "RATE-LIMITED EXIT" if s["rate_limited_exit"] else ("completed" if s["run_completed"] else "incomplete?")
        print(f"  Day {s['day']}: {flag:20s}  known_albums={s['known_albums_count']:4d}  "
              f"artists_checked={s['artists_checked_total']:4d}")

    final = day_summaries[-1] if day_summaries else None
    if final:
        full_cycle_done = final["artists_checked_total"] >= args.num_artists
        print(f"\nAll {args.num_artists} artists checked at least once: "
              f"{'YES' if full_cycle_done else 'NO (' + str(final['artists_checked_total']) + '/' + str(args.num_artists) + ')'}")
        rate_limit_days = sum(1 for s in day_summaries if s["rate_limited_exit"])
        print(f"Days that hit a rate-limit exit: {rate_limit_days}/{len(day_summaries)}")

    if args.keep_workdir:
        print(f"\nWorking directory preserved at: {workdir}")
        print(f"Final state file: {state_file}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
