#!/usr/bin/env python3
"""
Spotify Recent Albums from Followed Artists

Finds albums released in the past year from artists you follow on Spotify.
Uses a staggered checking schedule to avoid Spotify dev-mode quota lockouts.

Usage:
  1. Initial setup (one-time, local):
     SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy python spotify-recent-albums.py --auth

  2. Run (CI or local):
     SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy SPOTIFY_REFRESH_TOKEN=zzz python spotify-recent-albums.py

Environment variables:
  SPOTIFY_CLIENT_ID      - Spotify app client ID
  SPOTIFY_CLIENT_SECRET  - Spotify app client secret
  SPOTIFY_REFRESH_TOKEN  - OAuth refresh token (obtained via --auth flow)

  Testing-only overrides (do not set these in production; used by the mock
  test harness in spotify/tests/ to point the script at a local fake server
  instead of the real Spotify API):
  SPOTIFY_API_BASE_OVERRIDE
  SPOTIFY_TOKEN_URL_OVERRIDE
  SPOTIFY_AUTH_URL_OVERRIDE

Rate Limiting and Error Handling:
  - Uses a conservative 120 requests/minute budget to stay under Spotify limits
  - Fully respects Retry-After and X-RateLimit-Reset headers
  - Stops immediately on non-retryable Spotify errors such as invalid limit/offset/market
  - Saves progress to spotify-state.json for resume after throttling or interruption
  - Use --resume to continue after a rate limit or interruption

Staggered Checking:
  - Only checks a rotating subset of artists each run (default: 1/7th per day)
  - Every artist is checked at least once per interval (default: 7 days)
  - Previous albums still appear in the report even if the artist wasn't checked today

Playlist Sync:
  - When SPOTIFY_PLAYLIST_ID is set, newly-found albums get their tracks added
    to that playlist automatically.
  - Albums that age out of the --days window automatically get their tracks
    removed from the playlist on every run (see prune_expired_playlist_tracks).
  - Uses the /playlists/{id}/items endpoints (POST/GET/DELETE), per Spotify's
    February 2026 Dev Mode migration -- the old /playlists/{id}/tracks
    endpoints are removed for Development Mode apps as of March 9, 2026.
"""

import os
import re
import sys
import json
import time
import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests")
    sys.exit(1)

# Testing-only overrides: allows the mock test harness to point the script at
# a local fake server. Unset in production, so these default to the real
# Spotify endpoints and behavior is unchanged.
SPOTIFY_AUTH_URL = os.environ.get("SPOTIFY_AUTH_URL_OVERRIDE", "https://accounts.spotify.com/authorize")
SPOTIFY_TOKEN_URL = os.environ.get("SPOTIFY_TOKEN_URL_OVERRIDE", "https://accounts.spotify.com/api/token")
SPOTIFY_API_BASE = os.environ.get("SPOTIFY_API_BASE_OVERRIDE", "https://api.spotify.com/v1")
REDIRECT_URI = "http://127.0.0.1:8443/callback"
TOKEN_CACHE = Path.cwd() / ".spotify_token_cache.json"
STATE_FILE = Path.cwd() / "spotify-state.json"
CHECK_INTERVAL_DAYS = 3
LONG_WAIT_THRESHOLD_SECONDS = 300
MAX_REQUESTS_PER_MINUTE = 120
MAX_LONG_RATE_LIMIT_WAIT_SECONDS = 3600
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 10


_ID_SEGMENT = re.compile(r"/[A-Za-z0-9]{15,}(?=/|$)")


def endpoint_category(method, url):
    path = url.split("?", 1)[0]
    if path.startswith(SPOTIFY_API_BASE):
        path = path[len(SPOTIFY_API_BASE):]
    normalized = _ID_SEGMENT.sub("/{id}", path)
    return f"{method} {normalized}"


