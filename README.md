# Spotify Recent Albums

Finds albums released in the past year from artists you follow on Spotify.

## Setup

1. Create a Spotify app at https://developer.spotify.com/dashboard
2. Run the auth flow once locally:
   ```bash
   SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy python spotify-recent-albums.py --auth
   ```
3. Store the refresh token as `SPOTIFY_REFRESH_TOKEN` in GitHub Actions secrets along with `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`
4. Go into Spotify and create the playlist you want to update and set `SPOTIFY_PLAYLIST_ID` in GitHub Actions secrets.

## Usage

```bash
# Standard run
SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy SPOTIFY_REFRESH_TOKEN=zzz \
  python spotify-recent-albums.py

# Resume after a rate-limit exit (the default in CI)
python spotify-recent-albums.py --resume --days 365

# Conservative run with batching
python spotify-recent-albums.py --resume --batch-size 5 --min-request-interval 20
```

### CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--resume` | off | Resume a previously interrupted run |
| `--days` | 365 | Look back N days for new releases |
| `--interval-days` | 7 | Check each artist every N days |
| `--batch-size` | 5 | Max artists to process per run |
| `--min-request-interval` | 20 | Minimum seconds between API calls (0 to disable) |
| `--market` | US | ISO 3166-1 alpha-2 country code |
| `--json` | off | Output raw JSON instead of markdown |
| `--debug` | off | Print debug info while fetching |

## Testing with the Simulate Workflow Harness

The harness (`simulate_workflow_harness.py`) runs the script against a local mock Spotify server, simulating multiple daily cron runs to verify behavior under rate limits and batching.

### Basic usage

```bash
# From the spotify/ directory:
python simulate_workflow_harness.py --num-artists 82 --daily-quota 60 --days-to-simulate 5
```

This reproduces the original failure: 82 artists, a small dev-mode daily quota that gets tripped mid-run.

### Testing the slowdown fix

```bash
# With batching enabled (should avoid hitting the quota):
python simulate_workflow_harness.py \
  --num-artists 82 --daily-quota 60 --days-to-simulate 20 --fresh-state \
  --script-args "--batch-size 5 --min-request-interval 0"
```

(`--min-request-interval 0` keeps the simulated test fast; use `20` for production.)

### Full flag reference

| Flag | Default | Description |
|------|---------|-------------|
| `--script` | `spotify-recent-albums.py` | Path to the script under test |
| `--num-artists` | 82 | Number of simulated followed artists |
| `--albums-per-artist` | 1 | Albums returned per artist |
| `--daily-quota` | None (unlimited) | Requests/day before the mock returns a 24h 429 |
| `--rate-limit-per-minute` | None (off) | Rolling 60s request cap |
| `--short-429-every` | None (off) | Every Nth request gets a short 429 |
| `--days-lookback` | 365 | `--days` value passed to the script |
| `--interval-days` | 7 | `--interval-days` value passed to the script |
| `--days-to-simulate` | 5 | Number of simulated cron runs to execute |
| `--script-args` | "" | Extra CLI args passed through to the script |
| `--fresh-state` | off | Start from an empty state file |
| `--keep-workdir` | off | Preserve the temp directory for inspection |

### What to look for in the output

The harness prints a per-day summary and an overall summary. A healthy run shows:

- No day reports `RATE-LIMITED EXIT`
- `All N artists checked at least once: YES` is reached within `ceil(N / batch_size)` days
- `known_albums` grows steadily across days
