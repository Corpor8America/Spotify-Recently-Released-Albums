import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

TEST_DIR = Path(tempfile.mkdtemp())
os.environ["DATA_DIR"] = str(TEST_DIR)
os.environ["RUN_SCHEDULER"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spotify_core as core
import mock_spotify_server


class IsAutoExcludedTests(unittest.TestCase):
    def test_plain_name(self):
        self.assertFalse(core.is_auto_excluded("Album Name"))

    def test_live(self):
        self.assertTrue(core.is_auto_excluded("Album Name (Live)"))

    def test_remastered(self):
        self.assertTrue(core.is_auto_excluded("Album Name (Remastered)"))

    def test_deluxe(self):
        self.assertTrue(core.is_auto_excluded("Album Name (Deluxe Edition)"))

    def test_trailing_whitespace(self):
        self.assertTrue(core.is_auto_excluded("Album Name (Live) "))

    def test_mid_string_parenthetical_not_trailing(self):
        self.assertFalse(core.is_auto_excluded("Song (feat. Artist) - Single"))


class IsEffectivelyExcludedTests(unittest.TestCase):
    def test_auto_excluded_no_override(self):
        self.assertTrue(core.is_effectively_excluded({"auto_excluded": True}))

    def test_auto_excluded_with_override_false(self):
        self.assertFalse(core.is_effectively_excluded({"auto_excluded": True, "manual_override": False}))

    def test_not_auto_excluded_with_override_true(self):
        self.assertTrue(core.is_effectively_excluded({"auto_excluded": False, "manual_override": True}))

    def test_not_auto_excluded_no_override(self):
        self.assertFalse(core.is_effectively_excluded({"auto_excluded": False}))

    def test_empty_dict(self):
        self.assertFalse(core.is_effectively_excluded({}))


class ParseReleaseDateTests(unittest.TestCase):
    def test_full_date(self):
        self.assertEqual(core.parse_release_date("2026-07-29"), datetime(2026, 7, 29))

    def test_year_month(self):
        self.assertEqual(core.parse_release_date("2026-07"), datetime(2026, 7, 1))

    def test_year_only(self):
        self.assertEqual(core.parse_release_date("2026"), datetime(2026, 1, 1))

    def test_invalid_returns_none(self):
        self.assertIsNone(core.parse_release_date(""))


class GetReportAlbumsTests(unittest.TestCase):
    def test_filters_excluded_and_sorts(self):
        state = {
            "known_albums": {
                "a1": {"name": "Recent", "artist": "X", "release_date": "2026-07-01",
                       "auto_excluded": False, "added_to_playlist": False},
                "a2": {"name": "Old", "artist": "Y", "release_date": "2020-01-01",
                       "auto_excluded": False, "added_to_playlist": False},
                "a3": {"name": "Excluded", "artist": "Z", "release_date": "2026-06-01",
                       "auto_excluded": True, "added_to_playlist": False},
            }
        }
        result = core.get_report_albums(state, 365)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Recent")

    def test_manual_override_included(self):
        state = {
            "known_albums": {
                "a1": {"name": "Live (Live)", "artist": "X", "release_date": "2026-07-01",
                       "auto_excluded": True, "manual_override": False, "added_to_playlist": False},
            }
        }
        result = core.get_report_albums(state, 365)
        self.assertEqual(len(result), 1)


class GetExcludedAlbumsTests(unittest.TestCase):
    def test_returns_only_excluded(self):
        state = {
            "known_albums": {
                "a1": {"name": "Good", "artist": "X", "release_date": "2026-07-01",
                       "auto_excluded": False},
                "a2": {"name": "Bad (Live)", "artist": "Y", "release_date": "2026-06-01",
                       "auto_excluded": True},
            }
        }
        result = core.get_excluded_albums(state)
        self.assertEqual(len(result), 1)
        self.assertIn("id", result[0])


class RecordAlbumTests(unittest.TestCase):
    def test_preserves_manual_override(self):
        state = {"known_albums": {"a1": {"manual_override": True, "auto_excluded": False}}}
        artist = {"name": "Artist", "id": "art1"}
        album = {"id": "a1", "name": "Live (Live)", "album_type": "album",
                 "release_date": "2026-01-01", "external_urls": {"spotify": "http://x"},
                 "total_tracks": 5}
        core.record_album(state, artist, album, "2026-07-01T00:00:00+00:00")
        self.assertTrue(state["known_albums"]["a1"]["manual_override"])
        self.assertTrue(state["known_albums"]["a1"]["auto_excluded"])

    def test_new_album_defaults(self):
        state = {"known_albums": {}}
        artist = {"name": "Artist", "id": "art1"}
        album = {"id": "a2", "name": "Studio Album", "album_type": "album",
                 "release_date": "2026-01-01", "external_urls": {"spotify": "http://x"},
                 "total_tracks": 10}
        core.record_album(state, artist, album, "2026-07-01T00:00:00+00:00")
        self.assertFalse(state["known_albums"]["a2"]["auto_excluded"])
        self.assertIsNone(state["known_albums"]["a2"]["manual_override"])


class ReorderPlaylistTests(unittest.TestCase):
    def test_reorder_clears_current_playlist_and_rebuilds_from_state(self):
        state = {
            "known_albums": {
                "new": {
                    "release_date": "2026-07-01", "added_to_playlist": True,
                    "auto_excluded": False, "track_uris": ["new-1", "new-2"],
                },
                "old": {
                    "release_date": "2026-05-01", "added_to_playlist": True,
                    "auto_excluded": False, "track_uris": ["old-1", "old-2"],
                },
            }
        }
        # These stale items must be cleared; only state-backed tracks return.
        current = ["new-1", "old-1", "external", "new-2", "old-2", "new-1"]

        with patch.object(core, "get_playlist_track_uris", return_value=current), \
             patch.object(core, "remove_tracks_from_playlist") as remove, \
             patch.object(core, "add_tracks_to_playlist") as add:
            core.reorder_playlist("token", state, "playlist")

        remove.assert_called_once_with("token", "playlist", current, state)
        add.assert_called_once_with(
            "token", "playlist",
            ["old-1", "old-2", "new-1", "new-2"], state,
        )


class GetDueArtistsTests(unittest.TestCase):
    def test_all_due_when_no_history(self):
        artists = [{"id": "a1", "name": "A"}, {"id": "a2", "name": "B"}]
        state = {"artists": {}}
        due = core.get_due_artists(artists, state, 7)
        self.assertEqual(len(due), 2)

    def test_filters_recently_checked(self):
        artists = [{"id": "a1", "name": "A"}, {"id": "a2", "name": "B"}]
        state = {
            "artists": {
                "a1": {"name": "A", "last_checked": datetime.now(timezone.utc).isoformat()},
            }
        }
        due = core.get_due_artists(artists, state, 7)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["id"], "a2")

    def test_selects_oldest_batch_when_none_are_overdue(self):
        now = datetime.now(timezone.utc)
        artists = [
            {"id": "a1", "name": "A"},
            {"id": "a2", "name": "B"},
            {"id": "a3", "name": "C"},
            {"id": "a4", "name": "D"},
        ]
        state = {
            "artists": {
                "a1": {"name": "A", "last_checked": (now - timedelta(days=1)).isoformat()},
                "a2": {"name": "B", "last_checked": (now - timedelta(days=2)).isoformat()},
                "a3": {"name": "C", "last_checked": (now - timedelta(hours=6)).isoformat()},
                "a4": {"name": "D", "last_checked": (now - timedelta(hours=12)).isoformat()},
            }
        }
        due = core.get_due_artists(artists, state, 3)
        self.assertEqual([artist["id"] for artist in due], ["a2"])