class LongRateLimitBlock(Exception):
    """Raised when a request's category is rate-limited past
    LONG_WAIT_THRESHOLD_SECONDS. Callers decide whether to abort the whole
    run or just skip this phase and continue with unrelated work."""
    def __init__(self, category, retry_until):
        self.category = category
        self.retry_until = retry_until
        super().__init__(f"{category} blocked until {retry_until}")


def log(message):
    print(message, flush=True)


PAREN_PATTERN = re.compile(r"\(.*?\)\s*$")


def is_auto_excluded(album_name):
    return bool(PAREN_PATTERN.search(album_name.strip()))


def is_effectively_excluded(album):
    override = album.get("manual_override")
    if override is not None:
        return override
    return album.get("auto_excluded", False)


def parse_retry_after(value, default=1):
    if not value:
        return default
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(value)
            if dt is None:
                return default
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(dt.tzinfo)).total_seconds()
            return max(1, int(delta))
        except Exception:
            return default


def get_rate_limit_wait(resp, default_backoff=1):
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        return max(parse_retry_after(retry_after, default_backoff), default_backoff)

    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            wait = int(reset) - int(time.time())
            if wait > 0:
                return max(wait, default_backoff)
        except (TypeError, ValueError):
            pass

    return default_backoff


def is_non_retryable_spotify_error(resp):
    if resp.status_code not in {400, 401, 403, 404, 422}:
        return False
    body = resp.text or ""
    lower_body = body.lower()
    return "invalid limit" in lower_body or "invalid offset" in lower_body or "invalid market" in lower_body


def should_exit_for_long_wait(seconds, max_wait_seconds=MAX_LONG_RATE_LIMIT_WAIT_SECONDS):
    return seconds > max_wait_seconds


def wait_with_progress(seconds, message_prefix="Rate limited"):
    if seconds <= 0:
        return
    if should_exit_for_long_wait(seconds):
        log(f"{message_prefix}. Wait of {seconds}s exceeds the maximum allowed wait of {MAX_LONG_RATE_LIMIT_WAIT_SECONDS}s; exiting.")
        raise SystemExit(2)
    if seconds > 60:
        log(f"{message_prefix}. Continuing to wait with periodic progress updates...")
        remaining = int(seconds)
        while remaining > 0:
            chunk = min(60, remaining)
            time.sleep(chunk)
            remaining -= chunk
            if remaining > 0:
                log(f"{message_prefix}. {remaining}s remaining...")
        return
    time.sleep(seconds)


class RateLimiter:
    def __init__(self, max_requests, min_interval_seconds=0):
        self.max_requests = max_requests
        self.min_interval_seconds = min_interval_seconds
        self.timestamps = []
        self.last_request_time = None

    def wait_if_needed(self, label="request"):
        now = time.time()

        if self.min_interval_seconds and self.last_request_time is not None:
            elapsed = now - self.last_request_time
            if elapsed < self.min_interval_seconds:
                sleep_time = self.min_interval_seconds - elapsed
                print(f"  Rate limiter: waiting {sleep_time:.1f}s before {label} (min {self.min_interval_seconds}s between requests)")
                time.sleep(sleep_time)
                now = time.time()

        self.timestamps = [t for t in self.timestamps if now - t < 60]
        if len(self.timestamps) >= self.max_requests:
            sleep_time = 60 - (now - self.timestamps[0]) + 0.1
            if sleep_time > 0:
                print(f"  Rate limiter: waiting {sleep_time:.1f}s before {label} (hit {self.max_requests}/min limit)")
                time.sleep(sleep_time)

        self.last_request_time = time.time()
        self.timestamps.append(self.last_request_time)


rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE)


def get_client_credentials():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Error: Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET env vars.")
        sys.exit(1)
    return client_id, client_secret


def get_access_token(client_id, client_secret, refresh_token):
    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=(10, 20))
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"]


