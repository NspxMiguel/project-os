# BirdTunes — App Contract

> Supersedes section 10 of `ARCHITECTURE.md` wherever the two disagree. Everything
> here is normative.

## 1. What it is

A scheduled music player for pet birds, running on the project-os box and casting to a
real speaker on the network (Apple TV / HomePod over AirPlay, Chromecast, a Home
Assistant media player, or the Pi's own audio jack).

**The schedule is the product.** The normal state of BirdTunes is *idle and waiting*.
You tell it the hours you want music — typically the hours you are out of the house —
and it wakes up, picks a playlist, and shuffles through it until the window closes.
Manual play exists, but it is the exception, not the main path.

Three ideas, in order of importance:

1. **Windows** — time ranges, per weekday, each pointing at a playlist.
2. **Playlists** — named collections. Playback inside a window is random within the
   window's playlist, weighted by what the birds have reacted well to.
3. **Sources** — a track is a local audio file (any format) or something imported from
   YouTube (single video or a whole playlist), downloaded once and cached locally.

## 2. Audio format compatibility — read this before writing player code

This is the single most likely source of "why is nothing playing", so it is modelled
explicitly rather than discovered at runtime.

| Output backend | Plays | Verified how |
|---|---|---|
| `airplay` (pyatv RAOP) | **WAV, FLAC, MP3, Vorbis/OGG only** | pyatv decodes with `miniaudio`; `miniaudio.FileFormat` is exactly `{UNKNOWN, WAV, FLAC, MP3, VORBIS}` |
| `chromecast` | MP3, AAC/M4A, Opus, Vorbis, FLAC, WAV, WebM | Google Cast supported media types |
| `ha_player` | delegated to the HA entity — assume broad, verify on failure | — |
| `local` | whatever the detected binary handles (mpv/ffplay/cvlc/aplay) | — |

Consequences that must be implemented, not just documented:

- **AirPlay cannot play AAC/M4A or Opus/WebM.** YouTube serves exactly those. So a
  YouTube import destined for an Apple TV or HomePod must be transcoded to MP3, which
  requires **ffmpeg**. Local `.m4a` files the user already owns hit the same wall.
- Every track row stores `container` and `codec`. `Track.compatible_with(backend)` is a
  pure function over those two fields, unit-tested against the table above.
- The library, the playlist views and the queue builder **filter by the current output's
  compatibility** and surface the count of excluded tracks (e.g. "12 tracks hidden —
  your Apple TV can't play AAC. Install ffmpeg to convert them."). A track is never
  silently skipped mid-session without the reason being visible somewhere.
- `POST /convert` re-encodes selected incompatible tracks to MP3 when ffmpeg is present.
  Without ffmpeg the endpoint returns a clean 409 naming the missing binary.

## 3. Sources

### Local files
Any extension in `library.extensions` (default `.mp3 .flac .wav .ogg .oga .opus .m4a
.aac .wma .aiff`). Scanned from `library.paths`; default `~/.project_os/media/birdtunes`.
Nothing is copied or moved — the user's files stay where they are.

### YouTube
Optional dependency `yt-dlp` (pure Python, works on ARM, Python 3.9+; verified
`yt-dlp==2025.10.14`).

**Download and cache. Never stream at playback time.** Reasons, in order:
YouTube media URLs expire within hours and a scheduled player would break silently
overnight; AirPlay needs a locally decodable file; re-fetching the same tracks daily
wastes the Pi's bandwidth and SD card; and a cached library keeps playing when the
internet drops.

Import accepts a single video URL, a playlist URL, or a channel/mix URL. A playlist URL
may be imported **as a BirdTunes playlist** (creating one named after the YouTube
playlist) or flattened into an existing playlist.

`yt-dlp` is invoked in-process via `YoutubeDL` (never by shelling out), inside
`run_in_executor` because it blocks. Options:

```python
{
  "format": "bestaudio/best",
  "outtmpl": str(dest_dir / "%(title).120B [%(id)s].%(ext)s"),
  "noplaylist": <True unless importing a playlist>,
  "ignoreerrors": True,          # one dead video must not kill a 200-track import
  "quiet": True, "no_warnings": True, "noprogress": True,
  "cachedir": False,
  "retries": 3, "fragment_retries": 3, "socket_timeout": 30,
  "progress_hooks": [hook],
  "postprocessors": [{"key": "FFmpegExtractAudio",
                      "preferredcodec": "mp3",
                      "preferredquality": config("import.youtube.quality", "192")}]
                    if ffmpeg_available() else [],
}
```

- Listing a playlist first uses `extract_flat: "in_playlist"` so the UI can show
  "47 tracks found" before committing to a long download.
- Dedupe on the YouTube video id (`source_id`): re-importing a playlist adds only what
  is new, and never downloads a video already in the library.
- Imports run through a **single-worker queue** — a Pi 3B must not run four ffmpeg
  processes at once. Progress, per-item state and errors are published on
  `app.birdtunes.import` and persisted so a page reload does not lose track of a job.
- Cancellable mid-download; partial files are cleaned up.
- Age-restricted / private / region-blocked videos fail as *that item*, with the real
  reason recorded, and the import continues.
- Store `source="youtube"`, `source_id`, `source_url`, `thumbnail`, and the resolved
  local path. Uploader becomes `artist` when no better tag exists.

Legal note for the README (one paragraph, no lecturing): downloading is subject to
YouTube's Terms of Service and to the rights in the content; the user is responsible for
what they import, and the project ships no media of its own.

### Stream URLs
A direct http(s) audio URL (internet radio) is stored with `source="url"` and played
without downloading. Marked as non-shuffleable — a radio stream has no end.

## 4. Data model

Additions and changes to the tables in `ARCHITECTURE.md` §10:

```sql
-- tracks gains:
source_id TEXT, source_url TEXT, container TEXT, codec TEXT,
filesize INTEGER, thumbnail TEXT, bitrate INTEGER

app_birdtunes_playlists(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
  source TEXT,              -- "manual" | "youtube" | "smart"
  source_url TEXT, color TEXT, shuffle INTEGER DEFAULT 1,
  created_at TEXT, updated_at TEXT)

app_birdtunes_playlist_tracks(
  playlist_id TEXT, track_id TEXT, position INTEGER, added_at TEXT,
  PRIMARY KEY(playlist_id, track_id))

app_birdtunes_imports(
  id TEXT PRIMARY KEY, kind TEXT, url TEXT, title TEXT,
  state TEXT,               -- queued | running | done | error | cancelled
  progress REAL, total INTEGER, completed INTEGER, message TEXT,
  playlist_id TEXT, created_at TEXT, finished_at TEXT)
```

The playlist id `"all"` is **virtual and always present**: every non-blocked, non-missing
track. It cannot be renamed or deleted, and it is the default for a window with no
playlist set — so a fresh install with one scanned folder already works.

## 5. Schedule

Config `apps.settings.birdtunes.schedule`:

```yaml
schedule:
  enabled: true
  quiet_hours: {start: "20:00", end: "07:00"}
  windows:
    - id: "w1"
      name: "While I'm out"
      start: "09:00"
      end: "17:00"
      days: [0, 1, 2, 3, 4]        # 0 = Monday
      playlist_id: "all"
      volume: 0.3                  # optional per-window override
      enabled: true
```

Rules:

- Windows are evaluated in order; **the first enabled window matching now wins**.
  Overlaps are legal and resolved by order, which the UI must make visible.
- A window crossing midnight (`start > end`) is valid and spans the day boundary.
- `quiet_hours` always beats a window: entering quiet hours **stops** playback in
  progress, it does not merely refuse to start.
- `playback.max_session_minutes` still caps a single continuous run inside a long window.
- Presets the UI offers with one click — they only prefill the editor, they are not
  special-cased in the backend: *While I'm out (weekdays 09:00–17:00)*,
  *Mornings (07:00–10:00, daily)*, *Afternoons (14:00–18:00, daily)*,
  *Weekends only (10:00–16:00, Sat–Sun)*.
- `next_change()` returns what happens next and when ("plays at 09:00 tomorrow",
  "stops at 17:00") — the UI shows this permanently, because a scheduled app that looks
  dead is indistinguishable from a broken one.

## 6. Playback

Within a window: build the candidate set from the window's playlist, minus blocked,
minus missing files, minus tracks incompatible with the current output. Then
`recommender.pick_next()` over that set — random, but weighted by like / play-less /
dislike, recency and time-of-day tags, exactly as specified in `ARCHITECTURE.md` §10.
Shuffle is the default and the point ("vai tocando aleatoriamente sua playlist"); a
playlist with `shuffle = 0` plays in `position` order instead.

If the candidate set is empty, the app emits a state with a specific, actionable reason
(`no_tracks`, `all_incompatible`, `output_unreachable`, `quiet_hours`) — never a bare
"stopped".

## 7. API additions

Under `/api/apps/birdtunes`, in addition to §10:

```
GET    /playlists                        list (with track counts and total duration)
POST   /playlists                        {name, description?, color?, shuffle?}
GET    /playlists/{id}
PATCH  /playlists/{id}
DELETE /playlists/{id}                   (refuses "all")
POST   /playlists/{id}/tracks            {track_ids: []}
DELETE /playlists/{id}/tracks            {track_ids: []}
POST   /playlists/{id}/reorder           {track_ids: []}   full ordering
POST   /playlists/{id}/play              start now, ignoring the schedule

POST   /import/youtube                   {url, playlist_id?, as_playlist?} -> {job_id}
GET    /import/preview?url=              flat listing before committing
GET    /import                           jobs (active + recent)
DELETE /import/{job_id}                  cancel

GET    /schedule                         windows + quiet hours + next_change
PUT    /schedule
GET    /schedule/presets

GET    /compat                           current output, supported formats,
                                         counts of compatible/incompatible tracks
POST   /convert                          {track_ids: []} -> transcode to MP3 (409 without ffmpeg)
```

Event topics: `app.birdtunes.state`, `app.birdtunes.import`, `app.birdtunes.library`.

## 8. UI additions (panel.js)

- **Playlists** section: cards with name, track count, duration, colour; create, rename,
  delete, drag tracks in, reorder, "Play now".
- **Import from YouTube**: a URL field that previews what it found (title, item count,
  thumbnails) before downloading; a target selector (new playlist / existing playlist);
  a live progress list with per-item state and a cancel button; and, when ffmpeg is
  missing, a plain-language warning that imports will not play on AirPlay outputs plus
  the exact command to install it.
- **Schedule editor**: the 24-hour strip from §11 of the architecture, now showing which
  playlist owns each band, overlap highlighting, weekday toggles, preset buttons, and a
  permanent "what happens next" line.
- **Compatibility banner** on the library when the current output cannot play part of it,
  with a one-click "Convert N tracks to MP3" when ffmpeg is available.

## 9. Tests this adds

- `compatible_with()` against the whole format table, both directions
- window matching: order precedence, weekday filtering, midnight crossing,
  quiet-hours override, empty schedule
- `next_change()` for: inside a window, before today's window, after the last window of
  the week, and with the schedule disabled
- playlist CRUD, the virtual `all` playlist, delete protection, reordering
- YouTube import with `yt_dlp.YoutubeDL` faked at the seam — no network in tests:
  single video, playlist, dedupe on `source_id`, one failing item inside a good playlist,
  cancellation, and the no-ffmpeg path producing a non-MP3 track flagged as
  AirPlay-incompatible
- candidate-set filtering produces the right empty-reason code
