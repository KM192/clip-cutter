#!/usr/bin/env python3
"""
Clip Cutter - tool for quick editing of short video clips

Usage: python server.py [path/to/folder/with/videos]
  If folder is not provided, it can be selected in the UI.
"""

import sys
import os
import json
import uuid
import subprocess
import threading
import datetime
import webbrowser
import re
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from socketserver import ThreadingMixIn

PORT = 8000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WIN = sys.platform == 'win32'
_NO_WINDOW = 0x08000000  # Windows: CREATE_NO_WINDOW


def _find_tool(name):
    """Looks for tool next to server.py first, then in PATH."""
    local = os.path.join(SCRIPT_DIR, name + ('.exe' if IS_WIN else ''))
    if os.path.isfile(local):
        return local
    return name  # fallback: PATH


FFMPEG  = _find_tool('ffmpeg')
FFPROBE = _find_tool('ffprobe')

# ─── Global state ────────────────────────────────────────────────────────────

state_lock = threading.Lock()
state = {
    'folder': None,
    'clips': [],        # [{id, filename, duration, modified}]
    'selections': {},   # filename -> {filename, start_time, enabled}
    'title': '',
    'subtitle': '',
    'music': [],        # [{filename, duration}]
}

export_lock = threading.Lock()
export_status = {'status': 'idle', 'progress': '', 'percent': 0, 'output': ''}

# Single-client session guard: only the most recently connected tab is active
_session = {'id': None}
_session_lock = threading.Lock()

# HEVC preview cache: filename -> preview_path (or None if transcode failed)
_preview_cache       = {}
_preview_in_progress = set()   # filenames currently being transcoded
_preview_lock        = threading.Lock()


def pregenerate_hevc_previews(folder, clips):
    """Background thread: pre-generate H.264 previews for all HEVC clips that
    don't already have one cached on disk."""
    hevc = [c for c in clips if c.get('codec', '') in ('hevc', 'h265')]
    if not hevc:
        return
    print(f'[preview] Starting pre-generation for {len(hevc)} HEVC clips...')
    for c in hevc:
        ensure_h264_preview(folder, c['filename'])
    print('[preview] All previews ready.')


def ensure_h264_preview(folder, filename):
    """Return path to H.264 preview of a HEVC file, transcoding if needed.
    Returns None on failure or if another thread is already transcoding this file."""
    with _preview_lock:
        if filename in _preview_cache:
            return _preview_cache[filename]
        if filename in _preview_in_progress:
            return None  # another thread is working on it
        _preview_in_progress.add(filename)

    outcome = None
    try:
        preview_dir  = os.path.join(folder, '.preview')
        preview_path = os.path.join(preview_dir, filename)

        if os.path.isfile(preview_path):
            outcome = preview_path
            return preview_path

        try:
            os.makedirs(preview_dir, exist_ok=True)
        except Exception:
            return None

        source = os.path.join(folder, filename)
        print(f'  Transcoding HEVC→H264: {filename}...', flush=True)
        cmd = [
            FFMPEG, '-y', '-i', source,
            *_video_enc_args(23),
            '-c:a', 'aac', '-b:a', '128k',
            preview_path,
        ]
        rc, _, err = run_cmd(cmd, timeout=300)
        if rc != 0:
            print(f'  WARN: Transcoding failed: {filename}')
            return None

        print(f'  H264 preview ready: {filename}')
        outcome = preview_path
        return preview_path
    finally:
        with _preview_lock:
            _preview_cache[filename] = outcome      # None on failure, path on success
            _preview_in_progress.discard(filename)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def run_cmd(cmd, timeout=60, cwd=None):
    kwargs = {'capture_output': True, 'timeout': timeout}
    if cwd:
        kwargs['cwd'] = cwd
    if IS_WIN:
        kwargs['creationflags'] = _NO_WINDOW
    r = subprocess.run(cmd, **kwargs)
    return r.returncode, r.stdout, r.stderr


def ffprobe_info(path):
    """Returns (duration, video_codec_name). video_codec_name may be None."""
    try:
        rc, out, _ = run_cmd(
            [FFPROBE, '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format', path],
            timeout=30,
        )
        if rc == 0:
            d = json.loads(out)
            codec = None
            duration = 0.0
            for s in d.get('streams', []):
                if s.get('codec_type') == 'video':
                    codec = s.get('codec_name', '').lower() or None
                    if 'duration' in s:
                        duration = float(s['duration'])
            if not duration:
                fmt_dur = d.get('format', {}).get('duration')
                if fmt_dur:
                    duration = float(fmt_dur)
            return duration, codec
    except Exception as e:
        print(f'  ffprobe error for {os.path.basename(path)}: {e}')
    return 0.0, None


