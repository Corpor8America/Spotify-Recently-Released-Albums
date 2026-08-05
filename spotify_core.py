"""
Core Spotify logic for the containerized web app.

Adapted from the original spotify-recent-albums.py CLI script:
  - STATE_FILE / TOKEN_FILE now live under DATA_DIR (a mounted Docker
    volume) instead of next to the script and instead of being committed
    to a git branch.
  - The OAuth flow no longer spins up a localhost HTTPServer / opens a
    browser (that only works for a human running the CLI locally). It's
    split into get_auth_url() / exchange_code_for_token(), which the Flask
    app wires up to real HTTP routes (/login, /callback).
  - main()'s scan logic is exposed as run_scan(), callable both from the
    web app's "Run now" button and from the APScheduler background job.

Everything else (rate limiting, staggered checking, exclusion filtering,
playlist sync/prune) is unchanged from the original script.
"""

import os
import re
import json
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

# --- Paths (volume-backed) -------------------------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "spotify-state.json"
TOKEN_FILE = DATA_DIR / "spotify-token.json"   # stores the refresh_token once OAuth completes

SPOTIFY_AUTH_URL = os.environ.get("SPOTIFY_AUTH_URL_OVERRIDE", "https://accounts.spotify.com/authorize")
SPOTIFY_TOKEN_URL = os.environ.get("SPOTIFY_TOKEN_URL_OVERRIDE", "https://accounts.spotify.com/api/token")
SPOTIFY_API_BASE = os.environ.get("SPOTIFY_API_BASE_OVERRIDE", "https://api.spotify.com/v1")

CHECK_INTERVAL_DAYS = int(os.environ.get("INTERVAL_DAYS", "3"))
LONG_WAIT_THRESHOLD_SECONDS = 300
MAX_REQUESTS_PER_MINUTE = 120
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = float(os.environ.get("MIN_REQUEST_INTERVAL", "10"))
DEFAULT_DAYS_LOOKBACK = int(os.environ.get("DAYS_LOOKBACK", "365"))

CONFIG_FILE = DATA_DIR / "app-config.json"


def load_config():
    config = {
        "spotify_client_id": "",
        "spotify_client_secret": "",
        "spotify_playlist_id": "",
        "interval_days": int(os.environ.get("INTERVAL_DAYS", "3")),
        "min_request_interval": float(os.environ.get("MIN_REQUEST_INTERVAL", "10")),
        "days_lookback": int(os.environ.get("DAYS_LOOKBACK", "365")),
        "cron_schedule": os.environ.get("CRON_SCHEDULE", "0 6 * * *"),
        "public_base_url": os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        "flask_secret_key": "",
    }
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
            config.update(saved)
    if not config["flask_secret_key"]:
        import secrets
        config["flask_secret_key"] = secrets.token_hex(32)
        save_config(config)
    return config


def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    tmp.replace(CONFIG_FILE)


def is_configured():
    cfg = load_config()
    return bool(cfg.get("spotify_client_id")) and bool(cfg.get("spotify_client_secret"))


_ID_SEGMENT = re.compile(r"/[A-Za-z0-9]{15,}(?=/|$)")
PAREN_PATTERN = re.compile(r"\(.*?\)\s*$")

# Serializes scan runs so the scheduler and a manual "Run now" click can
# never overlap.
run_lock = threading.Lock()

# Serializes playlist reorder runs.
reorder_lock = threading.Lock()

# In-memory ring buffer of recent log lines, shown on the dashboard.
_log_lines = []
_log_lock = threading.Lock()

# Cancel event -- set by POST /cancel to abort a running scan.
_cancel_event = threading.Event()


def clear_logs():
    with _log_lock:
        _log_lines.clear()


def cancel_scan():
    _cancel_event.set()
    # Also clear any in-progress state so a resume doesn't pick it up.
    state = load_state()
    state["in_progress"] = None
    save_state(state)


def log(message):
    print(message, flush=True)
    with _log_lock:
        _log_lines.append(f"{datetime.now().strftime('%H:%M:%S')}  {message}")
        del _log_lines[:-500]


def get_recent_logs():
    with _log_lock:
        return list(_log_lines)


# --- Exclusion filter --------------------------------------------------

