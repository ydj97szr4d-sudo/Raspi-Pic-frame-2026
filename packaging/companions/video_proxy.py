#!/usr/bin/env python3
"""Background watcher: keeps every video in the PicFrame3 library within a
1080p box, because a Raspberry Pi 4 cannot move enough memory bandwidth to
play anything bigger smoothly -- not a codec problem, a bandwidth ceiling
(see PicFrame3's own picframe3/media/video.py:_hw_decode_note, which
documents this exact limit: "on a Pi 4 the ceiling for video is 1080p
whatever the codec").

How it works
------------
- Reads library.picture_folders AND its own settings (video_proxy.*) straight
  out of PicFrame3's own config.yaml, every scan -- so it always watches the
  same folder(s) the frame does, and picks up a setting changed on the web
  Settings page without needing a restart.
- Periodically scans those folders for video files (extension-based).
- For each one, compares its longer/shorter side against the configured box.
  A file that already fits is left completely alone -- re-checked only if its
  size/mtime change, via a small on-disk state cache, so a huge library
  doesn't get re-probed with ffprobe every cycle.
- A file that's too big is "settled" first (its size/mtime must be stable
  across two checks a few seconds apart) so a file still being written by
  Syncthing is never read mid-copy.
- Transcoded with ffmpeg: scaled down to fit an orientation-aware box, keeping
  aspect ratio, using the Pi's own hardware H.264 encoder (h264_v4l2m2m) when
  available so this doesn't compete for CPU with the frame itself; falls back
  to software libx264 if the hardware encoder isn't there, or if it's there
  but fails at runtime (e.g. the encode node is busy) -- that fallback is
  retried per file, so one hiccup doesn't skip a file outright.
- Written to a temp file next to the original, then swapped in with
  os.replace() -- an atomic rename, not an in-place write. That matters:
  it only redirects this one directory entry to a new file; it never
  mutates the underlying inode's bytes. So if this path is a hardlink
  shared with a Syncthing-synced original elsewhere, that original's data
  is completely unaffected -- only the frame-side copy changes to point at
  the new, smaller file. (Verified directly: transcoding a hardlinked copy
  leaves the other hardlink's inode and pixel dimensions untouched.)
- Only the first video and first audio stream are kept (GoPro/Insta360
  files often carry an extra GPMF telemetry stream ffmpeg can't copy, which
  would otherwise abort the whole transcode).

Settings (config.yaml, section "video_proxy" -- also on the web Settings
page under "Video proxy")
------------------------------------------------------------------------
- enabled:          off leaves every video exactly as it is (default: true)
- long_side_max:    longer edge of the fit box, px (default: 1920)
- short_side_max:   shorter edge of the fit box, px (default: 1080)
- video_bitrate:    ffmpeg target video bitrate (default: "8M")
- audio_bitrate:    ffmpeg target audio bitrate (default: "160k")
- scan_interval:    seconds between library sweeps (default: 300)
All of the above are read fresh at the start of every scan, so editing them
on the Settings page takes effect on the next sweep -- no restart needed.

Deploy notes
------------
- Requires ffmpeg/ffprobe on the Pi (installed by setup_video_proxy.sh).
- No extra Python packages needed -- stdlib + subprocess only (PyYAML is
  already installed in this venv for dht_mqtt.py's config loading).
- Run under systemd (service: video-proxy) with Restart=always.
"""
import datetime
import json
import os
import shutil
import subprocess
import time

CONFIG_PATH = os.path.expanduser("~/.config/picframe3/config.yaml")
STATE_FILE = os.path.expanduser("~/.local/share/picframe3/video_proxy_state.json")

SETTLE_WAIT = 10             # seconds between the two stability checks
SETTLE_RETRIES = 6           # how many times to re-check before giving up

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".insv", ".3gp", ".webm"}

# Fallbacks used only if config.yaml has no video_proxy section at all (e.g.
# an older config, or the file missing outright). Normally overridden every
# scan by DEFAULT_SETTINGS merged with whatever's in config.yaml -- see
# load_settings().
DEFAULT_SETTINGS = {
    "enabled": True,
    "long_side_max": 1920,
    "short_side_max": 1080,
    "video_bitrate": "8M",
    "audio_bitrate": "160k",
    "scan_interval": 300,
}

_hw_encoder_checked = None  # cached True/False, filled in on first use


def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Config / library folders
# --------------------------------------------------------------------------

def load_config():
    try:
        import yaml
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError as e:
        log(f"could not read {CONFIG_PATH}: {e}")
        return {}


def library_folders(cfg):
    folders = (cfg.get("library") or {}).get("picture_folders") or []
    return [os.path.expanduser(p) for p in folders]


def load_settings(cfg):
    """video_proxy.* from config.yaml, merged over the built-in defaults so
    a config that only sets some of them still works."""
    settings = dict(DEFAULT_SETTINGS)
    settings.update((cfg.get("video_proxy") or {}))
    return settings


