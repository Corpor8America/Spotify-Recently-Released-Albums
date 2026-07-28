#!/usr/bin/env python3
"""
ONE-TIME SCRIPT — run once, then delete.

Reorders the tracks currently in SPOTIFY_PLAYLIST_ID so they're sorted by
album release date: oldest release first, most recent release at the
bottom.

Only touches playlist track order. It does not add or remove any tracks
and does not modify spotify-state.json. It doesn't change the ongoing sync
behavior either -- the regular script still just appends newly-found
tracks to the end (per the "default order" decision in
recent-albums-playlist-plan.md); this is a manual, one-time cleanup of the
order, using the album/track data already recorded in spotify-state.json.

Place this file in the same directory as spotify-recent-albums.py before
running it (it imports that module directly, so the exclusion logic and
API plumbing can never drift out of sync with what the scheduled job
actually does).

Usage:
  SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy SPOTIFY_REFRESH_TOKEN=zzz \
  SPOTIFY_PLAYLIST_ID=your_playlist_id \
    python reorder_playlist_once.py

After running and confirming the playlist looks right in Spotify, delete
this script -- it has no ongoing purpose once the playlist is sorted.
"""

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "spotify_recent_albums", SCRIPT_DIR / "spotify-recent-albums.py"
)
main_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main_module)


def build_ordered_track_uris(state):
    """Every album currently reflected in the playlist (added_to_playlist:
    true, not effectively excluded), sorted by release date ascending --
    oldest first, most recent last -- with each album's own tracks kept in
    their existing order."""
    albums = [
        a for a in state.get("known_albums", {}).values()
        if a.get("added_to_playlist") and not main_module.is_effectively_excluded(a)
    ]

    def sort_key(album):
        parsed = main_module.parse_release_date(album["release_date"])
        # Albums with an unparseable date sort first rather than crashing
        # the comparison against real datetimes.
        return parsed if parsed is not None else datetime.min

    albums.sort(key=sort_key)

    uris = []
    for album in albums:
        uris.extend(album.get("track_uris") or [])
    return albums, uris


def replace_playlist_items(token, playlist_id, track_uris, state):
    """Dev Mode apps can't PUT (replace) a playlist. Instead, DELETE all
    current tracks then POST them back in the desired order."""

    url = f"{main_module.SPOTIFY_API_BASE}/playlists/{playlist_id}/items"

    for i in range(0, len(track_uris), 100):
        chunk = track_uris[i:i + 100]
        items = [{"uri": uri} for uri in chunk]
        main_module.spotify_request("DELETE", token, url, state, json_data={"items": items})

    for i in range(0, len(track_uris), 100):
        chunk = track_uris[i:i + 100]
        main_module.spotify_request("POST", token, url, state, json_data={"uris": chunk})


def main():
    playlist_id = os.environ.get("SPOTIFY_PLAYLIST_ID")
    if not playlist_id:
        print("Error: Set SPOTIFY_PLAYLIST_ID env var.")
        sys.exit(1)

    client_id, client_secret = main_module.get_client_credentials()
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    if not refresh_token:
        print("Error: Set SPOTIFY_REFRESH_TOKEN env var.")
        sys.exit(1)

    token = main_module.get_access_token(client_id, client_secret, refresh_token)
    state = main_module.load_state()

    albums, ordered_uris = build_ordered_track_uris(state)
    if not ordered_uris:
        print("No playlisted tracks found in spotify-state.json (nothing to reorder).")
        return

    print(f"Reordering {len(ordered_uris)} track(s) from {len(albums)} album(s), "
          f"oldest release first, newest at the bottom:\n")
    for album in albums:
        print(f"  {album['release_date']}  {album['artist']} - {album['name']}")

    replace_playlist_items(token, playlist_id, ordered_uris, state)
    print("\nDone. Check the playlist in Spotify, then delete this script.")


if __name__ == "__main__":
    main()