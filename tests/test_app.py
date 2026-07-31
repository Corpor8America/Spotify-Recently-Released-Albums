import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import flask
except ImportError:
    flask = None

TEST_DIR = Path(tempfile.mkdtemp())

os.environ["DATA_DIR"] = str(TEST_DIR)
os.environ["RUN_SCHEDULER"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import spotify_core as core

core.DATA_DIR = TEST_DIR
core.CONFIG_FILE = TEST_DIR / "app-config.json"
core.STATE_FILE = TEST_DIR / "spotify-state.json"
core.TOKEN_FILE = TEST_DIR / "spotify-token.json"
core.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

_config = {
    "spotify_client_id": "test_client_id",
    "spotify_client_secret": "test_client_secret",
    "spotify_playlist_id": "",
    "interval_days": 7,
    "min_request_interval": 0,
    "days_lookback": 365,
    "cron_schedule": "0 6 * * *",
    "public_base_url": "http://localhost:8080",
    "flask_secret_key": "test-secret-key-for-testing",
}
with open(core.CONFIG_FILE, "w") as f:
    json.dump(_config, f)

if flask is not None:
    from app import app


@unittest.skipIf(flask is None, "flask not installed")
class AppRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.client.testing = True
        self._clean_data_files()
        self._reset_config()

    def tearDown(self):
        self._clean_data_files()
        self._reset_config()

    def _clean_data_files(self):
        for p in [core.STATE_FILE, core.TOKEN_FILE]:
            if p.exists():
                p.unlink()

    def _reset_config(self):
        with open(core.CONFIG_FILE, "w") as f:
            json.dump(_config, f)

    def _write_config(self, overrides):
        cfg = dict(_config)
        cfg.update(overrides)
        with open(core.CONFIG_FILE, "w") as f:
            json.dump(cfg, f)

    def _write_token(self, token="test_refresh"):
        with open(core.TOKEN_FILE, "w") as f:
            json.dump({"refresh_token": token}, f)

    # --- dashboard -----------------------------------------------------------

    def test_dashboard_redirects_when_not_configured(self):
        self._write_config({"spotify_client_id": "", "spotify_client_secret": ""})
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/settings", response.location)

    def test_dashboard_renders_when_configured(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Recent Albums", response.data)

    def test_dashboard_shows_not_connected_when_no_token(self):
        response = self.client.get("/")
        self.assertIn(b"Not connected", response.data)

    def test_dashboard_shows_connected_when_token_exists(self):
        self._write_token()
        response = self.client.get("/")
        self.assertIn(b"Connected", response.data)

    # --- settings ------------------------------------------------------------

    def test_settings_page(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Settings", response.data)

    def test_settings_post_redirects(self):
        response = self.client.post("/settings", data={
            "spotify_client_id": "new_id",
            "spotify_client_secret": "new_secret",
            "spotify_playlist_id": "",
            "interval_days": "3",
            "min_request_interval": "10",
            "days_lookback": "365",
            "cron_schedule": "0 6 * * *",
    "public_base_url": "http://127.0.0.1:8081",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)

    # --- login ---------------------------------------------------------------

    def test_login_redirects_to_spotify(self):
        response = self.client.get("/login", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.spotify.com", response.location)

    def test_login_returns_500_when_no_client_id(self):
        self._write_config({"spotify_client_id": "", "spotify_client_secret": ""})
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 500)

    # --- callback ------------------------------------------------------------

    def test_callback_missing_code(self):
        response = self.client.get("/callback")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Missing authorization code", response.data)

    def test_callback_with_error(self):
        response = self.client.get("/callback?error=access_denied")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"access_denied", response.data)

    # --- scan actions --------------------------------------------------------

    @patch("app.core.run_scan")
    def test_run_now_triggers_scan(self, mock_run):
        response = self.client.post("/run", follow_redirects=False)
        self.assertEqual(response.status_code, 302)

    @patch("app.core.cancel_scan")
    def test_cancel_calls_cancel(self, mock_cancel):
        response = self.client.post("/cancel", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        mock_cancel.assert_called_once()

    # --- album overrides -----------------------------------------------------

    def _init_state_with_album(self):
        state = {
            "artists": {}, "known_albums": {}, "in_progress": None, "rate_limits": {},
        }
        state["known_albums"]["alb1"] = {
            "name": "Test Album", "artist": "Test Artist",
            "release_date": "2026-07-01", "auto_excluded": False,
            "manual_override": None, "added_to_playlist": False,
        }
        core.save_state(state)

    def test_set_override_true(self):
        self._init_state_with_album()
        response = self.client.post("/albums/alb1/override", data={"value": "true"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        loaded = core.load_state()
        self.assertTrue(loaded["known_albums"]["alb1"]["manual_override"])

    def test_set_override_false(self):
        self._init_state_with_album()
        response = self.client.post("/albums/alb1/override", data={"value": "false"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        loaded = core.load_state()
        self.assertFalse(loaded["known_albums"]["alb1"]["manual_override"])

    def test_set_override_unknown_album(self):
        response = self.client.post("/albums/nonexistent/override", data={"value": "true"})
        self.assertEqual(response.status_code, 404)

    # --- status API ----------------------------------------------------------

    def test_status_returns_json(self):
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("connected", data)
        self.assertIn("scan_running", data)
        self.assertIn("known_albums_count", data)

    def test_status_connected_false_when_no_token(self):
        response = self.client.get("/status")
        data = json.loads(response.data)
        self.assertFalse(data["connected"])

    def test_status_connected_true_with_token(self):
        self._write_token()
        response = self.client.get("/status")
        data = json.loads(response.data)
        self.assertTrue(data["connected"])


def tearDownModule():
    shutil.rmtree(TEST_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
