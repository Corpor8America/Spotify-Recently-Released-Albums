#!/usr/bin/env python3
"""
ONE-TIME SCRIPT — run once, then delete.

Sets `manual_override: true` on every album in `spotify-state.json` whose
name matches the trailing-parenthetical exclusion pattern. Albums that
already have a `manual_override` value are left alone.

No Spotify credentials needed — this only updates the local JSON file.
Excluded albums will be pruned from the playlist on the next scheduled run.

Usage:
  python spotify/backfill_exclusions_once.py

After running and confirming the output looks right, commit the updated
spotify-state.json and delete this script.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "spotify_recent_albums", SCRIPT_DIR / "spotify-recent-albums.py"
)
main_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main_module)


def main():
    state = main_module.load_state()

    print("Setting manual_override: true on excluded albums...")
    excluded = []
    already_overridden = []
    for album_id, album in state.get("known_albums", {}).items():
        if album.get("manual_override") is not None:
            already_overridden.append((album["name"], album["manual_override"]))
            continue
        if main_module.is_auto_excluded(album["name"]):
            album["manual_override"] = True
            excluded.append(album["name"])

    main_module.save_state(state)
    print(f"Excluded {len(excluded)} album(s) by setting manual_override: true.")
    for name in excluded:
        print(f"  {name}")
    if already_overridden:
        print(f"Skipped {len(already_overridden)} album(s) that already had manual_override set:")
        for name, value in already_overridden:
            print(f"  {name}: manual_override={value!r}")

    print("Done. Review spotify-state.json, commit it, then delete this script.")


if __name__ == "__main__":
    main()