def is_auto_excluded(album_name):
    return bool(PAREN_PATTERN.search(album_name.strip()))


def is_effectively_excluded(album):
    override = album.get("manual_override")
    if override is not None:
        return override
    return album.get("auto_excluded", False)


# --- Rate limiting / request plumbing (unchanged from the CLI script) ------

def endpoint_category(method, url):
    path = url.split("?", 1)[0]
    if path.startswith(SPOTIFY_API_BASE):
        path = path[len(SPOTIFY_API_BASE):]
    normalized = _ID_SEGMENT.sub("/{id}", path)
    return f"{method} {normalized}"


class LongRateLimitBlock(Exception):
    def __init__(self, category, retry_until):
        self.category = category
        self.retry_until = retry_until
        super().__init__(f"{category} blocked until {retry_until}")


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
                log(f"  Rate limiter: waiting {sleep_time:.1f}s before {label} (min {self.min_interval_seconds}s between requests)")
                time.sleep(sleep_time)
                now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < 60]
        if len(self.timestamps) >= self.max_requests:
            sleep_time = 60 - (now - self.timestamps[0]) + 0.1
            if sleep_time > 0:
                log(f"  Rate limiter: waiting {sleep_time:.1f}s before {label} (hit {self.max_requests}/min limit)")
                time.sleep(sleep_time)
        self.last_request_time = time.time()
        self.timestamps.append(self.last_request_time)


rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE, DEFAULT_MIN_REQUEST_INTERVAL_SECONDS)


def is_non_retryable_spotify_error(resp):
    if resp.status_code not in {400, 401, 403, 404, 422}:
        return False
    body = (resp.text or "").lower()
    return "invalid limit" in body or "invalid offset" in body or "invalid market" in body


def spotify_request(method, token, url, state, params=None, json_data=None, retries=5, backoff=1):
    category = endpoint_category(method, url)
    blocked_until = state.get("rate_limits", {}).get(category)
    if blocked_until:
        if int(time.time()) < blocked_until:
            raise LongRateLimitBlock(category, blocked_until)
        del state["rate_limits"][category]
        save_state(state)

    rate_limiter.wait_if_needed(f"{method} {url.split('?')[0]}")
    headers = {"Authorization": f"Bearer {token}"}
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, params=params, json=json_data)

    if resp.status_code == 429:
        retry_after_raw = resp.headers.get("Retry-After", str(backoff))
        retry_after = int(retry_after_raw)
        if retry_after > LONG_WAIT_THRESHOLD_SECONDS:
            retry_until = int(time.time()) + retry_after
            state.setdefault("rate_limits", {})[category] = retry_until
            save_state(state)
            log(f"  Rate limited on {method} {url} (category: {category}); "
                f"Retry-After={retry_after_raw!r}; blocked for {retry_after}s "
                f"until {datetime.fromtimestamp(retry_until, tz=timezone.utc).isoformat()}.")
            raise LongRateLimitBlock(category, retry_until)
        if retries <= 0:
            raise Exception("Rate limited - max retries exceeded")
        wait = max(retry_after, backoff)
        time.sleep(wait)
        return spotify_request(method, token, url, state, params, json_data, retries - 1, wait * 2)

    if resp.status_code in (502, 503, 504):
        if retries <= 0:
            raise RuntimeError(f"Server error {resp.status_code} - max retries exceeded")
        time.sleep(backoff)
        return spotify_request(method, token, url, state, params, json_data, retries - 1, backoff * 2)

    if resp.status_code >= 400:
        if is_non_retryable_spotify_error(resp):
            raise RuntimeError(f"Spotify API request failed: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()

    if resp.status_code == 204:
        return {}
    return resp.json()


def spotify_get(token, url, state, params=None):
    return spotify_request("GET", token, url, state, params=params)


# --- OAuth (web-flow, not CLI-localhost-flow) -------------------------------

def get_auth_url(client_id, redirect_uri, csrf_state):
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "user-follow-read playlist-modify-public playlist-modify-private",
        "state": csrf_state,
        "show_dialog": "false",
    }
    return f"{SPOTIFY_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(client_id, client_secret, code, redirect_uri):
    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    resp.raise_for_status()
    return resp.json()  # contains access_token + refresh_token