def scan_folder(folder):
    """Return sorted list of clip dicts."""
    entries = []
    try:
        for name in os.listdir(folder):
            if name.lower().endswith('.mp4'):
                path = os.path.join(folder, name)
                mtime = os.path.getmtime(path)
                entries.append((mtime, name.lower(), name))
    except Exception as e:
        print(f'Scan error: {e}')
        return []

    entries.sort()  # chronological (mtime), then alpha
    clips = []
    n = len(entries)
    for i, (mtime, _, name) in enumerate(entries):
        path = os.path.join(folder, name)
        print(f'  [{i+1}/{n}] {name} ...', end='', flush=True)
        dur, codec = ffprobe_info(path)
        tag = f' [{codec}]' if codec else ''
        print(f' {dur:.1f}s{tag}')
        clips.append({
            'id': i,
            'filename': name,
            'duration': round(dur, 3),
            'codec': codec or '',
            'modified': datetime.datetime.fromtimestamp(mtime).isoformat(),
        })
    return clips


def scan_music(folder):
    """Return list of music track dicts from folder/music/*.mp3, sorted alphabetically."""
    music_dir = os.path.join(folder, 'music')
    if not os.path.isdir(music_dir):
        return []
    tracks = []
    try:
        names = sorted(n for n in os.listdir(music_dir) if n.lower().endswith('.mp3'))
    except Exception as e:
        print(f'Music scan error: {e}')
        return []
    for name in names:
        path = os.path.join(music_dir, name)
        dur, _ = ffprobe_info(path)
        tracks.append({'filename': name, 'duration': round(dur, 3)})
        print(f'  Music: {name} ({dur:.1f}s)')
    return tracks


def _clip_cache_path(folder, filename, start_time):
    """Return deterministic cache path for a cut clip segment.
    Includes source-file mtime in the name → automatic invalidation if source changes."""
    cache_dir = os.path.join(folder, '.clip_cache')
    stem      = os.path.splitext(filename)[0]
    src       = os.path.join(folder, filename)
    try:
        mtime_ms = int(os.path.getmtime(src) * 1000)
    except Exception:
        mtime_ms = 0
    start_ms = int(round(start_time * 1000))
    return os.path.join(cache_dir, f'{stem}_{mtime_ms}_{start_ms:09d}.mp4')


def _title_card_cache_path(folder, title, subtitle):
    """Return cache path for a title card keyed by title+subtitle content."""
    h = hashlib.md5(f'{title}|{subtitle}'.encode()).hexdigest()[:12]
    return os.path.join(folder, '.clip_cache', f'title_{h}.mp4')


def _end_card_cache_path(folder):
    """Return cache path for the end card (content never changes)."""
    return os.path.join(folder, '.clip_cache', 'end_card.mp4')


def _day_card_cache_path(folder, date_str):
    """Return cache path for a day separator card keyed by YYYY-MM-DD date."""
    return os.path.join(folder, '.clip_cache', f'day_{date_str}.mp4')


DAYS_PL = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def load_selections(folder):
    """Returns (selections_dict, title, subtitle)."""
    path = os.path.join(folder, 'selections.json')
    if os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if data.get('source_folder') == folder:
                sel = {s['filename']: s for s in data.get('selections', [])}
                return sel, data.get('title', ''), data.get('subtitle', '')
        except Exception as e:
            print(f'Load selections error: {e}')
    return {}, '', ''


def save_selections(folder, selections, title='', subtitle=''):
    path = os.path.join(folder, 'selections.json')
    data = {
        'source_folder': folder,
        'created': datetime.datetime.now().isoformat(),
        'clip_duration': 3.0,
        'title': title,
        'subtitle': subtitle,
        'selections': list(selections.values()),
    }
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f'Save selections error: {e}')

# ─── GPU / encoder helpers ────────────────────────────────────────────────────

def _video_enc_args(crf=18):
    """Return FFmpeg video-encoder argument list.

    Uses GPU encoder (NVENC / AMF / QSV) when one was detected at startup,
    otherwise falls back to libx264 (CPU).  The quality parameter maps to:
      libx264  → -crf  (lower = better quality)
      NVENC    → -cq   (lower = better quality, VBR mode)
      AMF      → -qp_i / -qp_p
      QSV      → -global_quality (ICQ mode)
    """
    tail = ['-pix_fmt', 'yuv420p', '-profile:v', 'high']
    if GPU_ENCODER == 'h264_nvenc':
        return ['-c:v', 'h264_nvenc', '-preset', 'p4',
                '-rc', 'vbr', '-cq', str(crf), '-b:v', '0'] + tail
    if GPU_ENCODER == 'h264_amf':
        return ['-c:v', 'h264_amf', '-quality', 'speed',
                '-qp_i', str(crf), '-qp_p', str(crf + 2)] + tail
    if GPU_ENCODER == 'h264_qsv':
        return ['-c:v', 'h264_qsv', '-preset', 'fast',
                '-global_quality', str(crf)] + tail
    # CPU fallback
    return ['-c:v', 'libx264', '-preset', 'fast', '-crf', str(crf)] + tail


