# Clip Cutter

A local web application for quickly editing short MP4 clips into a single film.
Browse clips in the browser, mark 3-second fragments, click Export — you get a finished film with an intro card, background music, and a "The End" card.

## Requirements

- **Python 3.8+** — stdlib only, no `pip install`
- **FFmpeg** — `ffmpeg.exe` + `ffprobe.exe` placed next to `server.py`, **or** available in PATH
- **Browser** — Chrome, Edge, or Firefox

### Getting FFmpeg

Download a pre-built Windows binary from **[gyan.dev](https://www.gyan.dev/ffmpeg/builds/)** (recommended: `ffmpeg-release-essentials.zip`).
Extract and copy `ffmpeg.exe` and `ffprobe.exe` next to `server.py`.

## Running

```bash
python server.py C:\path\to\folder\with\videos
```

The server starts at `http://localhost:8000` and automatically opens the browser.
The folder can also be entered in the UI after launching without an argument:

```bash
python server.py
```

## Folder structure

```
my_folder/
├── clip1.mp4
├── clip2.mp4
├── music/              ← optional: background music
│   ├── 01_song.mp3
│   └── 02_song.mp3
├── selections.json     ← saved automatically (resume work)
├── .clip_cache/        ← cache of cut shots (speeds up re-exports)
├── .preview/           ← H.264 previews for HEVC clips (auto-generated)
└── output/             ← finished film goes here
    └── final_20260215_143022.mp4
```

## Workflow

### 1. Browse clips

Clips are sorted chronologically by file modification date.
For each clip decide: **Take** (and from which moment) or **Skip**.

When a clip has a timestamp set, playback stops automatically 3 seconds after the mark —
you can evaluate the fragment without pausing manually.

### 2. Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `S` | Mark current time as start of 3-sec shot |
| `←` / `→` | Seek −1 / +1 second |
| `,` / `.` | Seek −1 / +1 frame (when paused) |
| `Enter` | Take clip and go to next |
| `Delete` | Skip clip and go to next |
| `PageUp` | Previous clip |
| `PageDown` | Next clip |

Double-click on the progress bar sets the mark at the click position.

### 3. Queue and music

Below the player is the **queue of selected clips** (tiles in chronological order).
Below the queue — the **music bar**: MP3 files from the `music/` subfolder shown proportional to duration. The bar scrolls in sync with the queue.

### 4. Title, day cards, and export

In the footer:
- **Title / Subtitle** — text for the intro card (saved automatically)
- **Day cards** checkbox — insert a date separator card between groups of clips filmed on different days. Days are grouped from 05:00 to 05:00 (clips before 5 AM belong to the previous day).
- **▶ EXPORT** — start generating the film
- **🗑 clear cache** — delete all cached clips and cards, forcing full re-encode on the next export

The generated film contains:
1. Intro card — title + subtitle, 4 seconds, black background
2. *(optional)* Day separator cards — date + day name, 2 seconds each
3. Selected 3-second shots in chronological order
4. "The End" card — 5 seconds, black background
5. Background music — all MP3s from `music/` in alphabetical order, fade-out on "The End"

Export progress with estimated time remaining is shown in real time.

## Shot cache (`.clip_cache/`)

Each cut shot is cached with a key based on: source filename + mtime + start time.
Subsequent exports reuse cached shots — only changed or new shots are re-encoded.

The cache is automatically invalidated when:
- the source MP4 file changes (mtime)
- the shot start time changes
- the title/subtitle changes (different MD5 → new intro card)

Use the **🗑 clear cache** button in the UI to force full regeneration.

## GPU acceleration

The server automatically detects a hardware H.264 encoder at startup and uses it for all encoding operations (clip cutting, HEVC preview transcoding, and card generation).

| GPU | Encoder used | Equivalent quality flag |
|-----|-------------|------------------------|
| NVIDIA (Turing+) | `h264_nvenc` | `-cq` (VBR mode) |
| AMD | `h264_amf` | `-qp_i / -qp_p` |
| Intel | `h264_qsv` | `-global_quality` (ICQ) |
| *(none detected)* | `libx264` (CPU) | `-crf` |

The startup log shows which encoder is active:

```
Video encoder: h264_nvenc
```

**Requirements for GPU encoding:**
- An FFmpeg build that includes the encoder (`ffmpeg -encoders | grep h264`). Builds from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) include NVENC and AMF on Windows.
- The matching GPU driver must be installed. If the driver is absent the encoder silently falls back to CPU.

**Note:** Video filters (`drawtext`, `scale`, `colorspace`, `zscale`) run on the CPU regardless of the encoder. The GPU only accelerates the encoding step itself, which is still a significant speedup for the clip cutting loop.

## HDR / HEVC support

### HEVC preview
Clips recorded in HEVC (H.265) — e.g. newer iPhones, modern Android phones — do not play natively in Chrome/Edge. The server automatically generates H.264 previews in the background (`.preview/` directory). The UI is usable immediately as each preview finishes.

### HDR → SDR conversion
If your FFmpeg includes **libzimg** (`zscale` filter), the server automatically applies Hable tone-mapping for HDR-to-SDR conversion during clip cutting. Without libzimg, a colorspace matrix fallback is used. The server prints which mode is active at startup:

```
HDR tone-mapping (zscale): yes
```

All output clips are encoded as H.264 High Profile, yuv420p, BT.709, limited range — compatible with all standard players.

## Single-window session

The server handles one active front-end at a time. Opening a new tab or reloading displaces the previous one (the old tab shows a "Session lost" message and reconnects).

## Server API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/clips` | List of clips sorted chronologically |
| `GET` | `/api/selections` | Saved user decisions |
| `GET` | `/api/music` | List of music tracks from `music/` |
| `GET` | `/api/video/{filename}` | Serve source MP4 (Range requests supported) |
| `GET` | `/api/output/{filename}` | Serve generated film |
| `GET` | `/api/export/status` | Status / progress of current export |
| `GET` | `/api/preview_status` | Status of HEVC preview generation |
| `POST` | `/api/session` | Register session (single-client guard) |
| `POST` | `/api/folder` | Load folder with videos |
| `POST` | `/api/select` | Save decision for one clip |
| `POST` | `/api/settings` | Save title and subtitle |
| `POST` | `/api/export` | Start export |
| `POST` | `/api/clear-cache` | Delete all cached clips and cards |

## `selections.json` format

Saved automatically after each decision and loaded when the folder is reopened.

```json
{
  "source_folder": "C:\\videos\\may",
  "created": "2026-02-15T14:30:00",
  "title": "May Holiday 2026",
  "subtitle": "By the sea",
  "selections": [
    {"filename": "clip01.mp4", "start_time": 3.45, "enabled": true},
    {"filename": "clip02.mp4", "start_time": 0.0,  "enabled": true},
    {"filename": "clip03.mp4", "start_time": null,  "enabled": false}
  ]
}
```

## Export pipeline

```
[intro card 4s] + [day card 2s]? + [shot 3s] + ... + [The End 5s]
         |
    concat (-c copy, no re-encoding)
         |
    music mixing (filter_complex: concat MP3s → atrim → afade → amix with video audio)
         |
    output/final_YYYYMMDD_HHmmss.mp4
```

Each shot is normalised to 1080×1920, 30 fps, H.264 High, yuv420p, BT.709 — identical parameters across all segments allow lossless stream concat. Encoding uses GPU acceleration when available (see [GPU acceleration](#gpu-acceleration)).

## License

MIT