# --------------------------------------------------------------------------
# State cache -- avoids re-probing unchanged files every scan
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def file_fingerprint(path):
    st = os.stat(path)
    return {"mtime": st.st_mtime, "size": st.st_size}


# --------------------------------------------------------------------------
# ffprobe / ffmpeg
# --------------------------------------------------------------------------

def probe_dimensions(path):
    """Returns (width, height) of the first real video stream, or None.

    Uses JSON output and filters explicitly for codec_type == "video" with
    attached_pic cleared, rather than relying on "-select_streams v:0" and
    a one-line text format: some files here (certain iPhone/GoPro .MOV
    exports) carry more than one stream ffprobe still matches under v:0 --
    e.g. a small embedded cover-art/thumbnail image stream -- which made a
    plain "WxH" text parse blow up with more than one line of output. JSON
    plus an explicit codec_type/attached_pic filter can't be confused by
    that: it always picks the real video track's own dimensions."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type,width,height:stream_disposition=attached_pic",
             "-of", "json", path],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout
        data = json.loads(out)
        for s in data.get("streams", []):
            if s.get("codec_type") != "video":
                continue
            if (s.get("disposition") or {}).get("attached_pic"):
                continue  # cover art / thumbnail, not the real video
            if "width" in s and "height" in s:
                return int(s["width"]), int(s["height"])
        log(f"ffprobe found no real video stream in {os.path.basename(path)}")
        return None
    except Exception as e:
        log(f"ffprobe failed on {os.path.basename(path)}: {e}")
        return None


def needs_transcode(width, height, settings):
    long_side = max(width, height)
    short_side = min(width, height)
    return (long_side > settings["long_side_max"]
            or short_side > settings["short_side_max"])


def hw_encoder_available():
    global _hw_encoder_checked
    if _hw_encoder_checked is None:
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                check=True, capture_output=True, text=True, timeout=15,
            ).stdout
            _hw_encoder_checked = "h264_v4l2m2m" in out
        except Exception as e:
            log(f"could not check for hardware encoder (assuming none): {e}")
            _hw_encoder_checked = False
        log(f"hardware H.264 encoder {'available' if _hw_encoder_checked else 'NOT available -- falling back to software x264'}")
    return _hw_encoder_checked


def transcode(path, width, height, settings):
    """Scales `path` down to fit the configured box (orientation-aware) and
    atomically replaces it. Returns True on success."""
    long_max = settings["long_side_max"]
    short_max = settings["short_side_max"]
    video_bitrate = settings["video_bitrate"]
    audio_bitrate = settings["audio_bitrate"]

    if width >= height:
        box_w, box_h = long_max, short_max
    else:
        box_w, box_h = short_max, long_max

    scale_filter = (
        f"scale={box_w}:{box_h}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2"
    )

    root, ext = os.path.splitext(path)
    tmp_path = f"{root}.proxytmp{ext}"

    def build_cmd(use_hw):
        if use_hw:
            # Barely touches the CPU (the VPU does the work), so no thread
            # cap needed here.
            video_args = ["-c:v", "h264_v4l2m2m", "-b:v", video_bitrate,
                          "-maxrate", video_bitrate, "-bufsize", "16M"]
        else:
            # Software x264 on a Pi 4 will happily grab all 4 cores, which
            # starves whatever the frame itself is doing at that moment --
            # compositing, and above all decoding/playing a video, which
            # needs the CPU too even with hardware decode. Capped at 2
            # threads so there's always headroom left for playback, which
            # matters more than how fast this background job finishes.
            video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                          "-threads", "2"]
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-map_metadata", "0",
            "-vf", scale_filter,
            *video_args,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", audio_bitrate, "-ac", "2",
            "-movflags", "+faststart",
            tmp_path,
        ]
        # Lowest CPU/IO scheduling priority this OS offers, so this
        # background job never competes on equal footing with the frame
        # app for the CPU or the disk -- it only gets what's left over.
        # Falls back to running un-niced if `nice`/`ionice` aren't on PATH
        # (neither is ever absent on a stock Raspberry Pi OS, but a missing
        # tool here should degrade, not break transcoding outright).
        prefix = []
        if shutil.which("nice"):
            prefix += ["nice", "-n", "19"]
        if shutil.which("ionice"):
            prefix += ["ionice", "-c3"]
        return prefix + cmd

    def run_ffmpeg(use_hw):
        cmd = build_cmd(use_hw)
        log(f"transcoding {os.path.basename(path)} ({width}x{height} -> fit {box_w}x{box_h}, "
            f"{'hardware' if use_hw else 'software'} encoder)")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
            return True
        except subprocess.CalledProcessError as e:
            log(f"ffmpeg ({'hardware' if use_hw else 'software'} encoder) failed on "
                f"{os.path.basename(path)}: {e.stderr[-2000:] if e.stderr else e}")
            return False
        except subprocess.TimeoutExpired:
            log(f"ffmpeg timed out on {os.path.basename(path)}")
            return False

    global _hw_encoder_checked
    use_hw = hw_encoder_available()
    # Always actually try -- `run_ffmpeg(use_hw) if use_hw else False` used
    # to skip calling run_ffmpeg entirely when use_hw was False, silently
    # failing the whole file (never falling back to software) instead of
    # just using software outright. Once hardware fails even once at
    # runtime (see below, which also sets _hw_encoder_checked = False for
    # the rest of this process's life), every later file hit that dead
    # branch too -- so one transient hardware hiccup quietly broke proxy
    # transcoding for the rest of the library until the service restarted.
    ok = run_ffmpeg(use_hw)

    if use_hw and not ok:
        # The hardware encode node may be busy, missing, or otherwise not
        # actually usable even though ffmpeg was compiled with support for
        # it -- fall back to software rather than losing this file, and
        # stop trying hardware for the rest of this run so we don't pay
        # the failed-attempt cost on every subsequent file too.
        log("hardware encoder did not work at runtime -- falling back to "
            "software (libx264) for this and future files this run")
        _hw_encoder_checked = False
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        ok = run_ffmpeg(False)

    if not ok:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False

    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        log(f"ffmpeg produced no output for {os.path.basename(path)}")
        return False

    # Atomic swap -- see the module docstring for why this is safe with a
    # hardlinked original: this only redirects the directory entry, it
    # never writes into the original inode.
    os.replace(tmp_path, path)
    log(f"replaced {os.path.basename(path)} with a {box_w}x{box_h}-fit proxy")
    return True


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def find_videos(folders):
    for folder in folders:
        for dirpath, _dirnames, filenames in os.walk(folder):
            for name in filenames:
                if ".proxytmp" in name:
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in VIDEO_EXTS:
                    yield os.path.join(dirpath, name)


def settle(path):
    """Waits until a file's size/mtime stop changing, so a file still being
    copied in by Syncthing is never read mid-write. Returns False if it
    never settles within the retry budget (file probably still arriving;
    it will be picked up again next scan)."""
    try:
        last = file_fingerprint(path)
    except OSError:
        return False
    for _ in range(SETTLE_RETRIES):
        time.sleep(SETTLE_WAIT)
        try:
            current = file_fingerprint(path)
        except OSError:
            return False
        if current == last:
            return True
        last = current
    log(f"{os.path.basename(path)} still changing after {SETTLE_RETRIES * SETTLE_WAIT}s -- will retry next scan")
    return False


def scan_once(state):
    cfg = load_config()
    settings = load_settings(cfg)

    if not settings["enabled"]:
        log("video_proxy.enabled is false -- not touching any video this scan")
        return state

    folders = library_folders(cfg)
    if not folders:
        log("no library.picture_folders found in config.yaml -- nothing to watch")
        return state

    seen_this_scan = set()
    for path in find_videos(folders):
        seen_this_scan.add(path)
        try:
            fp = file_fingerprint(path)
        except OSError:
            continue

        cached = state.get(path)
        # Only a confirmed-good outcome ("ok" or "transcoded") is safe to
        # treat as settled and skip on an unchanged file. "probe_failed" and
        # "transcode_failed" are error states, not decisions -- caching
        # those as final meant a file that failed once (e.g. because of a
        # bug that's since been fixed) would be silently skipped forever,
        # since its mtime/size never changes. Those are retried every scan
        # until they actually succeed. A file whose *settled* good outcome
        # was decided under different settings (a different box size) is
        # also re-checked -- see the "settings" stamp below.
        if (cached and cached.get("status") in ("ok", "transcoded")
                and cached.get("mtime") == fp["mtime"] and cached.get("size") == fp["size"]
                and cached.get("box") == (settings["long_side_max"], settings["short_side_max"])):
            continue  # unchanged since we last decided about it, same box size

        if not settle(path):
            continue  # still being written; try again next scan

        fp = file_fingerprint(path)  # refresh after settling

        dims = probe_dimensions(path)
        if dims is None:
            state[path] = {**fp, "status": "probe_failed"}
            continue
        width, height = dims

        box = (settings["long_side_max"], settings["short_side_max"])
        if needs_transcode(width, height, settings):
            ok = transcode(path, width, height, settings)
            if ok:
                state[path] = {**file_fingerprint(path), "status": "transcoded", "box": box}
            else:
                state[path] = {**fp, "status": "transcode_failed", "box": box}
        else:
            state[path] = {**fp, "status": "ok", "box": box}

        save_state(state)  # save progress as we go, not just at the end

    # Drop entries for files that no longer exist, so the cache doesn't
    # grow forever as your library is reorganised.
    for stale in set(state) - seen_this_scan:
        del state[stale]

    return state


def main():
    log("video-proxy watcher starting")
    state = load_state()
    while True:
        state = scan_once(state)
        save_state(state)
        # Read fresh so a changed scan_interval takes effect on the very
        # next sleep, not only after a restart.
        interval = load_settings(load_config())["scan_interval"]
        time.sleep(interval)


if __name__ == "__main__":
    main()