class EndpointCategoryTests(unittest.TestCase):
    def test_normalizes_ids(self):
        url = f"{core.SPOTIFY_API_BASE}/artists/abc123def456ghi/albums"
        cat = core.endpoint_category("GET", url)
        self.assertIn("{id}", cat)
        self.assertNotIn("abc123def456ghi", cat)

    def test_strips_api_base(self):
        url = f"{core.SPOTIFY_API_BASE}/me/following"
        cat = core.endpoint_category("GET", url)
        self.assertEqual(cat, "GET /me/following")


class RateLimiterTests(unittest.TestCase):
    def test_no_delay_when_under_limit(self):
        limiter = core.RateLimiter(120, min_interval_seconds=0)
        start = time.monotonic()
        limiter.wait_if_needed()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1)

    def test_respects_min_interval(self):
        limiter = core.RateLimiter(120, min_interval_seconds=2)
        limiter.last_request_time = time.time() - 0.5
        start = time.monotonic()
        limiter.wait_if_needed()
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.0)


class StateFileTests(unittest.TestCase):
    def setUp(self):
        if core.STATE_FILE.exists():
            core.STATE_FILE.unlink()

    def tearDown(self):
        if core.STATE_FILE.exists():
            core.STATE_FILE.unlink()

    def test_load_returns_defaults_when_missing(self):
        state = core.load_state()
        self.assertEqual(state["artists"], {})
        self.assertEqual(state["known_albums"], {})
        self.assertIsNone(state["in_progress"])
        self.assertEqual(state["rate_limits"], {})

    def test_save_and_load_roundtrip(self):
        original = {
            "artists": {"a1": {"name": "Test", "last_checked": "2026-01-01T00:00:00"}},
            "known_albums": {},
            "in_progress": None,
            "rate_limits": {},
        }
        core.save_state(original)
        loaded = core.load_state()
        self.assertEqual(loaded["artists"]["a1"]["name"], "Test")

    def test_clear_expired_rate_limits_removes_only_past_entries(self):
        state = {
            "artists": {},
            "known_albums": {},
            "in_progress": None,
            "rate_limits": {"expired": 99, "future": 101},
        }
        changed = core.clear_expired_rate_limits(state, now=100)
        self.assertTrue(changed)
        self.assertEqual(state["rate_limits"], {"future": 101})

    def test_clear_expired_rate_limits_reports_no_change(self):
        state = {"rate_limits": {"future": 101}}
        changed = core.clear_expired_rate_limits(state, now=100)
        self.assertFalse(changed)
        self.assertEqual(state["rate_limits"], {"future": 101})


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        if core.TOKEN_FILE.exists():
            core.TOKEN_FILE.unlink()

    def tearDown(self):
        if core.TOKEN_FILE.exists():
            core.TOKEN_FILE.unlink()

    def test_save_and_load(self):
        core.save_refresh_token("test_token_123")
        loaded = core.load_refresh_token()
        self.assertEqual(loaded, "test_token_123")

    def test_load_returns_none_when_missing(self):
        self.assertIsNone(core.load_refresh_token())

    def test_is_connected_false_when_no_token(self):
        self.assertFalse(core.is_connected())

    def test_is_connected_true_with_token(self):
        core.save_refresh_token("token")
        self.assertTrue(core.is_connected())


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self.config_file = core.CONFIG_FILE
        if self.config_file.exists():
            self.config_file.unlink()

    def tearDown(self):
        if self.config_file.exists():
            self.config_file.unlink()

    def test_load_returns_defaults_when_missing(self):
        config = core.load_config()
        self.assertEqual(config["interval_days"], 3)
        self.assertEqual(config["days_lookback"], 365)
        self.assertIn("flask_secret_key", config)

    def test_save_and_load(self):
        config = {"spotify_client_id": "test", "spotify_client_secret": "secret",
                  "spotify_playlist_id": "", "interval_days": 14,
                  "min_request_interval": 10, "days_lookback": 180,
                  "cron_schedule": "0 6 * * *", "public_base_url": "http://test:8080",
                  "flask_secret_key": "key"}
        core.save_config(config)
        loaded = core.load_config()
        self.assertEqual(loaded["interval_days"], 14)

    def test_is_configured_false_when_missing(self):
        if core.CONFIG_FILE.exists():
            core.CONFIG_FILE.unlink()
        self.assertFalse(core.is_configured())

    def test_is_configured_true_when_set(self):
        with open(core.CONFIG_FILE, "w") as f:
            json.dump({"spotify_client_id": "x", "spotify_client_secret": "y",
                       "flask_secret_key": "test-key"}, f)
        self.assertTrue(core.is_configured())


