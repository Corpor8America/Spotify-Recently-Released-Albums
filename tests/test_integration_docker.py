"""
Docker integration tests for the containerized web app.

These tests exercise the full stack: real gunicorn, real Flask routes, real
HTTP requests, real file I/O on the /data volume, and a real scan against the
mock Spotify server container.  Run them with the app started via:

    docker compose -f docker-compose.dev.yml up -d --build

Set INTEGRATION_TEST=1 so `unittest discover` skips them during the regular
unit-test run:

    INTEGRATION_TEST=1 python -m unittest tests/test_integration_docker.py -v

Because the app container bind-mounts ./tests/seed as /data, tests can also
inspect/reset the app's persisted state directly from the host.  Stateful
tests call _reset_app_state() first, which waits for any background scan to
finish, restores the seed files, and waits for the mock server to go quiet
(so no stray async reorder/scan from a previous test is still in flight).
The mock Spotify server is exercised through its /_control/* endpoints
(snapshot, configure, reset_quota, reset_playlist) and its playlist items
GET endpoint so tests can assert on playlist contents and ordering.

Environment:
  INTEGRATION_TEST=1       required to run this module
  INTEGRATION_APP_URL      app base URL (default http://localhost:8080)
  INTEGRATION_MOCK_URL     mock Spotify base URL (default http://127.0.0.1:8791)
"""

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

APP_URL = os.environ.get("INTEGRATION_APP_URL", "http://localhost:8080")
MOCK_URL = os.environ.get("INTEGRATION_MOCK_URL", "http://127.0.0.1:8791")
POLL_INTERVAL = 1
SCAN_TIMEOUT_SECONDS = 120

THIS_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = THIS_DIR.parent
SEED_DIR = THIS_DIR / "seed"
STATE_FILE = SEED_DIR / "spotify-state.json"
TOKEN_FILE = SEED_DIR / "spotify-token.json"
CONFIG_FILE = SEED_DIR / "app-config.json"
COMPOSE_FILE = REPO_ROOT / "docker-compose.dev.yml"

_SEED_CONFIG = json.loads(CONFIG_FILE.read_text())
_SEED_STATE = {"artists": {}, "known_albums": {}, "in_progress": None, "rate_limits": {}}
_SEED_TOKEN = {"refresh_token": "mock-refresh-token"}

MOCK_PLAYLIST_ID = "mock-playlist-id"
TRACK_PREFIX = "spotify:track:album_"