def spotify_request(method, token, url, state, params=None, json_data=None, retries=5, backoff=1):
    category = endpoint_category(method, url)

    blocked_until = state.get("rate_limits", {}).get(category)
    if blocked_until and int(time.time()) < blocked_until:
        raise LongRateLimitBlock(category, blocked_until)

    rate_limiter.wait_if_needed(f"{method} {url.split('?')[0]}")
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


def spotify_get(token, url, state, params=None):
    return spotify_request("GET", token, url, state, params=params)


def spotify_post(token, url, state, json_data=None):
    return spotify_request("POST", token, url, state, json_data=json_data)


def spotify_delete(token, url, state, json_data=None):
    return spotify_request("DELETE", token, url, state, json_data=json_data)


def get_followed_artists(token, state):
    artists = []
    url = f"{SPOTIFY_API_BASE}/me/following"
    params = {"type": "artist", "limit": 50}
    while True:
        data = spotify_get(token, url, state, params)
        items = data.get("artists", {}).get("items", [])
        if not items:
            break
        artists.extend(items)
        after = data.get("artists", {}).get("cursors", {}).get("after")
        if not after:
            break
        params["after"] = after
    return artists


def get_artist_albums(token, artist_id, state, market="US"):
    albums = []
    url = f"{SPOTIFY_API_BASE}/artists/{artist_id}/albums"
    limit = 10
    offset = 0
    while offset < 1000:
        params = {"include_groups": "album", "limit": limit, "offset": offset, "market": market}
        data = spotify_get(token, url, state, params)
        if data is None:
            break
        items = data.get("items", [])
        if not items:
            break
        albums.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return albums


def search_artist_albums(token, artist_name, state, market="US"):
    albums = []
    url = f"{SPOTIFY_API_BASE}/search"
    limit = 10
    offset = 0
    while offset < 1000:
        params = {"q": f'artist:"{artist_name}"', "type": "album", "limit": limit, "offset": offset, "market": market}
        data = spotify_get(token, url, state, params)
        if data is None:
            break
        items = data.get("albums", {}).get("items", [])
        if not items:
            break
        albums.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return albums


def parse_release_date(date_str):
    parts = date_str.split("-")
    if len(parts) == 3:
        return datetime.strptime(date_str, "%Y-%m-%d")
    elif len(parts) == 2:
        return datetime.strptime(date_str, "%Y-%m")
    elif len(parts) == 1:
        return datetime.strptime(date_str, "%Y")
    return None