def get_access_token(client_id, client_secret, refresh_token):
    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def save_refresh_token(refresh_token):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump({"refresh_token": refresh_token, "saved_at": datetime.now(timezone.utc).isoformat()}, f)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def load_refresh_token():
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            return json.load(f).get("refresh_token")
    return os.environ.get("SPOTIFY_REFRESH_TOKEN")  # allow seeding via env on first boot


def is_connected():
    return load_refresh_token() is not None


# --- State file --------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)
            state.setdefault("rate_limits", {})
            state.setdefault("artists", {})
            state.setdefault("known_albums", {})
            state.setdefault("in_progress", None)
            return state
    return {"artists": {}, "known_albums": {}, "in_progress": None, "rate_limits": {}}


def clear_expired_rate_limits(state, now=None):
    now_ts = int(now if now is not None else time.time())
    rate_limits = state.setdefault("rate_limits", {})
    expired = [category for category, retry_until in rate_limits.items()
               if int(retry_until) <= now_ts]
    for category in expired:
        del rate_limits[category]
    return bool(expired)


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)  # atomic on POSIX, avoids a torn write on crash


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
        batch_size = max(1, len(artists) // max(1, interval_days))
        due = [artist for artist, _ in checked[:min(len(checked), batch_size)]]
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
        "added_to_playlist": existing.get("added_to_playlist", False),
        "track_uris": existing.get("track_uris", []),
    }


def parse_release_date(date_str):
    if not date_str:
        return None
    parts = date_str.split("-")
    if len(parts) == 3:
        return datetime.strptime(date_str, "%Y-%m-%d")
    elif len(parts) == 2:
        return datetime.strptime(date_str, "%Y-%m")
    elif len(parts) == 1:
        return datetime.strptime(date_str, "%Y")
    return None


def get_report_albums(state, days):
    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for album_id, album in state.get("known_albums", {}).items():
        if is_effectively_excluded(album):
            continue
        release_date = parse_release_date(album["release_date"])
        if release_date is None or release_date >= cutoff:
            entry = dict(album)
            entry["id"] = album_id
            result.append(entry)
    def _sort_key(a):
        d = parse_release_date(a["release_date"])
        return d if d is not None else datetime.min
    result.sort(key=_sort_key, reverse=True)
    return result


def get_excluded_albums(state):
    result = []
    for album_id, album in state.get("known_albums", {}).items():
        if is_effectively_excluded(album):
            entry = dict(album)
            entry["id"] = album_id
            result.append(entry)
    result.sort(key=lambda a: a.get("release_date", ""), reverse=True)
    return result


# --- Spotify API calls ---------------------------------------------------

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
    limit, offset = 10, 0
    while offset < 1000:
        params = {"include_groups": "album", "limit": limit, "offset": offset, "market": market}
        data = spotify_get(token, url, state, params)
        items = data.get("items", [])
        if not items:
            break
        albums.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return albums


def get_album_track_uris(token, album_id, state):
    uris = []
    url = f"{SPOTIFY_API_BASE}/albums/{album_id}/tracks"
    limit, offset = 50, 0
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


def get_playlist_track_uris(token, playlist_id, state):
    """Return every track URI currently in a playlist, including duplicates."""
    uris = []
    url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items"
    limit, offset = 100, 0
    while True:
        data = spotify_get(token, url, state, {"limit": limit, "offset": offset})
        items = data.get("items", [])
        uris.extend(
            item["track"]["uri"] for item in items
            if item.get("track") and item["track"].get("uri")
        )
        total = data.get("total")
        if not items or len(items) < limit or (total is not None and len(uris) >= total):
            break
        offset += limit
    return uris


def add_tracks_to_playlist(token, playlist_id, track_uris, state):
    url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items"
    for i in range(0, len(track_uris), 100):
        spotify_request("POST", token, url, state, json_data={"uris": track_uris[i:i + 100]})


def remove_tracks_from_playlist(token, playlist_id, track_uris, state):
    url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items"
    for i in range(0, len(track_uris), 100):
        items = [{"uri": u} for u in track_uris[i:i + 100]]
        spotify_request("DELETE", token, url, state, json_data={"items": items})


