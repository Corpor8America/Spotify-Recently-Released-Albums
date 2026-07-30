# Ideas from Selbi182/SpotifyDiscoveryBot

[SpotifyDiscoveryBot](https://github.com/Selbi182/SpotifyDiscoveryBot) and its
companion library
[SpotifyDependencies](https://github.com/Selbi182/SpotifyDependencies) are a
Java-based always-running bot that crawls followed artists and sorts releases
into separate playlists by type. Several of its ideas could apply to this
Python/CI project.

---

## 1. Album-type classification beyond `album_type == "album"`

We currently only include Spotify's `album_type == "album"` (line 707).
SpotifyDependencies has three utility methods that detect additional types,
ported below.

### EP detection (`isExtendedPlay`)

An album labeled `album_type= single` is reclassified as EP if:
- Title matches `\bE\W?P\W?\b`
- ≥5 tracks or ≥20 min total duration
- ≥3 tracks and ≥10 min and no title track

```python
import re

EP_MATCHER = re.compile(r"\bE\W?P\W?\b")
EP_SONG_COUNT = 5
EP_DURATION_MS = 20 * 60 * 1000
EP_SONG_COUNT_LESSER = 3
EP_DURATION_LESSER_MS = 10 * 60 * 1000

def is_ep(album_name, tracks):
    if EP_MATCHER.search(album_name):
        return True
    count = len(tracks)
    total_ms = sum(t["duration_ms"] for t in tracks)
    if count >= EP_SONG_COUNT or total_ms >= EP_DURATION_MS:
        return True
    if count >= EP_SONG_COUNT_LESSER and total_ms >= EP_DURATION_LESSER_MS:
        stripped = stripped_title(album_name)
        return not any(stripped_title(t["name"]) == stripped for t in tracks)
    return False
```

### Remix detection (`isRemix`)

Title matches `\b(RMX|REMIX+|REMIXES)\b`:
- If album title matches → ≥20% of tracks must also match
- Otherwise → ≥67% of tracks must match

```python
REMIX_MATCHER = re.compile(r"\b(RMX|REMIX\+|REMIXES)\b", re.IGNORECASE)

def is_remix(album_name, tracks):
    has_remix_title = bool(REMIX_MATCHER.search(album_name))
    track_names = [t["name"] for t in tracks]
    remix_count = sum(1 for n in track_names if REMIX_MATCHER.search(n))
    ratio = remix_count / len(track_names) if track_names else 0
    threshold = 0.2 if has_remix_title else 0.67
    return ratio > threshold
```

### Live detection (`isLiveRelease`)

Two-tier:
- If >90% of track titles match `\b(LIVE|SHOW|TOUR)\b` and >3 tracks → immediate true
- Otherwise, fetch audio features and check average `liveness > 0.55` (>0.40 if album title has live keywords)

```python
LIVE_MATCHER = re.compile(r"\b(LIVE|SHOW|TOUR)\b", re.IGNORECASE)
LIVE_MATCHER_EXTRA = re.compile(
    r"(\bLIVE\W*$|\bLIVE.*?\b(\d{4}|(IN|AT|ON|PERFORMANCE|SHOW|CONCERT|SESSION))\b)",
    re.IGNORECASE,
)

def is_live(album_name, tracks, audio_features=None):
    track_names = [t["name"] for t in tracks]
    live_tracks = sum(1 for n in track_names if LIVE_MATCHER.search(n))
    ratio = live_tracks / len(track_names) if track_names else 0
    if ratio > 0.9 and (len(tracks) > 3 or LIVE_MATCHER_EXTRA.search(album_name)):
        return True
    if any(LIVE_MATCHER.search(n) for n in [album_name] + track_names):
        if audio_features:
            avg = sum(f.get("liveness", 0) for f in audio_features) / len(audio_features)
            threshold = 0.4 if LIVE_MATCHER.search(album_name) else 0.55
            return avg > threshold
    return False
```

### Re-release detection (`containsRereleaseWord`)

```python
RE_RELEASE_MATCHER = re.compile(
    r"(anniversary|re\W?(issue|master|record)|\d+\W+(jahr|year))",
    re.IGNORECASE,
)

def is_rerelease(album_name):
    m = RE_RELEASE_MATCHER.search(album_name)
    return bool(m and m.start() > 0)
```

The Java version also checks track playability (all tracks must be playable)
and recency to decide whether to classify or discard.

---

## 2. Per-artist blacklist by type

Allow skipping specific types for specific artists (e.g. "no remixes from
Artist X"):

```python
# In state.json or a config file:
artist_type_blacklist = {
    "7dGJo4pcD2V6oG8kP0tJRR": {"remix", "re_release"},
}
```

---

## 3. Separate playlists by type (optional)

Instead of one playlist, create separate playlists per type:
`Album`, `Single`, `EP`, `Remix`, `Live`, `Re-Release`.

Configured by mapping each type to a playlist ID (blank = disabled):

```python
playlist_map = {
    "album": os.environ.get("SPOTIFY_PLAYLIST_ID_ALBUM"),
    "ep": os.environ.get("SPOTIFY_PLAYLIST_ID_EP"),
    "live": os.environ.get("SPOTIFY_PLAYLIST_ID_LIVE"),
}
```

---

## 4. Circular playlist fitting

Spotify playlists cap at 10,000 tracks. The DiscoveryBot rotates tracks:
newest in, oldest out. Our current prune function already removes aged-out
tracks, so we'd hit this only if someone follows enough artists that the
daily window stays full indefinitely. Low priority.

---

## 5. Webhook forwarder

The DiscoveryBot can relay new releases to a webhook URL (Discord, etc.).
Could be added as a simple `requests.post(url, json=payload)` after
discovery — no code dependency needed.

---

## Summary: what's most worth doing

| Feature | Effort | Value |
|---------|--------|-------|
| EP detection (port `isExtendedPlay`) | Small | Medium — catches multi-track singles |
| Remix detection (port `isRemix`) | Small | Low — not many true remix albums |
| Live detection (port `isLiveRelease`) | Medium (needs audio features API call) | Medium — catches live albums mislabeled as regular albums |
| Re-release detection (port regex) | Trivial | Low — already mostly caught by paren exclusion |
| Per-artist type blacklist | Small | Medium — useful for noisy artists |
| Separate playlists by type | Medium | Low — adds complexity for a single-user CI script |
| Circular playlist fitting | Low | Low — won't hit 10k in practice |
| Webhook forwarder | Small | Medium — push notifications on new finds |