def format_markdown_table(albums):
    if not albums:
        return "No new albums found in the past year.\n"

    lines = [
        "| Artist | Album | Type | Release Date |",
        "|--------|-------|------|--------------|",
    ]
    for a in sorted(albums, key=lambda x: x["release_date"], reverse=True):
        artist = a["artist"].replace("|", "\\|")
        name = a["name"].replace("|", "\\|")
        lines.append(f"| {artist} | [{name}]({a['url']}) | {a['type'].capitalize()} | {a['release_date']} |")
    lines.append("")
    lines.append(f"*{len(albums)} release(s) found*")
    return "\n".join(lines)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)
            state.setdefault("rate_limits", {})
            return state
    return {"artists": {}, "known_albums": {}, "in_progress": None, "rate_limits": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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
    if not due and artists:
        checked = []
        for artist in artists:
            entry = state["artists"].get(artist["id"])
            if entry is not None:
                checked.append((artist, datetime.fromisoformat(entry["last_checked"])))
        checked.sort(key=lambda x: x[1])
        due = [a for a, _ in checked[:min(len(checked), max(1, len(artists) // interval_days))]]
    return due


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


def add_tracks_to_playlist(token, playlist_id, track_uris, state):
    # NOTE: Spotify's February 2026 Dev Mode migration (enforced for existing
    # apps as of March 9, 2026) removed POST /playlists/{id}/tracks for
    # Development Mode apps. Use /playlists/{id}/items instead.
    url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items"
    for i in range(0, len(track_uris), 100):
        chunk = track_uris[i:i + 100]
        spotify_request("POST", token, url, state, json_data={"uris": chunk})


def remove_tracks_from_playlist(token, playlist_id, track_uris, state):
    # Same migration as add_tracks_to_playlist: DELETE /playlists/{id}/tracks
    # is removed; use /playlists/{id}/items, and the body param is renamed
    # from "tracks" to "items" (each item is {"uri": "..."}).
    url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items"
    for i in range(0, len(track_uris), 100):
        chunk = track_uris[i:i + 100]
        items = [{"uri": uri} for uri in chunk]
        spotify_request("DELETE", token, url, state, json_data={"items": items})


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


def do_auth_flow(client_id, client_secret):
    state = "spotify_auth_state"
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "user-follow-read playlist-modify-public playlist-modify-private",
        "state": state,
        "show_dialog": "true",
    }
    auth_url = f"{SPOTIFY_AUTH_URL}?{urlencode(params)}"

    print(f"\nOpen this URL in your browser to authorize:\n\n  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    received_code = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal received_code
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if "code" in qs:
                received_code = qs["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Authorization successful! You can close this tab.</h1>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Authorization failed.")
        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 8443), Handler)
    print("Waiting for authorization (listening on http://127.0.0.1:8443)...")
    while received_code is None:
        server.handle_request()

    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": received_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    resp.raise_for_status()
    data = resp.json()

    print(f"\nRefresh token (save this as SPOTIFY_REFRESH_TOKEN):\n\n  {data['refresh_token']}\n")
    return data["refresh_token"]


def main():
    parser = argparse.ArgumentParser(description="Find recent albums from followed Spotify artists")
    parser.add_argument("--auth", action="store_true", help="Run the initial OAuth authorization flow")
    parser.add_argument("--resume", action="store_true", help="Resume a previously interrupted run")
    parser.add_argument("--days", type=int, default=365, help="Look back N days (default: 365)")
    parser.add_argument("--interval-days", type=int, default=CHECK_INTERVAL_DAYS,
                        help=f"Check each artist every N days (default: {CHECK_INTERVAL_DAYS})")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of markdown")
    parser.add_argument("--debug", action="store_true", help="Print debug info while fetching")
    parser.add_argument("--test-artist", type=str, default=None, help="Test with a single artist by name (uses search)")
    parser.add_argument("--test-id", type=str, default=None, help="Test with a single artist by ID (uses artist albums endpoint)")
    parser.add_argument("--market", type=str, default="US", help="ISO 3166-1 alpha-2 country code (default: US)")
    parser.add_argument("--min-request-interval", type=float, default=DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
                        help=f"Minimum seconds between any two Spotify API calls (default: {DEFAULT_MIN_REQUEST_INTERVAL_SECONDS}). "
                             f"Set to 0 to disable.")
    args = parser.parse_args()
    rate_limiter.min_interval_seconds = args.min_request_interval

    client_id, client_secret = get_client_credentials()

    if args.auth:
        do_auth_flow(client_id, client_secret)
        return

    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    if not refresh_token:
        print("Error: Set SPOTIFY_REFRESH_TOKEN env var (run with --auth first).")
        sys.exit(1)

    token = get_access_token(client_id, client_secret, refresh_token)
    cutoff = datetime.now() - timedelta(days=args.days)
    state = load_state()

    if args.test_artist or args.test_id:
        if args.test_artist:
            print(f"Testing search_artist_albums for: {args.test_artist}")
            test_artist = {"name": args.test_artist, "id": "test"}
            albums = search_artist_albums(token, args.test_artist, state, args.market)
        else:
            print(f"Testing get_artist_albums for ID: {args.test_id}")
            test_artist = {"name": "test", "id": args.test_id}
            albums = get_artist_albums(token, args.test_id, state, args.market)
        print(f"Found {len(albums)} album(s):")
        for a in albums:
            print(f"  {a['name']} ({a['release_date']}) - {a['album_type']}")
        return

    blocked_categories = []

    # --- Phase 1: fetch followed artists ---
    artists = []
    try:
        log("Fetching followed artists...")
        artists = get_followed_artists(token, state)
        log(f"Found {len(artists)} followed artists.")
    except LongRateLimitBlock as e:
        log(f"Skipping artist scan this run -- {e.category} is rate-limited "
            f"until {datetime.fromtimestamp(e.retry_until, tz=timezone.utc)}.")
        blocked_categories.append(e.category)

    # --- Phase 2: due-artist scan loop (only if phase 1 succeeded) ---
    if artists:
        if args.resume and state.get("in_progress") is not None:
            ip = state["in_progress"]
            processed_ids = set(ip["processed_ids"])
            due_ids = ip["due_ids"]
            due_artists = [a for a in artists if a["id"] in due_ids]
            log(f"Resuming: {len(due_artists) - len(processed_ids)}/{len(due_artists)} artists remaining "
                f"(interval: {args.interval_days}d)")
        else:
            due_artists = get_due_artists(artists, state, args.interval_days)
            processed_ids = set()
            state["in_progress"] = {
                "due_ids": [a["id"] for a in due_artists],
                "processed_ids": [],
            }
            save_state(state)
            log(f"{len(due_artists)} due artists selected for this run (interval: {args.interval_days}d)")

        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            log("Starting artist scan...")
            for i, artist in enumerate(due_artists, 1):
                if artist["id"] in processed_ids:
                    continue

                log(f"  [{i}/{len(due_artists)}] {artist['name']} - fetching albums...")
                if i % 10 == 0 or i == len(due_artists):
                    log(f"  Progress checkpoint: {i}/{len(due_artists)} artists processed")
                try:
                    albums = get_artist_albums(token, artist["id"], state, args.market)
                except Exception as e:
                    log(f"    ERROR: {artist['name']} ({artist['id']}): {e}")
                    if args.debug:
                        print("    Skipping due to error.")
                    processed_ids.add(artist["id"])
                    state["in_progress"]["processed_ids"] = list(processed_ids)
                    save_state(state)
                    continue

                log(f"    Retrieved {len(albums)} album(s)")
                new_count = 0
                for album in albums:
                    if album["album_type"] != "album":
                        if args.debug:
                            print(f"      SKIP: {album['name']} ({album['album_type']}) - not an album")
                        continue
                    artist_ids = [a["id"] for a in album.get("artists", [])]
                    if artist["id"] not in artist_ids:
                        if args.debug:
                            print(f"      SKIP: {album['name']} - artist ID mismatch (got {artist_ids}, want {artist['id']})")
                        continue
                    release_date = parse_release_date(album["release_date"])
                    if release_date and release_date >= cutoff:
                        if args.debug:
                            print(f"      INCLUDE: {album['name']} ({album['release_date']})")
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
                        new_count += 1
                    elif not release_date:
                        if args.debug:
                            print(f"      INCLUDE: {album['name']} ({album['release_date']!r}) - bad date, including anyway")
                        record_album(state, artist, album, now_iso)
                        new_count += 1
                    elif args.debug:
                        print(f"      SKIP: {album['name']} ({album['release_date']}) - before cutoff")

                if new_count:
                    log(f"    Added {new_count} new album(s)")
                else:
                    log("    No new albums added")

                state["artists"][artist["id"]] = {"name": artist["name"], "last_checked": now_iso}
                processed_ids.add(artist["id"])
                state["in_progress"]["processed_ids"] = list(processed_ids)
                save_state(state)

        except LongRateLimitBlock as e:
            log(f"Stopping artist scan -- {e.category} is rate-limited until "
                f"{datetime.fromtimestamp(e.retry_until, tz=timezone.utc)}. "
                f"Progress so far is saved; will resume next run.")
            blocked_categories.append(e.category)

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


if __name__ == "__main__":
    main()