def prune_playlist(token, state, days, playlist_id):
    if not playlist_id:
        return
    cutoff = datetime.now() - timedelta(days=days)
    known = state.get("known_albums", {})
    removal_ids, keep_uris = [], set()
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
    log(f"Pruning {len(removal_ids)} album(s) from playlist (aged-out or excluded)...")
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
                log(f"  Removed {len(to_remove)} track(s) from '{album['name']}'")
            except Exception as e:
                log(f"  ERROR removing '{album['name']}' from playlist: {e}")
                continue
        album["added_to_playlist"] = False
        album["track_uris"] = []
        save_state(state)


def reorder_playlist(token, state, playlist_id):
    """Reorders the playlist so tracks are sorted by album release date
    (oldest first). Deletes all current tracks and re-adds them in the
    desired order."""
    if not playlist_id:
        return

    albums = [
        a for a in state.get("known_albums", {}).values()
        if a.get("added_to_playlist") and not is_effectively_excluded(a)
    ]

    def sort_key(album):
        parsed = parse_release_date(album["release_date"])
        return parsed if parsed is not None else datetime.min

    albums.sort(key=sort_key)

    ordered_uris = []
    for album in albums:
        ordered_uris.extend(album.get("track_uris") or [])

    if not ordered_uris:
        log("No playlisted tracks found to reorder.")
        return

    # Rebuild from persisted album state, but fetch the existing playlist so
    # the delete phase truly clears every item before the replacement is added.
    current_uris = get_playlist_track_uris(token, playlist_id, state)

    # Dev Mode apps can't PUT (replace) a playlist. Delete all current
    # tracks then POST them back in the desired order.
    log(f"Reordering {len(ordered_uris)} track(s) from {len(albums)} album(s)...")
    if current_uris:
        remove_tracks_from_playlist(token, playlist_id, current_uris, state)
    add_tracks_to_playlist(token, playlist_id, ordered_uris, state)
    log("Playlist reorder complete.")


def create_playlist(token, name, description=None):
    """Creates a private playlist for the authenticated user and returns
    its Spotify ID."""
    me = spotify_request("GET", token, f"{SPOTIFY_API_BASE}/me", {})
    body = {"name": name, "public": False}
    if description:
        body["description"] = description
    resp = spotify_request(
        "POST", token, f"{SPOTIFY_API_BASE}/users/{me['id']}/playlists", {},
        json_data=body)
    return resp["id"]


# --- The scan itself (equivalent of the old main()) ------------------------

def start_scan(days=None, interval_days=None, min_request_interval=None, market="US"):
    """Reserve the scan lock before starting its background thread."""
    if not run_lock.acquire(blocking=False):
        log("Scan already in progress -- skipping this trigger.")
        return False
    threading.Thread(
        target=run_scan,
        kwargs={
            "days": days,
            "interval_days": interval_days,
            "min_request_interval": min_request_interval,
            "market": market,
            "lock_held": True,
        },
        daemon=True,
    ).start()
    return True


