"""
Mock Spotify API server.

Simulates just enough of the real Spotify Web API surface for
spotify-recent-albums.py to run against: token refresh, followed artists
(paginated), artist albums (paginated), album tracks (paginated), and
playlist track add/remove (plus a GET /playlists/{id}/items so tests can
inspect playlist contents and order). It also simulates the failure modes
that matter for this project:

  - A per-minute rate limit (like Spotify's real short-lived 429s).
  - A "daily quota" ceiling: once a configurable number of requests have been
    made since the quota was last reset, every further request returns 429
    with a long Retry-After (default 24h) -- this is what reproduces the
    dev-mode lockout described in the docs.
  - A few /_control/* endpoints so the test harness can configure the
    simulated catalog, inspect request counts, and reset the "day" boundary
    between simulated runs without needing to literally wait 24 real hours.

Playlist track endpoints use /playlists/{id}/items (not /tracks), matching
Spotify's February 2026 Dev Mode migration, which removed the old
/playlists/{id}/tracks endpoints for Development Mode apps as of March 9,
2026. The DELETE body param is "items" (not "tracks") to match as well.

The /token endpoint distinguishes grant_type so tests can verify the OAuth
authorization_code flow returns a fresh refresh_token while the refresh
flow just returns an access token.

Run standalone for manual poking:
    python mock_spotify_server.py --port 8791

Or import `MockSpotifyServer` and use it programmatically (this is what the
test harness does):

    server = MockSpotifyServer(num_artists=82, daily_quota=120)
    server.start()
    ...
    server.stop()
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def _make_artist(i):
    return {
        "id": f"artist{i:04d}",
        "name": f"Test Artist {i}",
    }


def _make_album(artist_id, artist_idx, album_idx, release_date, total_tracks=10, paren=False):
    album_id = f"album_{artist_id}_{album_idx:03d}"
    name = f"Album {album_idx} by Artist {artist_idx}"
    if paren:
        name += " (Deluxe)"
    return {
        "id": album_id,
        "name": name,
        "album_type": "album",
        "release_date": release_date,
        "total_tracks": total_tracks,
        "artists": [{"id": artist_id}],
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
    }


class _State:
    """Shared, thread-safe mutable state for the mock server."""

    def __init__(self, num_artists=82, albums_per_artist=1, daily_quota=None,
                 rate_limit_per_minute=None, short_429_every=None,
                 recent_release_date="2026-07-01", per_category_quota=None):
        self.lock = threading.Lock()
        self.num_artists = num_artists
        self.albums_per_artist = albums_per_artist
        self.recent_release_date = recent_release_date

        # Quota / rate-limit knobs -- all optional, all None means "off".
        self.daily_quota = daily_quota
        self.rate_limit_per_minute = rate_limit_per_minute
        self.short_429_every = short_429_every  # e.g. 7 -> every 7th request gets a short 429

        self.per_category_quota = per_category_quota or {}  # e.g. {"GET /v1/artists": 40, "DELETE /v1/playlists": 200}
        self.category_request_counts = {}  # category -> count since reset

        self.request_count_since_reset = 0
        self.request_timestamps = []  # for the per-minute limiter
        self.total_requests = 0
        self.request_log = []  # list of (method, path) for inspection

        self.artists = [_make_artist(i) for i in range(num_artists)]
        # track uris per album, generated lazily/deterministically
        self.playlist_tracks = []  # list of track uris currently "in" the mock playlist
        self.created_playlists = []  # playlists created via POST /v1/users/{id}/playlists

        # Per-artist overrides for the generated catalog.
        self.artist_release_dates = {}  # artist_id -> release date string
        self.paren_album_artists = []   # artist_ids whose album names get " (Deluxe)"

    def reset_quota(self):
        with self.lock:
            self.request_count_since_reset = 0
            self.category_request_counts = {}

    def snapshot(self):
        with self.lock:
            return {
                "total_requests": self.total_requests,
                "request_count_since_reset": self.request_count_since_reset,
                "playlist_track_count": len(self.playlist_tracks),
                "created_playlist_ids": [p["id"] for p in self.created_playlists],
            }


class _Handler(BaseHTTPRequestHandler):
    state: _State = None  # set by MockSpotifyServer

    def log_message(self, format, *args):
        pass  # keep test output quiet; harness prints its own summaries

    # ---- helpers -------------------------------------------------------

    def _send_json(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _match_category(self, method, path):
        """Map a request (method, path) to a per_category_quota key by
        checking if the path starts with any registered prefix."""
        for prefix in self.state.per_category_quota:
            parts = prefix.split(" ", 1)
            if len(parts) == 2 and parts[0] == method and path.startswith(parts[1]):
                return prefix
        return None

    def _check_quota_and_rate_limit(self, method, path):
        """Returns a (status, headers, body) tuple if the request should be
        rejected with a 429, or None if it should proceed normally."""
        s = self.state
        with s.lock:
            s.total_requests += 1
            s.request_count_since_reset += 1
            now = time.time()
            s.request_timestamps = [t for t in s.request_timestamps if now - t < 60]

            # Daily quota check (long 429, simulates dev-mode lockout)
            if s.daily_quota is not None and s.request_count_since_reset > s.daily_quota:
                return (429, {"Retry-After": "86400"},
                        {"error": {"status": 429, "message": "API rate limit exceeded"}})

            # Per-category quota check
            if s.per_category_quota:
                category_prefix = self._match_category(method, path)
                if category_prefix:
                    count = s.category_request_counts.get(category_prefix, 0) + 1
                    s.category_request_counts[category_prefix] = count
                    limit = s.per_category_quota.get(category_prefix)
                    if limit is not None and count > limit:
                        return (429, {"Retry-After": "86400"},
                                {"error": {"status": 429, "message": f"Category quota exceeded: {category_prefix}"}})

            # Per-minute rate limit check (short 429)
            if s.rate_limit_per_minute is not None and len(s.request_timestamps) >= s.rate_limit_per_minute:
                return (429, {"Retry-After": "5"},
                        {"error": {"status": 429, "message": "Too many requests"}})

            # Periodic short 429 injection, for exercising the retry path
            if s.short_429_every and s.total_requests % s.short_429_every == 0:
                return (429, {"Retry-After": "1"},
                        {"error": {"status": 429, "message": "Too many requests"}})

            s.request_timestamps.append(now)
        return None

    def _auth_ok(self):
        return self.headers.get("Authorization", "").startswith("Bearer ")

    # ---- routing ---------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        with self.state.lock:
            self.state.request_log.append(("GET", path))

        if path == "/_control/snapshot":
            self._send_json(200, self.state.snapshot())
            return

        if not self._auth_ok() and not path.startswith("/_control"):
            self._send_json(401, {"error": {"status": 401, "message": "Invalid access token"}})
            return

        rejection = self._check_quota_and_rate_limit("GET", path)
        if rejection:
            status, headers, body = rejection
            self._send_json(status, body, headers)
            return

        if path == "/v1/me":
            self._handle_me()
        elif path == "/v1/me/following":
            self._handle_following(qs)
        elif path.startswith("/v1/artists/") and path.endswith("/albums"):
            artist_id = path.split("/")[3]
            self._handle_artist_albums(artist_id, qs)
        elif path.startswith("/v1/albums/") and path.endswith("/tracks"):
            album_id = path.split("/")[3]
            self._handle_album_tracks(album_id, qs)
        elif path.startswith("/v1/playlists/") and path.endswith("/items"):
            self._handle_playlist_items()
        else:
            self._send_json(404, {"error": {"status": 404, "message": "not found"}})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        with self.state.lock:
            self.state.request_log.append(("POST", path))

        if path == "/_control/reset_quota":
            self.state.reset_quota()
            self._send_json(200, {"ok": True})
            return

        if path == "/_control/reset_playlist":
            with self.state.lock:
                self.state.playlist_tracks = []
            self._send_json(200, {"ok": True})
            return

        if path == "/_control/configure":
            cfg = json.loads(raw_body or b"{}")
            with self.state.lock:
                for key in ("daily_quota", "rate_limit_per_minute", "short_429_every",
                            "per_category_quota", "recent_release_date",
                            "albums_per_artist", "artist_release_dates",
                            "paren_album_artists"):
                    if key in cfg:
                        setattr(self.state, key, cfg[key])
            self._send_json(200, {"ok": True})
            return

        if path == "/token" or path.endswith("/api/token"):
            form = {k: v[0] for k, v in parse_qs(raw_body.decode("utf-8")).items()}
            grant_type = form.get("grant_type")
            refresh_token = (
                "mock-refresh-token-auth"
                if grant_type == "authorization_code"
                else "mock-refresh-token"
            )
            self._send_json(200, {
                "access_token": "mock-access-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": refresh_token,
            })
            return

        if not self._auth_ok():
            self._send_json(401, {"error": {"status": 401, "message": "Invalid access token"}})
            return

        rejection = self._check_quota_and_rate_limit("POST", path)
        if rejection:
            status, headers, body = rejection
            self._send_json(status, body, headers)
            return

        # NOTE: /items, not /tracks -- matches Spotify's Feb 2026 migration.
        if path.startswith("/v1/users/") and path.endswith("/playlists"):
            body = json.loads(raw_body or b"{}")
            playlist_id = f"playlist{len(self.state.created_playlists) + 1:04d}"
            with self.state.lock:
                self.state.created_playlists.append({
                    "id": playlist_id,
                    "name": body.get("name"),
                })
            self._send_json(201, {
                "id": playlist_id,
                "name": body.get("name"),
                "public": body.get("public", False),
                "external_urls": {"spotify": f"https://open.spotify.com/playlist/{playlist_id}"},
            })
            return

        if path.startswith("/v1/playlists/") and path.endswith("/items"):
            body = json.loads(raw_body or b"{}")
            uris = body.get("uris", [])
            with self.state.lock:
                self.state.playlist_tracks.extend(uris)
            self._send_json(201, {"snapshot_id": "mock-snapshot"})
            return

        self._send_json(404, {"error": {"status": 404, "message": "not found"}})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        if not self._auth_ok():
            self._send_json(401, {"error": {"status": 401, "message": "Invalid access token"}})
            return

        rejection = self._check_quota_and_rate_limit("DELETE", path)
        if rejection:
            status, headers, body = rejection
            self._send_json(status, body, headers)
            return

        # NOTE: /items, not /tracks, and body param is "items" not "tracks"
        # -- matches Spotify's Feb 2026 migration.
        if path.startswith("/v1/playlists/") and path.endswith("/items"):
            body = json.loads(raw_body or b"{}")
            remove_uris = {t["uri"] for t in body.get("items", [])}
            with self.state.lock:
                self.state.playlist_tracks = [t for t in self.state.playlist_tracks if t not in remove_uris]
            self._send_json(200, {"snapshot_id": "mock-snapshot"})
            return

        self._send_json(404, {"error": {"status": 404, "message": "not found"}})

    # ---- endpoint implementations -----------------------------------

    def _handle_me(self):
        self._send_json(200, {
            "id": "mock-user",
            "display_name": "Mock User",
            "uri": "spotify:user:mock-user",
        })

    def _handle_following(self, qs):
        s = self.state
        limit = int(qs.get("limit", 50))
        after = qs.get("after")
        start = 0
        if after is not None:
            for idx, a in enumerate(s.artists):
                if a["id"] == after:
                    start = idx + 1
                    break
        page = s.artists[start:start + limit]
        next_after = page[-1]["id"] if page and (start + limit) < len(s.artists) else None
        self._send_json(200, {
            "artists": {
                "items": page,
                "cursors": {"after": next_after},
                "total": len(s.artists),
            }
        })

    def _handle_artist_albums(self, artist_id, qs):
        s = self.state
        limit = int(qs.get("limit", 10))
        offset = int(qs.get("offset", 0))
        try:
            artist_idx = int(artist_id.replace("artist", ""))
        except ValueError:
            artist_idx = 0
        release_date = s.artist_release_dates.get(artist_id, s.recent_release_date)
        paren = artist_id in s.paren_album_artists
        all_albums = [
            _make_album(artist_id, artist_idx, n, release_date, paren=paren)
            for n in range(s.albums_per_artist)
        ]
        page = all_albums[offset:offset + limit]
        self._send_json(200, {"items": page, "total": len(all_albums)})

    def _handle_album_tracks(self, album_id, qs):
        limit = int(qs.get("limit", 50))
        offset = int(qs.get("offset", 0))
        total_tracks = 10
        all_tracks = [
            {"uri": f"spotify:track:{album_id}_{n:02d}", "name": f"Track {n}"}
            for n in range(total_tracks)
        ]
        page = all_tracks[offset:offset + limit]
        self._send_json(200, {"items": page, "total": len(all_tracks)})

    def _handle_playlist_items(self):
        with self.state.lock:
            uris = list(self.state.playlist_tracks)
        self._send_json(200, {
            "items": [{"track": {"uri": u}} for u in uris],
            "total": len(uris),
        })


class MockSpotifyServer:
    def __init__(self, num_artists=82, albums_per_artist=1, daily_quota=None,
                 rate_limit_per_minute=None, short_429_every=None,
                 recent_release_date="2026-07-01", per_category_quota=None,
                 host="127.0.0.1", port=0):
        self.state = _State(
            num_artists=num_artists,
            albums_per_artist=albums_per_artist,
            daily_quota=daily_quota,
            rate_limit_per_minute=rate_limit_per_minute,
            short_429_every=short_429_every,
            recent_release_date=recent_release_date,
            per_category_quota=per_category_quota,
        )
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.httpd.server_address
        return f"http://127.0.0.1:{port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def reset_quota(self):
        self.state.reset_quota()

    def configure(self, **kwargs):
        with self.state.lock:
            for k, v in kwargs.items():
                setattr(self.state, k, v)

    def snapshot(self):
        return self.state.snapshot()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the mock Spotify server standalone")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--num-artists", type=int, default=82)
    parser.add_argument("--daily-quota", type=int, default=None)
    args = parser.parse_args()

    server = MockSpotifyServer(num_artists=args.num_artists, daily_quota=args.daily_quota,
                               host=args.host, port=args.port)
    server.start()
    print(f"Mock Spotify server running at {server.base_url}")
    print("Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()