class LogTests(unittest.TestCase):
    def setUp(self):
        core.clear_logs()

    def test_log_appends_line(self):
        core.log("test message")
        logs = core.get_recent_logs()
        self.assertTrue(any("test message" in line for line in logs))

    def test_clear_logs_empties(self):
        core.log("hello")
        core.clear_logs()
        self.assertEqual(len(core.get_recent_logs()), 0)


class CancelScanTests(unittest.TestCase):
    def test_cancel_sets_event_and_clears_in_progress(self):
        state = {"artists": {}, "known_albums": {}, "in_progress": {"due_ids": ["a1"], "processed_ids": []}, "rate_limits": {}}
        core.save_state(state)
        core.cancel_scan()
        loaded = core.load_state()
        self.assertIsNone(loaded["in_progress"])
        self.assertTrue(core._cancel_event.is_set())
        core._cancel_event.clear()


class CreatePlaylistTests(unittest.TestCase):
    """Live tests of core.create_playlist against the mock Spotify server."""

    @classmethod
    def setUpClass(cls):
        cls.server = mock_spotify_server.MockSpotifyServer()
        cls.server.start()
        cls._orig_api_base = core.SPOTIFY_API_BASE
        cls._orig_min_interval = core.rate_limiter.min_interval_seconds
        core.SPOTIFY_API_BASE = cls.server.base_url + "/v1"
        core.rate_limiter.min_interval_seconds = 0

    @classmethod
    def tearDownClass(cls):
        core.SPOTIFY_API_BASE = cls._orig_api_base
        core.rate_limiter.min_interval_seconds = cls._orig_min_interval
        cls.server.stop()

    def test_creates_playlist_and_returns_id(self):
        playlist_id = core.create_playlist("mock-access-token", "Test Playlist")
        self.assertTrue(playlist_id.isalnum())
        self.assertTrue(playlist_id.startswith("playlist"))
        self.assertEqual(self.server.state.created_playlists[-1]["name"], "Test Playlist")

    def test_hits_me_then_users_playlists(self):
        core.create_playlist("mock-access-token", "Another")
        log = self.server.state.request_log
        self.assertIn(("GET", "/v1/me"), log)
        self.assertIn(("POST", "/v1/users/mock-user/playlists"), log)