@unittest.skipUnless(os.environ.get("INTEGRATION_TEST"), "Set INTEGRATION_TEST=1 to run Docker integration tests")
class DockerIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.session = requests.Session()

    # --- basic reachability ------------------------------------------------

    def _get_status(self):
        r = self.session.get(f"{APP_URL}/status", timeout=10)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_app_ready_and_status_shape(self):
        data = self._get_status()
        for key in ("connected", "scan_running", "in_progress",
                    "rate_limits", "known_albums_count", "logs"):
            self.assertIn(key, data)
        self.assertIsInstance(data["connected"], bool)
        self.assertIsInstance(data["scan_running"], bool)
        self.assertIsInstance(data["known_albums_count"], int)

    # --- dashboard ---------------------------------------------------------

    def test_dashboard_renders(self):
        r = self.session.get(f"{APP_URL}/", timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Recent Albums", r.text)

    # --- settings ----------------------------------------------------------

    def test_settings_page_renders(self):
        r = self.session.get(f"{APP_URL}/settings", timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Settings", r.text)

    def test_settings_post_saves(self):
        r = self.session.post(
            f"{APP_URL}/settings",
            data={
                "spotify_client_id": "mock-client-id",
                "spotify_client_secret": "mock-client-secret",
                "spotify_playlist_id": "mock-playlist-id",
                "interval_days": "3",
                "min_request_interval": "0",
                "days_lookback": "365",
                "cron_schedule": "0 6 * * *",
                "public_base_url": "http://localhost:8080",
            },
            allow_redirects=False,
            timeout=10,
        )
        self.assertEqual(r.status_code, 302)

    # --- OAuth -------------------------------------------------------------

    def test_login_redirects_to_mock_authorize(self):
        r = self.session.get(f"{APP_URL}/login", allow_redirects=False, timeout=10)
        self.assertEqual(r.status_code, 302)
        self.assertIn("authorize", r.headers["Location"])

    def test_callback_missing_code_returns_400(self):
        r = self.session.get(f"{APP_URL}/callback", timeout=10)
        self.assertEqual(r.status_code, 400)

    def test_oauth_callback_completes_connection(self):
        """Full OAuth dance: /login -> mock authorize -> /callback.  The
        mock returns a distinct refresh token for the authorization_code
        grant, so we can verify the app persisted it."""
        r = self.session.get(f"{APP_URL}/login", allow_redirects=False, timeout=10)
        self.assertEqual(r.status_code, 302)
        location = r.headers["Location"]
        self.assertIn("authorize", location)
        state = parse_qs(urlparse(location).query)["state"][0]

        r = self.session.get(
            f"{APP_URL}/callback",
            params={"state": state, "code": "mock-code"},
            allow_redirects=False,
            timeout=10,
        )
        self.assertEqual(r.status_code, 302)

        saved = json.loads(TOKEN_FILE.read_text())
        self.assertEqual(saved["refresh_token"], "mock-refresh-token-auth")
        self.assertTrue(self._get_status()["connected"])

    # --- scan actions ------------------------------------------------------

    def test_run_now_redirects(self):
        r = self.session.post(f"{APP_URL}/run", allow_redirects=False, timeout=10)
        self.assertEqual(r.status_code, 302)
        self._wait_for_scan()  # drain the background scan it starts

    def test_full_scan_discovers_albums(self):
        self.session.post(f"{APP_URL}/run", timeout=10)
        known = self._wait_for_scan()
        self.assertGreater(known, 0)

    def test_cancel_scan_redirects(self):
        r = self.session.post(f"{APP_URL}/cancel", allow_redirects=False, timeout=10)
        self.assertEqual(r.status_code, 302)

    def test_reorder_playlist_redirects(self):
        r = self.session.post(f"{APP_URL}/reorder", allow_redirects=False, timeout=10)
        self.assertEqual(r.status_code, 302)

    # --- high-value flows (each resets state for determinism) --------------

    def test_scan_adds_album_tracks_to_playlist(self):
        """A full scan must actually sync the discovered albums' tracks into
        the mock playlist."""
        self._reset_app_state()
        self._mock_reset()
        self._run_scan_and_wait()
        items = self._playlist_items()
        self.assertEqual(len(items), 20 * 10)  # 20 artists, 1 album x 10 tracks
        self.assertIn("spotify:track:album_artist0001_000_00", items)

    def test_second_scan_does_not_duplicate_tracks(self):
        """Re-running a scan when no artist is due must not re-add tracks."""
        self._reset_app_state()
        self._mock_reset()
        self._run_scan_and_wait()
        first = self._playlist_items()
        self.assertGreater(len(first), 0)
        self._run_scan_and_wait()
        second = self._playlist_items()
        self.assertEqual(len(second), len(first))
        self.assertEqual(set(second), set(first))

    def test_reorder_sorts_playlist_by_release_date(self):
        """Reorder must rewrite the playlist sorted oldest-album-first."""
        self._reset_app_state()
        self._mock_reset()
        self._mock_configure(artist_release_dates={
            "artist0001": "2026-07-01",
            "artist0002": "2026-05-01",
        })
        self._run_scan_and_wait()
        before = self._playlist_items()
        self.assertGreater(len(before), 0)

        r = self.session.post(f"{APP_URL}/reorder", allow_redirects=False, timeout=10)
        self.assertEqual(r.status_code, 302)
        self._wait_for_log("Playlist reorder complete.")

        after = self._playlist_items()
        self.assertEqual(len(after), len(before))
        self.assertEqual(set(after), set(before))
        self.assertEqual(after[0], "spotify:track:album_artist0002_000_00")
        self.assertTrue(all(u.startswith("spotify:track:album_artist0002_") for u in after[:10]))
        self.assertLess(
            after.index("spotify:track:album_artist0002_000_00"),
            after.index("spotify:track:album_artist0001_000_00"),
        )

    def test_excluded_album_is_pruned_on_rescan(self):
        """Marking a playlisted album as excluded then rescanning must remove
        its tracks from the playlist and clear its added_to_playlist flag."""
        self._reset_app_state()
        self._mock_reset()
        self._run_scan_and_wait()
        r = self.session.post(
            f"{APP_URL}/albums/album_artist0001_000/override",
            data={"value": "true"},
            allow_redirects=False,
            timeout=10,
        )
        self.assertEqual(r.status_code, 302)
        self._run_scan_and_wait()

        items = self._playlist_items()
        self.assertNotIn("spotify:track:album_artist0001_000_00", items)
        album = self._read_state()["known_albums"]["album_artist0001_000"]
        self.assertFalse(album["added_to_playlist"])
        self.assertIs(album["manual_override"], True)

    def test_reincluded_album_is_re_added(self):
        """Re-including a pruned album and rescanning an artist that is due
        again must re-add its tracks to the playlist."""
        self._reset_app_state()
        self._mock_reset()
        self._run_scan_and_wait()
        self.session.post(
            f"{APP_URL}/albums/album_artist0001_000/override",
            data={"value": "true"},
            allow_redirects=False,
            timeout=10,
        )
        self._run_scan_and_wait()
        self.assertNotIn("spotify:track:album_artist0001_000_00", self._playlist_items())

        # Simulate time passing: drop last_checked so every artist is due again.
        state = self._read_state()
        state["artists"] = {}
        self._write_state(state)

        self.session.post(
            f"{APP_URL}/albums/album_artist0001_000/override",
            data={"value": "false"},
            allow_redirects=False,
            timeout=10,
        )
        self._run_scan_and_wait()
        self.assertIn("spotify:track:album_artist0001_000_00", self._playlist_items())

    def test_auto_excluded_album_not_playlisted(self):
        """Albums whose names end in parentheses are auto-excluded: recorded
        but never synced to the playlist."""
        self._reset_app_state()
        self._mock_reset()
        self._mock_configure(paren_album_artists=["artist0003"])
        self._run_scan_and_wait()

        items = self._playlist_items()
        self.assertFalse([u for u in items if u.startswith("spotify:track:album_artist0003_")])
        album = self._read_state()["known_albums"]["album_artist0003_000"]
        self.assertIs(album["auto_excluded"], True)
        self.assertIs(album["added_to_playlist"], False)

    def test_rate_limit_lockout_stops_scan(self):
        """A long 429 (simulated dev-mode daily quota) must stop the scan and
        record the blocked category in /status rate_limits."""
        self._reset_app_state()
        self._mock_reset()
        self._mock_configure(daily_quota=3)
        self._run_scan_and_wait()

        data = self._get_status()
        self.assertNotEqual(data["rate_limits"], {})
        self.assertEqual(data["known_albums_count"], 1)  # stopped after the 4th request

        # Clean up so later scans are unaffected: clear quota + persisted limits.
        self._mock_configure(daily_quota=None)
        self.session.post(f"{MOCK_URL}/_control/reset_quota", timeout=10)
        state = self._read_state()
        state["rate_limits"] = {}
        self._write_state(state)

    def test_z_data_persists_across_restart(self):
        """The /data volume must survive an app container restart: albums and
        the saved refresh token are still there."""
        self._reset_app_state()
        self._mock_reset()
        self._run_scan_and_wait()
        count = self._get_status()["known_albums_count"]
        self.assertGreater(count, 0)

        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "restart", "spotify-recent-albums"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        self._wait_for_app_ready()
        data = self._get_status()
        self.assertEqual(data["known_albums_count"], count)
        self.assertTrue(data["connected"])

    # --- album overrides ---------------------------------------------------

    def test_override_unknown_album_404(self):
        r = self.session.post(
            f"{APP_URL}/albums/does-not-exist/override",
            data={"value": "true"},
            timeout=10,
        )
        self.assertEqual(r.status_code, 404)

    def test_override_known_album(self):
        self.session.post(f"{APP_URL}/run", timeout=10)
        self._wait_for_scan()
        r = self.session.post(
            f"{APP_URL}/albums/album_artist0001_000/override",
            data={"value": "true"},
            allow_redirects=False,
            timeout=10,
        )
        self.assertEqual(r.status_code, 302)

    # --- mock Spotify helpers ---------------------------------------------

    def _mock_configure(self, **kwargs):
        r = self.session.post(f"{MOCK_URL}/_control/configure", json=kwargs, timeout=10)
        self.assertEqual(r.status_code, 200)

    def _mock_snapshot(self):
        r = self.session.get(f"{MOCK_URL}/_control/snapshot", timeout=10)
        r.raise_for_status()
        return r.json()

    def _mock_reset(self):
        """Put the mock back to its default (unlimited, empty) state."""
        self._mock_configure(
            daily_quota=None,
            rate_limit_per_minute=None,
            short_429_every=None,
            per_category_quota={},
            artist_release_dates={},
            paren_album_artists=[],
        )
        self.session.post(f"{MOCK_URL}/_control/reset_quota", timeout=10)
        self.session.post(f"{MOCK_URL}/_control/reset_playlist", timeout=10)

    def _playlist_items(self):
        r = self.session.get(
            f"{MOCK_URL}/v1/playlists/{MOCK_PLAYLIST_ID}/items",
            headers={"Authorization": "Bearer mock-access-token"},
            timeout=10,
        )
        r.raise_for_status()
        return [item["track"]["uri"] for item in r.json()["items"]]

    # --- app state helpers ------------------------------------------------

    def _read_state(self):
        return json.loads(STATE_FILE.read_text())

    def _write_state(self, state):
        STATE_FILE.write_text(json.dumps(state))

    def _reset_app_state(self):
        """Restore the app's persisted state to the seed files.  Waits for
        background work (scans, reorders) to settle first so a stray async
        task from a previous test can't clobber the reset."""
        self._wait_for_scan_idle()
        for name, data in (("spotify-state.json", _SEED_STATE),
                           ("spotify-token.json", _SEED_TOKEN),
                           ("app-config.json", _SEED_CONFIG)):
            (SEED_DIR / name).write_text(json.dumps(data))
        self._wait_for_mock_idle()
        self._mock_reset()
        self._wait_for_mock_idle()
        self._get_status()

    def _run_scan_and_wait(self):
        r = self.session.post(f"{APP_URL}/run", allow_redirects=False, timeout=10)
        self.assertEqual(r.status_code, 302)
        return self._wait_for_scan()

    # --- polling helpers ---------------------------------------------------

    def _wait_for_scan(self):
        """Poll /status until the background scan finishes; return album count.
        Tolerates transient 5xx from the status route: on the Docker Desktop
        bind mount the app's atomic state replace can briefly make the state
        file invisible to a concurrent reader."""
        deadline = time.time() + SCAN_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                data = self._get_status()
            except (AssertionError, requests.RequestException):
                time.sleep(POLL_INTERVAL)
                continue
            if not data["scan_running"]:
                return data["known_albums_count"]
            time.sleep(POLL_INTERVAL)
        self.fail("Background scan did not finish within timeout")

    def _wait_for_scan_idle(self, timeout=SCAN_TIMEOUT_SECONDS):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = self._get_status()
            except (AssertionError, requests.RequestException):
                time.sleep(POLL_INTERVAL)
                continue
            if not data["scan_running"]:
                return
            time.sleep(POLL_INTERVAL)
        self.fail("Background scan did not settle before reset")

    def _wait_for_log(self, text, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                logs = "\n".join(self._get_status()["logs"])
            except (AssertionError, requests.RequestException):
                time.sleep(POLL_INTERVAL)
                continue
            if text in logs:
                return
            time.sleep(POLL_INTERVAL)
        self.fail(f"Did not see log line {text!r} within timeout")

    def _wait_for_app_ready(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.session.get(f"{APP_URL}/status", timeout=5)
                if r.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(POLL_INTERVAL)
        self.fail("App did not become ready within timeout")

    def _wait_for_mock_idle(self, quiet_cycles=2, quiet_interval=0.5):
        """Wait until the mock stops receiving requests for a moment, so no
        async app thread (reorder, scan) is still in flight."""
        last = self._mock_snapshot()["total_requests"]
        quiet = 0
        while quiet < quiet_cycles:
            time.sleep(quiet_interval)
            now = self._mock_snapshot()["total_requests"]
            if now == last:
                quiet += 1
            else:
                quiet = 0
                last = now


if __name__ == "__main__":
    unittest.main()
