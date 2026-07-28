import importlib.util
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "spotify-recent-albums.py"
SPEC = importlib.util.spec_from_file_location("spotify_recent_albums", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PrunePlaylistTests(unittest.TestCase):
    def test_removes_aged_out_album_tracks(self):
        state = {
            "known_albums": {
                "alb1": {
                    "name": "Old Album", "release_date": "2020-01-01",
                    "added_to_playlist": True, "track_uris": ["spotify:track:a", "spotify:track:b"],
                }
            },
            "artists": {}, "in_progress": None,
        }
        calls = []
        def fake_remove(token, playlist_id, uris, state):
            calls.append(uris)
        with patch.object(MODULE, "remove_tracks_from_playlist", side_effect=fake_remove):
            with patch.object(MODULE, "save_state", return_value=None):
                MODULE.prune_playlist("token", state, 365, "playlist123")
        self.assertEqual(calls, [["spotify:track:a", "spotify:track:b"]])
        self.assertFalse(state["known_albums"]["alb1"]["added_to_playlist"])

    def test_shared_track_is_not_removed(self):
        state = {
            "known_albums": {
                "old": {
                    "name": "Old", "release_date": "2020-01-01",
                    "added_to_playlist": True, "track_uris": ["spotify:track:shared", "spotify:track:only_old"],
                },
                "current": {
                    "name": "Current", "release_date": "2026-07-01",
                    "added_to_playlist": True, "track_uris": ["spotify:track:shared"],
                },
            },
            "artists": {}, "in_progress": None,
        }
        calls = []
        def fake_remove(token, playlist_id, uris, state):
            calls.append(uris)
        with patch.object(MODULE, "remove_tracks_from_playlist", side_effect=fake_remove):
            with patch.object(MODULE, "save_state", return_value=None):
                MODULE.prune_playlist("token", state, 365, "playlist123")
        self.assertEqual(calls, [["spotify:track:only_old"]])

    def test_missing_track_uris_falls_back_to_fetch(self):
        state = {
            "known_albums": {
                "alb1": {
                    "name": "Old Album", "release_date": "2020-01-01",
                    "added_to_playlist": True,
                }
            },
            "artists": {}, "in_progress": None,
        }
        with patch.object(MODULE, "get_album_track_uris", return_value=["spotify:track:fetched"]) as fetch_mock:
            with patch.object(MODULE, "remove_tracks_from_playlist", return_value=None) as remove_mock:
                with patch.object(MODULE, "save_state", return_value=None):
                    MODULE.prune_playlist("token", state, 365, "playlist123")
        fetch_mock.assert_called_once()
        remove_mock.assert_called_once_with("token", "playlist123", ["spotify:track:fetched"], state)

    def test_no_playlist_id_is_noop(self):
        state = {"known_albums": {"alb1": {"name": "X", "release_date": "2020-01-01", "added_to_playlist": True, "track_uris": ["u"]}}, "artists": {}, "in_progress": None}
        with patch.object(MODULE, "remove_tracks_from_playlist") as remove_mock:
            MODULE.prune_playlist("token", state, 365, None)
        remove_mock.assert_not_called()

    def test_not_yet_expired_album_is_untouched(self):
        state = {"known_albums": {"alb1": {"name": "X", "release_date": "2026-07-01", "added_to_playlist": True, "track_uris": ["u"]}}, "artists": {}, "in_progress": None}
        with patch.object(MODULE, "remove_tracks_from_playlist") as remove_mock:
            MODULE.prune_playlist("token", state, 365, "playlist123")
        remove_mock.assert_not_called()

    def test_manually_excluded_album_is_pruned(self):
        state = {
            "known_albums": {
                "alb1": {
                    "name": "Live Album (Live)", "release_date": "2026-07-01",
                    "added_to_playlist": True, "track_uris": ["spotify:track:x"],
                    "manual_override": True,
                }
            },
            "artists": {}, "in_progress": None,
        }
        calls = []
        def fake_remove(token, playlist_id, uris, state):
            calls.append(uris)
        with patch.object(MODULE, "remove_tracks_from_playlist", side_effect=fake_remove):
            with patch.object(MODULE, "save_state", return_value=None):
                MODULE.prune_playlist("token", state, 365, "playlist123")
        self.assertEqual(calls, [["spotify:track:x"]])
        self.assertFalse(state["known_albums"]["alb1"]["added_to_playlist"])

    def test_manually_included_album_is_not_pruned(self):
        state = {
            "known_albums": {
                "alb1": {
                    "name": "Live Album (Live)", "release_date": "2026-07-01",
                    "added_to_playlist": True, "track_uris": ["spotify:track:x"],
                    "auto_excluded": True, "manual_override": False,
                }
            },
            "artists": {}, "in_progress": None,
        }
        with patch.object(MODULE, "remove_tracks_from_playlist") as remove_mock:
            MODULE.prune_playlist("token", state, 365, "playlist123")
        remove_mock.assert_not_called()

    def test_shared_track_excluded_and_current(self):
        state = {
            "known_albums": {
                "excluded": {
                    "name": "Live (Live)", "release_date": "2026-07-01",
                    "added_to_playlist": True, "track_uris": ["spotify:track:shared", "spotify:track:only_excluded"],
                    "manual_override": True,
                },
                "current": {
                    "name": "Current", "release_date": "2026-07-01",
                    "added_to_playlist": True, "track_uris": ["spotify:track:shared"],
                },
            },
            "artists": {}, "in_progress": None,
        }
        calls = []
        def fake_remove(token, playlist_id, uris, state):
            calls.append(uris)
        with patch.object(MODULE, "remove_tracks_from_playlist", side_effect=fake_remove):
            with patch.object(MODULE, "save_state", return_value=None):
                MODULE.prune_playlist("token", state, 365, "playlist123")
        self.assertEqual(calls, [["spotify:track:only_excluded"]])


class IsAutoExcludedTests(unittest.TestCase):
    def test_plain_name(self):
        self.assertFalse(MODULE.is_auto_excluded("Album Name"))

    def test_live(self):
        self.assertTrue(MODULE.is_auto_excluded("Album Name (Live)"))

    def test_remastered(self):
        self.assertTrue(MODULE.is_auto_excluded("Album Name (Remastered)"))

    def test_deluxe_edition(self):
        self.assertTrue(MODULE.is_auto_excluded("Album Name (Deluxe Edition)"))

    def test_trailing_whitespace(self):
        self.assertTrue(MODULE.is_auto_excluded("Album Name (Deluxe Edition) "))

    def test_mid_string_parenthetical_is_excluded_when_trailing(self):
        self.assertTrue(MODULE.is_auto_excluded("Album Name (Part One)"))

    def test_mid_string_parenthetical_not_trailing(self):
        self.assertFalse(MODULE.is_auto_excluded("Album Name (Part One) - Bonus Track"))


class IsEffectivelyExcludedTests(unittest.TestCase):
    def test_auto_excluded_no_override(self):
        self.assertTrue(MODULE.is_effectively_excluded({"auto_excluded": True}))

    def test_auto_excluded_override_false(self):
        self.assertFalse(MODULE.is_effectively_excluded({"auto_excluded": True, "manual_override": False}))

    def test_not_auto_excluded_override_true(self):
        self.assertTrue(MODULE.is_effectively_excluded({"auto_excluded": False, "manual_override": True}))

    def test_not_auto_excluded_no_override(self):
        self.assertFalse(MODULE.is_effectively_excluded({"auto_excluded": False}))

    def test_no_fields(self):
        self.assertFalse(MODULE.is_effectively_excluded({}))


class RecordAlbumTests(unittest.TestCase):
    def test_preserves_manual_override(self):
        state = {"known_albums": {"alb1": {"manual_override": True, "auto_excluded": False}}}
        artist = {"name": "Artist", "id": "art1"}
        album = {"id": "alb1", "name": "Live (Live)", "album_type": "album",
                 "release_date": "2026-01-01", "external_urls": {"spotify": "http://x"},
                 "total_tracks": 5}
        MODULE.record_album(state, artist, album, "2026-07-01T00:00:00+00:00")
        self.assertTrue(state["known_albums"]["alb1"]["manual_override"])
        self.assertTrue(state["known_albums"]["alb1"]["auto_excluded"])

    def test_new_album_has_auto_excluded(self):
        state = {"known_albums": {}}
        artist = {"name": "Artist", "id": "art1"}
        album = {"id": "alb2", "name": "Studio Album", "album_type": "album",
                 "release_date": "2026-01-01", "external_urls": {"spotify": "http://x"},
                 "total_tracks": 10}
        MODULE.record_album(state, artist, album, "2026-07-01T00:00:00+00:00")
        self.assertFalse(state["known_albums"]["alb2"]["auto_excluded"])
        self.assertIsNone(state["known_albums"]["alb2"]["manual_override"])


if __name__ == "__main__":
    unittest.main()