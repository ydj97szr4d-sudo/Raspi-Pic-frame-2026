"""Hands the whole screen to VLC for one clip, instead of decoding it ourselves.

``video.py``'s ``VideoPlayer`` decodes into a texture the renderer composites
like a photograph -- crossfades, Ken Burns and overlays all keep working over
video, but every frame costs a Python-side copy out of GStreamer plus a GL
upload, and on this board that pair is the ceiling: confirmed directly, a
clip that decoded at 30+fps standing alone measured only a handful of fps
once it had to go through that path live (see video.py's module docstring
for the colorimetry fix that *did* help, and the session notes for what this
did not turn out to be -- a competing thread pool, a background overlay
redraw, or the display's own refresh rate).

This module is the other option (``slideshow.video_engine: vlc``): spawn
``cvlc`` with its own direct-to-DRM output (``--vout=drm_vout``) and let it
paint the screen itself, with no Python frame-pulling in the way at all.
VLC's DRM output needs to be the exclusive DRM master to do that, which this
process normally is (see ``gfx/backend.py``'s ``release_display()`` /
``reacquire_display()`` for the handoff) -- confirmed directly: launched
while picframe3 still held the display, VLC's drm_vout failed outright
("Failed to get xlease", "Failed to set atomic cap").

The cost of this path is real and is paid up front, not discovered later:
nothing else can be drawn to the screen for as long as VLC owns it, so there
is no crossfade into or out of the clip, no clock or weather overlay, no Ken
Burns -- the picture simply becomes the video, starkly, until it ends.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

_log = logging.getLogger(__name__)

#: Resolved once and reused -- ``shutil.which`` is a handful of stat() calls,
#: cheap but pointless to repeat before every single clip.
_VLC_BIN: str | None = None
_VLC_RESOLVED = False


def available() -> bool:
    """Whether a VLC binary with the DRM output module can be found at all.

    Only checks that *something* is installed under one of the usual names --
    not that ``drm_vout`` specifically is compiled in, which is cheaper to
    discover from the one real attempt in ``start()`` than from parsing
    ``vlc --list`` ahead of every clip.
    """
    return _resolve() is not None


def _resolve() -> str | None:
    global _VLC_BIN, _VLC_RESOLVED
    if not _VLC_RESOLVED:
        _VLC_RESOLVED = True
        _VLC_BIN = shutil.which("cvlc") or shutil.which("vlc")
        if _VLC_BIN is None:
            _log.info("neither cvlc nor vlc is on PATH; video_engine: vlc cannot run")
    return _VLC_BIN


class VlcTakeover:
    """One VLC subprocess, playing one clip directly to the display.

    Non-blocking throughout: ``start()`` returns as soon as the process is
    launched, and ``poll()`` -- called from the render loop, not awaited --
    says only whether it is still going.  There is no frame to pull and
    nothing to upload; the whole point is that VLC is painting the screen
    itself.
    """

    def __init__(self, *, mute: bool = False, loop: bool = False,
                 max_seconds: float = 0.0):
        self.mute = mute
        self.loop = loop
        self.max_seconds = float(max_seconds)
        self._proc: subprocess.Popen | None = None
        self._path: str | None = None

    def start(self, path: str) -> bool:
        self.stop()
        vlc = _resolve()
        if vlc is None:
            return False
        args = [
            vlc,
            "--vout=drm_vout",
            "--no-osd",
            "--quiet",              # VLC's own logging, not this process's
            "--no-video-title-show",
            "--play-and-exit",      # exit instead of sitting at an empty playlist
        ]
        if self.mute:
            args.append("--no-audio")
        if self.max_seconds > 0:
            # ``--loop`` repeats the whole file rather than just this window,
            # so the two are not combined here: a looping clip with a cutoff
            # gets the cutoff (the window the picture is allotted matters
            # more than one extra repeat of a short clip), a non-looping one
            # gets both play-and-exit and the cutoff.
            args.append(f"--stop-time={self.max_seconds:g}")
        elif self.loop:
            args.append("--loop")
        args.append(path)
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Its own process group: stop() can signal it (and anything
                # it spawned, e.g. a helper decoder) without touching
                # picframe3's own process group or whatever sent the request.
                start_new_session=True,
            )
        except OSError as exc:
            _log.warning("could not start VLC for %s: %s", path, exc)
            self._proc = None
            return False
        self._path = path
        _log.info("handed the display to VLC for %s (pid %d)",
                   os.path.basename(path), self._proc.pid)
        return True

    def poll(self) -> bool:
        """True while VLC is still running this clip."""
        if self._proc is None:
            return False
        if self._proc.poll() is None:
            return True
        code = self._proc.returncode
        if code not in (0, None):
            _log.info("VLC exited %s playing %s", code, os.path.basename(self._path or ""))
        self._proc = None
        return False

    def stop(self) -> None:
        """Ends the clip early -- skip/next, or the frame shutting down."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - wedged driver
                _log.warning("VLC (pid %d) did not exit after SIGKILL", proc.pid)

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