def _detect_gpu_encoder():
    """Probe for a working hardware H.264 encoder and return its name, or None."""
    rc, out, _ = run_cmd([FFMPEG, '-encoders'], timeout=5)
    for enc in ('h264_nvenc', 'h264_amf', 'h264_qsv'):
        if enc.encode() not in out:
            continue
        # Verify the encoder actually works (GPU driver may be absent even if listed)
        rc, _, _ = run_cmd([
            FFMPEG, '-y',
            '-f', 'lavfi', '-i', 'color=black:s=64x64:r=1',
            '-t', '0.1', '-c:v', enc, '-f', 'null', '-',
        ], timeout=10)
        if rc == 0:
            return enc
    return None


# ─── Title/End card helpers ───────────────────────────────────────────────────

def esc_drawtext(s):
    """Escape special characters for FFmpeg drawtext filter (unquoted option value)."""
    return (s.replace('\\', '\\\\')
             .replace(':', '\\:')
             .replace("'", "\\'")
             .replace('%', '%%')
             .replace(',', '\\,'))


def _esc_font(path):
    """Escape font path for FFmpeg drawtext fontfile option (unquoted).
    Forward slashes are assumed. Escapes colon for Windows drive letter (C: → C\\:)."""
    return path.replace(':', '\\:')