class RunScanBlockedCategoryTests(unittest.TestCase):
    """A scan must not start a phase whose endpoint category is already
    rate-limited: it should skip the phase, keep the persisted block, and
    leave resume progress untouched so the remaining artists are checked on
    a later scan."""

    def setUp(self):
        for path in (core.STATE_FILE, core.CONFIG_FILE, core.TOKEN_FILE):
            if path.exists():
                path.unlink()
        core.clear_logs()
        core.save_config({
            "spotify_client_id": "cid", "spotify_client_secret": "cs",
            "spotify_playlist_id": "", "interval_days": 3,
            "min_request_interval": 0, "days_lookback": 365,
            "cron_schedule": "0 6 * * *", "public_base_url": "http://x",
            "flask_secret_key": "test-key",
        })
        core.save_refresh_token("test-token")

    def tearDown(self):
        if core.STATE_FILE.exists():
            core.STATE_FILE.unlink()
        core._cancel_event.clear()

    def _seed_resume_state(self, blocked_until):
        artists = [{"id": f"a{i}", "name": f"Artist {i}"} for i in range(10)]
        core.save_state({
            "artists": {},
            "known_albums": {},
            "in_progress": {
                "due_ids": [a["id"] for a in artists],
                "processed_ids": [a["id"] for a in artists[:5]],
            },
            "rate_limits": {"GET /artists/{id}/albums": blocked_until},
        })
        return artists

    def test_skips_album_phase_when_album_category_blocked(self):
        artists = self._seed_resume_state(int(time.time()) + 3600)
        with patch.object(core, "get_access_token", return_value="tok"), \
             patch.object(core, "get_followed_artists", return_value=artists) as followed, \
             patch.object(core, "get_artist_albums",
                          side_effect=AssertionError("album phase must be skipped")) as albums:
            result = core.run_scan(days=365, interval_days=3, min_request_interval=0)

        self.assertEqual(result["blocked_categories"], ["GET /artists/{id}/albums"])
        albums.assert_not_called()
        followed.assert_called_once()
        state = core.load_state()
        self.assertIn("GET /artists/{id}/albums", state["rate_limits"])
        self.assertEqual(len(state["in_progress"]["processed_ids"]), 5)

    def test_runs_album_phase_when_not_blocked(self):
        artists = [{"id": f"a{i}", "name": f"Artist {i}"} for i in range(2)]
        core.save_state({
            "artists": {},
            "known_albums": {},
            "in_progress": None,
            "rate_limits": {},
        })
        with patch.object(core, "get_access_token", return_value="tok"), \
             patch.object(core, "get_followed_artists", return_value=artists), \
             patch.object(core, "get_artist_albums", return_value=[]) as albums:
            result = core.run_scan(days=365, interval_days=3, min_request_interval=0)

        self.assertEqual(result["blocked_categories"], [])
        self.assertEqual(albums.call_count, 2)
        state = core.load_state()
        self.assertEqual(state["rate_limits"], {})
        self.assertIsNone(state["in_progress"])

    def test_blocked_until_returns_future_only(self):
        future = int(time.time()) + 3600
        past = int(time.time()) - 3600
        state = {"rate_limits": {"GET /artists/{id}/albums": future}}
        self.assertEqual(core.blocked_until(state, "GET /artists/{id}/albums"), future)
        state = {"rate_limits": {"GET /artists/{id}/albums": past}}
        self.assertIsNone(core.blocked_until(state, "GET /artists/{id}/albums"))
        self.assertIsNone(core.blocked_until({}, "GET /artists/{id}/albums"))


def tearDownModule():
    shutil.rmtree(TEST_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