def run_scan(days=None, interval_days=None, min_request_interval=None, market="US", lock_held=False):
    """Runs one full scan pass: fetch followed artists, check due artists,
    record + playlist-sync new albums, prune the playlist. Safe to call
    from either the scheduler or a manual 'Run now' click -- run_lock
    ensures only one scan runs at a time."""
    _cfg = load_config()
    days = days or _cfg.get("days_lookback", DEFAULT_DAYS_LOOKBACK)
    interval_days = interval_days or _cfg.get("interval_days", CHECK_INTERVAL_DAYS)
    rate_limiter.min_interval_seconds = (
        min_request_interval if min_request_interval is not None
        else _cfg.get("min_request_interval", DEFAULT_MIN_REQUEST_INTERVAL_SECONDS)
    )

    if not lock_held and not run_lock.acquire(blocking=False):
        log("Scan already in progress -- skipping this trigger.")
        return {"status": "already_running"}

    clear_logs()
    _cancel_event.clear()

    try:
        _cfg = load_config()
        client_id = _cfg["spotify_client_id"]
        client_secret = _cfg["spotify_client_secret"]
        refresh_token = load_refresh_token()
        if not client_id or not client_secret or not refresh_token:
            log("Not connected to Spotify yet -- visit /login first.")
            return {"status": "not_connected"}

        token = get_access_token(client_id, client_secret, refresh_token)
        cutoff = datetime.now() - timedelta(days=days)
        state = load_state()
        if clear_expired_rate_limits(state):
            save_state(state)
        playlist_id = _cfg["spotify_playlist_id"] or None
        blocked_categories = []

        try:
            log("Fetching followed artists...")
            artists = get_followed_artists(token, state)
            log(f"Found {len(artists)} followed artists.")
        except LongRateLimitBlock as e:
            log(f"Skipping artist scan -- {e.category} rate-limited until "
                f"{datetime.fromtimestamp(e.retry_until, tz=timezone.utc)}.")
            blocked_categories.append(e.category)
            artists = []

        if artists:
            ip = state.get("in_progress")
            if ip is not None:
                processed_ids = set(ip["processed_ids"])
                due_artists = [a for a in artists if a["id"] in ip["due_ids"]]
                log(f"Resuming: {len(due_artists) - len(processed_ids)}/{len(due_artists)} remaining")
            else:
                due_artists = get_due_artists(artists, state, interval_days)
                processed_ids = set()
                state["in_progress"] = {"due_ids": [a["id"] for a in due_artists], "processed_ids": []}
                save_state(state)
                log(f"{len(due_artists)}/{len(artists)} artists due for a check (interval: {interval_days}d)")

            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                for i, artist in enumerate(due_artists, 1):
                    if artist["id"] in processed_ids:
                        continue
                    if _cancel_event.is_set():
                        log("  Scan cancelled by user.")
                        break
                    log(f"  [{i}/{len(due_artists)}] {artist['name']} - fetching albums...")
                    try:
                        albums = get_artist_albums(token, artist["id"], state, market)
                    except LongRateLimitBlock:
                        raise
                    except Exception as e:
                        log(f"    ERROR: {artist['name']}: {e}")
                        processed_ids.add(artist["id"])
                        state["in_progress"]["processed_ids"] = list(processed_ids)
                        save_state(state)
                        continue

                    log(f"    Retrieved {len(albums)} album(s)")
                    new_count = 0
                    for album in albums:
                        if album["album_type"] != "album":
                            continue
                        artist_ids = [a["id"] for a in album.get("artists", [])]
                        if artist["id"] not in artist_ids:
                            continue
                        release_date = parse_release_date(album["release_date"])
                        if release_date and release_date < cutoff:
                            continue

                        existing_entry = state["known_albums"].get(album["id"])
                        needs_playlist_add = existing_entry is None or not existing_entry.get("added_to_playlist", False)
                        record_album(state, artist, album, now_iso)
                        entry = state["known_albums"][album["id"]]
                        if needs_playlist_add and not is_effectively_excluded(entry) and playlist_id:
                            try:
                                track_uris = get_album_track_uris(token, album["id"], state)
                                add_tracks_to_playlist(token, playlist_id, track_uris, state)
                                entry["added_to_playlist"] = True
                                entry["track_uris"] = track_uris
                                log(f"      Added {len(track_uris)} track(s) from '{album['name']}'")
                            except LongRateLimitBlock:
                                raise
                            except Exception as e:
                                entry["added_to_playlist"] = False
                                log(f"      ERROR adding '{album['name']}': {e}")
                        new_count += 1

                    if new_count:
                        log(f"    Added {new_count} new album(s)")
                    else:
                        log("    No new albums added")
                    state["artists"][artist["id"]] = {"name": artist["name"], "last_checked": now_iso}
                    processed_ids.add(artist["id"])
                    state["in_progress"]["processed_ids"] = list(processed_ids)
                    save_state(state)
            except LongRateLimitBlock as e:
                log(f"Stopping scan -- {e.category} rate-limited until "
                    f"{datetime.fromtimestamp(e.retry_until, tz=timezone.utc)}. Progress saved.")
                blocked_categories.append(e.category)

        if not blocked_categories:
            state["in_progress"] = None
            save_state(state)

        try:
            prune_playlist(token, state, days, playlist_id)
        except LongRateLimitBlock as e:
            log(f"Skipping prune -- {e.category} rate-limited.")
            blocked_categories.append(e.category)

        log("Scan finished." + (f" Blocked categories: {blocked_categories}" if blocked_categories else ""))
        return {"status": "ok", "blocked_categories": blocked_categories}
    finally:
        _cancel_event.clear()
        run_lock.release()
