#!/usr/bin/env python3
"""One-shot: add missing albums' tracks to playlist and update state."""

import importlib.util
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "spotify_recent_albums", SCRIPT_DIR / "spotify-recent-albums.py"
)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

client_id, client_secret = m.get_client_credentials()
refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
if not refresh_token:
    print("Error: Set SPOTIFY_REFRESH_TOKEN env var.")
    sys.exit(1)
playlist_id = os.environ.get("SPOTIFY_PLAYLIST_ID")
if not playlist_id:
    print("Error: Set SPOTIFY_PLAYLIST_ID env var.")
    sys.exit(1)

token = m.get_access_token(client_id, client_secret, refresh_token)
state = m.load_state()

album_ids = [
    "5vO7AYAWlgTAKVMoHs0Bpt",
    "2jQzIrMq57RXUdN5krdfnw",
    "654l01PNNorXhMXu0vj8Jv",
    "1Cmef1jzXLr1mEGu28IhwT",
    "3obnGtCJC6klk2uDxVxW9V",
    "1UE3oGyftp6WFWQXOPdz8I",
    "3FBzXSl4FgMrQ3ntxpQqCK",
    "24OrRjy9pkvWBQNJ6IFOI0",
]

for aid in album_ids:
    album = state["known_albums"].get(aid)
    if not album:
        print(f"  SKIP {aid}: not in state")
        continue
    if album.get("added_to_playlist"):
        print(f"  SKIP {album['artist']} - {album['name']}: already in playlist")
        continue

    print(f"  {album['artist']} - {album['name']} ...", end=" ")
    try:
        uris = m.get_album_track_uris(token, aid, state)
        m.add_tracks_to_playlist(token, playlist_id, uris, state)
        album["added_to_playlist"] = True
        album["track_uris"] = uris
        print(f"added {len(uris)} track(s)")
    except Exception as e:
        album["added_to_playlist"] = False
        album["track_uris"] = []
        print(f"ERROR: {e}")

m.save_state(state)
print("\nDone. Now re-run the reorder script.")