def find_text_font():
    candidates = [
        'C:/Windows/Fonts/segoeui.ttf',
        'C:/Windows/Fonts/arial.ttf',
        'C:/Windows/Fonts/calibri.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/Library/Fonts/Arial.ttf',
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p.replace('\\', '/')
    return None


def find_icon_font():
    """Find font with PLAY icon. Segoe UI Symbol has U+23F5 ⏵; fallback uses ▶ from any font."""
    candidates = [
        'C:/Windows/Fonts/seguisym.ttf',   # Segoe UI Symbol – has ⏵
        'C:/Windows/Fonts/segmdl2.ttf',    # MDL2 Assets – has ⏵
        'C:/Windows/Fonts/segoeui.ttf',    # fallback – will use ▶
        'C:/Windows/Fonts/arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/Library/Fonts/Arial.ttf',
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p.replace('\\', '/')
    return None


def generate_title_card(title, subtitle, out_path):
    """Generate 4-second title card MP4 (1080x1920).
    Play icon visible 0-1s, title+subtitle visible 0-3s (static, no animation).
    Returns out_path on success, or None on failure."""
    text_font = find_text_font()
    if not text_font:
        print('WARN: No font found – intro card skipped')
        return None

    # All Windows fonts share C:/Windows/Fonts/, so one cwd works for all.
    font_dir  = os.path.dirname(text_font)
    text_name = os.path.basename(text_font)  # e.g. 'segoeui.ttf'

    # Separate icon font: Segoe UI Symbol has proper filled play icons.
    icon_font  = find_icon_font() or text_font
    icon_name  = os.path.basename(icon_font)
    name_lower = icon_name.lower()
    # Segoe UI Symbol / MDL2 have ⏵ (U+23F5); everything else uses ▶ (U+25B6)
    icon_char  = '\u23f5' if ('seguisym' in name_lower or 'segmdl2' in name_lower) else '\u25b6'

    title_esc = esc_drawtext(title)

    # \, inside enable= escapes the comma so it's not treated as a filter separator
    vf_parts = [
        f"drawtext=fontfile={icon_name}:text={icon_char}:fontsize=320:"
        f"fontcolor=white:x=(w-tw)/2:y=580:enable=lt(t\\,1)",
        f"drawtext=fontfile={text_name}:text={title_esc}:fontsize=90:"
        f"fontcolor=white:x=(w-tw)/2:y=1020:enable=lt(t\\,3)",
    ]
    if subtitle.strip():
        sub_esc = esc_drawtext(subtitle.strip())
        vf_parts.append(
            f"drawtext=fontfile={text_name}:text={sub_esc}:fontsize=60:"
            f"fontcolor=white:x=(w-tw)/2:y=1140:enable=lt(t\\,3)"
        )

    cmd = [
        FFMPEG, '-y',
        '-f', 'lavfi', '-i', 'color=c=black:s=1080x1920:r=30',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-t', '4',
        '-vf', ','.join(vf_parts),
        '-map', '0:v', '-map', '1:a',
        *_video_enc_args(18),
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
        out_path,
    ]
    rc, _, err = run_cmd(cmd, timeout=60, cwd=font_dir)
    if rc != 0:
        print('WARN: Error generating intro card:', err.decode('utf-8', errors='replace')[-600:])
        return None
    return out_path


def generate_end_card(out_path):
    """Generate 5-second 'The End' card MP4 (1080x1920).
    Returns out_path on success, or None on failure."""
    font = find_text_font()
    if not font:
        print('WARN: No font found – end card skipped')
        return None
    font_dir  = os.path.dirname(font)
    font_name = os.path.basename(font)
    vf = (f"drawtext=fontfile={font_name}:text=The End:fontsize=120:"
          f"fontcolor=white:x=(w-tw)/2:y=(h-th)/2")
    cmd = [
        FFMPEG, '-y',
        '-f', 'lavfi', '-i', 'color=c=black:s=1080x1920:r=30',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-t', '5',
        '-vf', vf,
        '-map', '0:v', '-map', '1:a',
        *_video_enc_args(18),
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
        out_path,
    ]
    rc, _, err = run_cmd(cmd, timeout=60, cwd=font_dir)
    if rc != 0:
        print('WARN: Error generating end card:', err.decode('utf-8', errors='replace')[-600:])
        return None
    return out_path

def generate_day_card(date_str, day_name, out_path):
    """Generate 2-second day separator card MP4 (1080x1920).
    Shows DD-MM-YYYY centered, day name below. Dark navy background.
    Returns out_path on success, or None on failure."""
    font = find_text_font()
    if not font:
        print('WARN: No font found – day card skipped')
        return None
    font_dir  = os.path.dirname(font)
    font_name = os.path.basename(font)

    # Convert YYYY-MM-DD → DD-MM-YYYY for display
    try:
        parts = date_str.split('-')
        display_date = f'{parts[2]}-{parts[1]}-{parts[0]}'
    except Exception:
        display_date = date_str

    date_esc = esc_drawtext(display_date)
    vf_parts = [
        f"drawtext=fontfile={font_name}:text={date_esc}:fontsize=96:"
        f"fontcolor=white:x=(w-tw)/2:y=(h-th)/2-50",
    ]
    if day_name:
        day_esc = esc_drawtext(day_name)
        vf_parts.append(
            f"drawtext=fontfile={font_name}:text={day_esc}:fontsize=60:"
            f"fontcolor=#aaaaaa:x=(w-tw)/2:y=(h-th)/2+70"
        )

    cmd = [
        FFMPEG, '-y',
        '-f', 'lavfi', '-i', 'color=c=#121220:s=1080x1920:r=30',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-t', '2',
        '-vf', ','.join(vf_parts),
        '-map', '0:v', '-map', '1:a',
        *_video_enc_args(18),
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
        out_path,
    ]
    rc, _, err = run_cmd(cmd, timeout=60, cwd=font_dir)
    if rc != 0:
        print('WARN: Error generating day card:', err.decode('utf-8', errors='replace')[-400:])
        return None
    return out_path


# ─── Music mixing ─────────────────────────────────────────────────────────────

def _add_music_to_video(folder, video_path, out_path, music_tracks):
    """Mix music_tracks (from folder/music/) with video_path, write to out_path.
    Returns out_path on success, None on failure."""
    if not music_tracks:
        return None

    music_dir = os.path.join(folder, 'music')
    video_dur, _ = ffprobe_info(video_path)
    if not video_dur:
        print('WARN: Cannot read video duration – skipping music')
        return None

    fade_dur   = min(10.0, video_dur)
    fade_start = max(0.0, video_dur - fade_dur)

    music_paths = [os.path.join(music_dir, t['filename']) for t in music_tracks]
    n = len(music_paths)

    inputs = [FFMPEG, '-y', '-i', video_path]
    for p in music_paths:
        inputs += ['-i', p]

    # Build filter_complex: concat N audio streams → trim → fade-out → amix with video
    if n == 1:
        prefix = f'[1:a]'
        parts  = []
    else:
        concat_ins = ''.join(f'[{i+1}:a]' for i in range(n))
        parts  = [f'{concat_ins}concat=n={n}:v=0:a=1[mus_raw]']
        prefix = '[mus_raw]'

    trim_filter = (f'{prefix}atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS,'
                   f'afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}[mus]')
    mix_filter  = '[0:a][mus]amix=inputs=2:duration=first:dropout_transition=0[audio_out]'

    parts.extend([trim_filter, mix_filter])
    full_filter = ';'.join(parts)

    cmd = inputs + [
        '-filter_complex', full_filter,
        '-map', '0:v',
        '-map', '[audio_out]',
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k',
        out_path,
    ]

    names = ', '.join(t['filename'] for t in music_tracks)
    print(f'  Mixing music ({n} track(s)): {names}  |  fade @{fade_start:.1f}s', flush=True)
    rc, _, err = run_cmd(cmd, timeout=600)
    if rc != 0:
        print('WARN: Music mixing error:', err.decode('utf-8', errors='replace')[-400:])
        return None
    return out_path


# ─── Export ───────────────────────────────────────────────────────────────────

def export_worker(folder, clips, selections, out_name, title='', subtitle='', music_tracks=None, include_day_cards=True):
    global export_status

    def set_status(s, msg, pct, output=None):
        with export_lock:
            export_status.update({'status': s, 'progress': msg, 'percent': pct})
            if output is not None:
                export_status['output'] = output

    set_status('working', 'Preparing...', 0)

    selected = [
        (c, selections[c['filename']])
        for c in clips
        if c['filename'] in selections and selections[c['filename']].get('enabled')
    ]

    if not selected:
        set_status('error', 'No clips selected', 0)
        return

    cache_dir = os.path.join(folder, '.clip_cache')
    temp_dir  = os.path.join(folder, 'temp')
    out_dir   = os.path.join(folder, 'output')
    try:
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(temp_dir,  exist_ok=True)
        os.makedirs(out_dir,   exist_ok=True)
    except Exception as e:
        set_status('error', f'Error creating directories: {e}', 0)
        return

    # Title card — check cache first
    title_file = None
    if title:
        title_cache = _title_card_cache_path(folder, title, subtitle)
        if os.path.isfile(title_cache):
            print('  [cache] intro card')
            title_file = title_cache
        else:
            set_status('working', 'Generating intro card...', 2)
            title_file = generate_title_card(title, subtitle, title_cache)

    cut_clips = []  # list of (date_str, cache_path)
    # Count total slots (primary + duplicates) for progress reporting
    total_slots = sum(1 + len(sel.get('extra_starts') or []) for _, sel in selected)
    slot_idx = 0

    for clip, sel in selected:
        inp      = os.path.join(folder, clip['filename'])
        # Group by day with 5:00 AM cutoff — clips before 5 AM belong to the previous day
        modified_str = clip.get('modified') or ''
        try:
            dt = datetime.datetime.fromisoformat(modified_str)
            if dt.hour < 5:
                dt -= datetime.timedelta(days=1)
            clip_day = dt.strftime('%Y-%m-%d')
        except Exception:
            clip_day = modified_str[:10]
        starts   = [sel.get('start_time') or 0.0] + [float(t) for t in (sel.get('extra_starts') or [])]

        for j, start in enumerate(starts):
            label      = clip['filename'] + (f' [{j+1}/{len(starts)}]' if len(starts) > 1 else '')
            cache_path = _clip_cache_path(folder, clip['filename'], start)

            if os.path.isfile(cache_path):
                print(f'  [cache] {label}')
                cut_clips.append((clip_day, cache_path))
                slot_idx += 1
                continue

            set_status('working', f'Cutting {slot_idx+1}/{total_slots}: {label}', int(slot_idx / total_slots * 70))
            slot_idx += 1

            if FFMPEG_HAS_ZSCALE:
                # Full HDR→SDR tone-mapping pipeline (requires libzimg)
                color_vf = ('zscale=t=linear:npl=100,format=gbrpf32le,'
                            'zscale=p=bt709,tonemap=tonemap=hable:desat=0,'
                            'zscale=t=bt709:m=bt709:r=tv,format=yuv420p')
            else:
                # Fallback: colorspace matrix conversion (no tone-mapping)
                color_vf = 'colorspace=space=bt709:trc=bt709:primaries=bt709:range=mpeg'
            vf = (f'{color_vf},'
                  'scale=1080:1920:force_original_aspect_ratio=decrease,'
                  'pad=1080:1920:-1:-1:color=black')
            cmd = [
                FFMPEG, '-y',
                '-ss', str(start),
                '-i', inp,
                '-t', '3',
                *_video_enc_args(18),
                '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', '1',
                '-c:a', 'aac', '-b:a', '192k', '-ar', '44100', '-ac', '2',
                '-vf', vf,
                '-af', 'afade=t=out:st=2.7:d=0.3',
                '-r', '30',
                cache_path,
            ]
            rc, _, err = run_cmd(cmd, timeout=120)
            if rc != 0:
                msg = err.decode('utf-8', errors='replace')[-400:]
                set_status('error', f'Cutting error {label}: {msg}', 0)
                return
            cut_clips.append((clip_day, cache_path))

    # End card — check cache first
    end_cache = _end_card_cache_path(folder)
    if os.path.isfile(end_cache):
        print('  [cache] end card')
        end_file = end_cache
    else:
        set_status('working', 'Generating end card...', 72)
        end_file = generate_end_card(end_cache)

    # Day separator cards — one per unique day in chronological order
    day_files = {}  # date_str -> path or None
    if include_day_cards:
        for (day, _) in cut_clips:
            if day and day not in day_files:
                day_cache = _day_card_cache_path(folder, day)
                if os.path.isfile(day_cache):
                    print(f'  [cache] day card {day}')
                    day_files[day] = day_cache
                else:
                    set_status('working', f'Generating day card {day}...', 78)
                    try:
                        dt_obj   = datetime.datetime.strptime(day, '%Y-%m-%d')
                        day_name = DAYS_PL[dt_obj.weekday()]
                    except Exception:
                        day_name = ''
                    day_files[day] = generate_day_card(day, day_name, day_cache)

    # Assemble final file list: [title_card?] + [day_card + clips_of_day...]* + [end_card?]
    # First day card is skipped when a title card precedes it (clips flow directly after title).
    concat_list = []
    if title_file:
        concat_list.append(title_file)
    last_day = None
    is_first_day = True
    for (day, cache_path) in cut_clips:
        if day != last_day:
            last_day = day
            if include_day_cards and day_files.get(day) and not (is_first_day and title_file):
                concat_list.append(day_files[day])
            is_first_day = False
        concat_list.append(cache_path)
    if end_file:
        concat_list.append(end_file)

    # Write filelist.txt (FFmpeg concat demuxer)
    flist = os.path.join(temp_dir, 'filelist.txt')
    try:
        with open(flist, 'w', encoding='utf-8') as f:
            for p in concat_list:
                p_unix = os.path.abspath(p).replace('\\', '/')
                f.write(f"file '{p_unix}'\n")
    except Exception as e:
        set_status('error', f'Filelist error: {e}', 0)
        return

    has_music   = bool(music_tracks)
    out_path    = os.path.join(out_dir, out_name)
    concat_path = os.path.join(temp_dir, 'concat_out.mp4') if has_music else out_path

    set_status('working', 'Merging clips...', 86)
    cmd = [FFMPEG, '-y', '-f', 'concat', '-safe', '0', '-i', flist, '-c', 'copy', concat_path]
    rc, _, err = run_cmd(cmd, timeout=300)
    if rc != 0:
        msg = err.decode('utf-8', errors='replace')[-400:]
        set_status('error', f'Merging error: {msg}', 0)
        return

    if has_music:
        set_status('working', 'Adding music...', 92)
        result = _add_music_to_video(folder, concat_path, out_path, music_tracks)
        if result:
            try:
                os.remove(concat_path)
            except Exception:
                pass
        else:
            # Fallback: use concat result without music
            try:
                os.replace(concat_path, out_path)
            except Exception:
                pass
            print('WARN: Music was not added – film saved without music')

    # Cleanup — only temp dir (clips live in .clip_cache/, not in temp)
    set_status('working', 'Cleanup...', 95)
    try:
        os.remove(flist)
        os.rmdir(temp_dir)
    except Exception:
        pass

    set_status('done', f'Done! Saved: output/{out_name}', 100, output=out_name)
    print(f'Export done: output/{out_name}')

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    # Paths that don't require a valid session
    _OPEN_PATHS = frozenset({
        ('GET',  '/'),
        ('GET',  '/index.html'),
        ('POST', '/api/session'),
    })

    def log_message(self, fmt, *args):
        pass  # silence access log

    def handle_error(self, request, client_address):
        # Silence noisy connection-reset/abort errors from the browser
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def _session_ok(self, method, path):
        """Return True if the request has the current session token, or if
        no session has been registered yet, or if the path is always open."""
        if (method, path) in self._OPEN_PATHS:
            return True
        # Video/output files are fetched by the browser's native <video> element
        # which cannot add custom headers – allow these without session check.
        if method == 'GET' and (path.startswith('/api/video/') or path.startswith('/api/output/')):
            return True
        with _session_lock:
            if _session['id'] is None:
                return True  # no session registered – allow all
            return self.headers.get('X-Session-Id') == _session['id']

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(n)
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if not self._session_ok('GET', path):
            self.send_json({'error': 'session_expired'}, 409)
            return

        if path in ('/', '/index.html'):
            self._serve_static('index.html')
        elif path == '/api/state':
            with state_lock:
                self.send_json({
                    'loaded': state['folder'] is not None,
                    'folder': state['folder'],
                    'count': len(state['clips']),
                    'title': state['title'],
                    'subtitle': state['subtitle'],
                })
        elif path == '/api/clips':
            with state_lock:
                self.send_json({'clips': state['clips']})
        elif path == '/api/selections':
            with state_lock:
                self.send_json({'selections': state['selections']})
        elif path == '/api/preview_status':
            with state_lock:
                clips = list(state['clips'])
            with _preview_lock:
                cache    = dict(_preview_cache)
                in_prog  = set(_preview_in_progress)
            status = {}
            for c in clips:
                fname = c['filename']
                codec = c.get('codec', '')
                if codec not in ('hevc', 'h265'):
                    status[fname] = 'ready'
                elif fname in cache:
                    status[fname] = 'ready' if cache[fname] else 'error'
                elif fname in in_prog:
                    status[fname] = 'working'
                else:
                    status[fname] = 'pending'
            all_ready = all(v in ('ready', 'error') for v in status.values())
            self.send_json({'status': status, 'all_ready': all_ready})
        elif path == '/api/music':
            with state_lock:
                self.send_json({'tracks': state['music']})
        elif path == '/api/export/status':
            with export_lock:
                self.send_json(dict(export_status))
        elif path.startswith('/api/video/'):
            filename = unquote(path[len('/api/video/'):])
            self._serve_video(filename)
        elif path.startswith('/api/output/'):
            filename = unquote(path[len('/api/output/'):])
            self._serve_output(filename)
        else:
            self.send_error(404, 'Not Found')

    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_json()

        if path == '/api/session':
            new_id = str(uuid.uuid4())
            with _session_lock:
                _session['id'] = new_id
            print(f'New session: {new_id[:8]}...')
            self.send_json({'session_id': new_id})
            return

        if not self._session_ok('POST', path):
            self.send_json({'error': 'session_expired'}, 409)
            return

        if path == '/api/folder':
            folder = data.get('folder', '').strip()
            if not os.path.isdir(folder):
                self.send_json({'error': f'Folder does not exist: {folder}'}, 400)
                return
            print(f'Scanning: {folder}')
            clips = scan_folder(folder)
            if not clips:
                self.send_json({'error': 'No MP4 files found in folder'}, 400)
                return
            sel, title, subtitle = load_selections(folder)
            music = scan_music(folder)
            with state_lock:
                state['folder']    = folder
                state['clips']     = clips
                state['selections'] = sel
                state['title']     = title
                state['subtitle']  = subtitle
                state['music']     = music
            print(f'Loaded {len(clips)} clips, {len(sel)} saved decisions, {len(music)} tracks')
            threading.Thread(target=pregenerate_hevc_previews, args=(folder, clips), daemon=True).start()
            self.send_json({'ok': True, 'count': len(clips)})

        elif path == '/api/select':
            fname = data.get('filename')
            if not fname:
                self.send_json({'error': 'Missing filename'}, 400)
                return
            with state_lock:
                folder   = state['folder']
                title    = state['title']
                subtitle = state['subtitle']
                state['selections'][fname] = {
                    'filename': fname,
                    'start_time': data.get('start_time'),
                    'enabled': bool(data.get('enabled', True)),
                    'extra_starts': [float(t) for t in (data.get('extra_starts') or [])],
                }
                sel_copy = dict(state['selections'])
            if folder:
                save_selections(folder, sel_copy, title, subtitle)
            self.send_json({'ok': True})

        elif path == '/api/settings':
            title    = data.get('title', '').strip()
            subtitle = data.get('subtitle', '').strip()
            with state_lock:
                state['title']    = title
                state['subtitle'] = subtitle
                folder   = state['folder']
                sel_copy = dict(state['selections'])
            if folder:
                save_selections(folder, sel_copy, title, subtitle)
            self.send_json({'ok': True})

        elif path == '/api/export':
            with state_lock:
                if not state['folder']:
                    self.send_json({'error': 'No folder loaded'}, 400)
                    return
                folder = state['folder']
                clips  = list(state['clips'])
                sel    = dict(state['selections'])
                music  = list(state['music'])

            with export_lock:
                if export_status['status'] == 'working':
                    self.send_json({'error': 'Export already in progress'}, 400)
                    return

            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            out_name = data.get('output_filename') or f'final_{ts}.mp4'
            out_name = re.sub(r'[^\w\-_. ]', '_', out_name).strip('_') or f'final_{ts}.mp4'
            if not out_name.lower().endswith('.mp4'):
                out_name += '.mp4'

            title            = data.get('title', '').strip()
            subtitle         = data.get('subtitle', '').strip()
            include_day_cards = bool(data.get('include_day_cards', True))

            t = threading.Thread(
                target=export_worker,
                args=(folder, clips, sel, out_name, title, subtitle, music, include_day_cards),
                daemon=True,
            )
            t.start()
            self.send_json({'ok': True, 'output': out_name})

        elif path == '/api/clear-cache':
            with state_lock:
                folder = state['folder']
            if not folder:
                self.send_json({'error': 'No folder loaded'}, 400)
                return
            with export_lock:
                if export_status['status'] == 'working':
                    self.send_json({'error': 'Export in progress — cannot clear cache now'}, 400)
                    return
            cache_dir = os.path.join(folder, '.clip_cache')
            deleted = 0
            if os.path.isdir(cache_dir):
                for f in os.listdir(cache_dir):
                    try:
                        os.remove(os.path.join(cache_dir, f))
                        deleted += 1
                    except Exception:
                        pass
            print(f'Cache cleared: {deleted} files deleted')
            self.send_json({'ok': True, 'deleted': deleted})

        else:
            self.send_json({'error': 'Not found'}, 404)

    def _serve_static(self, name):
        fpath = os.path.join(SCRIPT_DIR, name)
        if not os.path.isfile(fpath):
            self.send_error(404, 'File not found')
            return
        with open(fpath, 'rb') as f:
            body = f.read()
        ext = name.rsplit('.', 1)[-1].lower()
        ct = {
            'html': 'text/html; charset=utf-8',
            'js': 'application/javascript',
            'css': 'text/css',
        }.get(ext, 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_video(self, filename):
        with state_lock:
            folder = state['folder']
            clips  = state['clips']
        if not folder:
            self.send_error(404, 'No folder loaded')
            return

        # Prevent path traversal
        filename = os.path.basename(filename)
        fpath = os.path.join(folder, filename)
        if not os.path.isfile(fpath):
            self.send_error(404, 'Video not found')
            return

        # If HEVC/H.265 – transcode to H.264 for browser compatibility
        codec = next((c.get('codec', '') for c in clips if c['filename'] == filename), '')
        if not codec:
            # Codec not in clip list (e.g. fresh scan without codec field) – detect now
            _, codec = ffprobe_info(fpath)
            codec = codec or ''
        if codec in ('hevc', 'h265'):
            preview = ensure_h264_preview(folder, filename)
            if preview:
                fpath = preview

        size = os.path.getsize(fpath)
        rng = self.headers.get('Range')

        if rng:
            m = re.match(r'bytes=(\d+)-(\d*)', rng)
            if not m:
                self.send_response(416)
                self.end_headers()
                return
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Content-Length', length)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            try:
                with open(fpath, 'rb') as f:
                    f.seek(start)
                    left = length
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except Exception:
                pass
        else:
            self.send_response(200)
            self.send_header('Content-Length', size)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            try:
                with open(fpath, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except Exception:
                pass

    def _serve_output(self, filename):
        """Serve a generated output file (MP4) from the output/ subfolder."""
        with state_lock:
            folder = state['folder']
        if not folder:
            self.send_error(404, 'No folder loaded')
            return
        filename = os.path.basename(filename)
        fpath = os.path.join(folder, 'output', filename)
        if not os.path.isfile(fpath):
            self.send_error(404, 'File not found')
            return
        size = os.path.getsize(fpath)
        rng  = self.headers.get('Range')
        if rng:
            m = re.match(r'bytes=(\d+)-(\d*)', rng)
            if not m:
                self.send_response(416); self.end_headers(); return
            start  = int(m.group(1))
            end    = int(m.group(2)) if m.group(2) else size - 1
            end    = min(end, size - 1)
            if start > end:
                self.send_response(416); self.end_headers(); return
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range',  f'bytes {start}-{end}/{size}')
            self.send_header('Content-Length', length)
            self.send_header('Content-Type',   'video/mp4')
            self.send_header('Accept-Ranges',  'bytes')
            self.end_headers()
            try:
                with open(fpath, 'rb') as f:
                    f.seek(start)
                    left = length
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk: break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except Exception:
                pass
        else:
            self.send_response(200)
            self.send_header('Content-Length', size)
            self.send_header('Content-Type',   'video/mp4')
            self.send_header('Accept-Ranges',  'bytes')
            self.end_headers()
            try:
                with open(fpath, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
            except Exception:
                pass

# ─── Entry point ──────────────────────────────────────────────────────────────

FFMPEG_HAS_ZSCALE = False  # set by check_ffmpeg()
GPU_ENCODER = None          # set by check_ffmpeg(); 'h264_nvenc' | 'h264_amf' | 'h264_qsv' | None

def check_ffmpeg():
    global FFMPEG_HAS_ZSCALE, GPU_ENCODER
    for tool, path in (('ffmpeg', FFMPEG), ('ffprobe', FFPROBE)):
        try:
            rc, _, _ = run_cmd([path, '-version'], timeout=5)
            src = 'local' if os.path.isabs(path) else 'PATH'
            print(f'{tool}: OK ({src})')
        except FileNotFoundError:
            print(f'ERROR: {tool} not found!')
            print(f'  Place next to server.py: {tool}.exe')
            print(f'  or add to PATH. Download: https://ffmpeg.org/download.html')
            sys.exit(1)
    # Detect zscale (libzimg) for HDR→SDR tone mapping
    rc, out, _ = run_cmd([FFMPEG, '-filters'], timeout=5)
    FFMPEG_HAS_ZSCALE = b'zscale' in out
    print(f'HDR tone-mapping (zscale): {"yes" if FFMPEG_HAS_ZSCALE else "no (colorspace fallback)"}')
    # Detect GPU encoder (NVENC / AMF / QSV)
    GPU_ENCODER = _detect_gpu_encoder()
    label = GPU_ENCODER if GPU_ENCODER else 'libx264 (CPU)'
    print(f'Video encoder: {label}')


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    check_ffmpeg()
    print('FFmpeg: OK')

    if len(sys.argv) > 1:
        folder = os.path.abspath(sys.argv[1])
        if not os.path.isdir(folder):
            print(f'ERROR: Folder does not exist: {folder}')
            sys.exit(1)
        print(f'Scanning: {folder}')
        clips = scan_folder(folder)
        if not clips:
            print(f'ERROR: No MP4 files in: {folder}')
            sys.exit(1)
        sel, title, subtitle = load_selections(folder)
        music = scan_music(folder)
        with state_lock:
            state['folder']    = folder
            state['clips']     = clips
            state['selections'] = sel
            state['title']     = title
            state['subtitle']  = subtitle
            state['music']     = music
        print(f'Loaded {len(clips)} clips, {len(music)} tracks')
        threading.Thread(target=pregenerate_hevc_previews, args=(folder, clips), daemon=True).start()

    server = ThreadedHTTPServer(('localhost', PORT), Handler)
    url = f'http://localhost:{PORT}'
    print(f'Server started: {url}')
    print('Ctrl+C to stop.')

    def _open():
        import time
        time.sleep(0.6)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
