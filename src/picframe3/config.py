"""Configuration: typed dataclasses loaded from one YAML file.

Design notes:

* Every setting has a default, so a missing or half-written config file still
  produces a frame that runs.  ``picframe`` required a complete
  ``configuration.yaml`` and raised ``KeyError`` deep inside the viewer when a
  key was absent -- a bad failure mode for an appliance.
* Unknown keys are reported as warnings, not errors: a config written for a
  newer version still boots.
* Settings are addressable by dotted path (``slideshow.interval``) so MQTT and
  the web UI can read and write them through one code path, with validation.
"""

from __future__ import annotations

import copy
import functools
import logging
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, get_args, get_origin

_log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = (
    "~/.config/picframe3/config.yaml",
    "/etc/picframe3/config.yaml",
)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

@dataclass
class DisplayConfig:
    backend: str = "auto"                 # auto | kms | headless
    device: str | None = None          # /dev/dri/card1; None = autodetect
    connector: str | None = None       # HDMI-A-1; None = first connected
    #: Which mode to set, as ``1920x1080`` or ``3840x2160@30``.  Empty takes
    #: the connector's preferred mode, which is the right answer for almost
    #: every panel.  It is not the right answer when the preferred mode is one
    #: the Pi cannot actually drive -- a 4K television asks for 2160p60, which
    #: needs ``hdmi_enable_4kp60`` on a Pi 4 and is beyond the Pi 4's pixel
    #: clock without it -- or when a panel offers several and lies about which
    #: it likes.  A mode the connector does not list is refused with the list
    #: of the ones it does, rather than rendering somewhere nobody can see.
    mode: str = ""
    #: 0 or 180.  A quarter turn is done by the kernel instead
    #: (``video=HDMI-A-1:1080x1920M@60,rotate=90`` in cmdline.txt) so the Pi
    #: reports a portrait mode and every layer works in real pixels.
    rotate: int = 0
    vsync: bool = True
    #: Only applies while something is animating; a still picture draws nothing.
    fps_limit: float = 60.0
    background: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])
    brightness: float = 1.0


@dataclass
class SlideshowConfig:
    interval: float = 180.0               # seconds a picture stays up
    transition: str = "fade"              # or "random"
    transition_time: float = 2.5
    order: str = "shuffle"
    recent_days: int = 7
    reshuffle_after: int = 1
    portrait_pairs: bool = False
    shuffle: bool = True                  # kept for compatibility with picframe
    paused: bool = False
    video_loop: bool = False
    video_mute: bool = True
    video_max_seconds: float = 0.0        # 0 = play to the end
    # "gstreamer": decode into the renderer's own texture -- crossfades,
    # Ken Burns and overlays keep working over video, but every frame is a
    # Python-side buffer copy plus a GL upload, which on this board is the
    # ceiling (see video.py's module docstring). "vlc": hand the whole
    # screen to VLC's own direct-to-DRM output for the clip's duration --
    # smooth regardless of resolution, but nothing else can draw to the
    # screen while it plays: no crossfade in or out, no clock/weather
    # overlay, no Ken Burns. The frame's own renderer keeps its EGL context
    # and every texture the whole time (only DRM master changes hands), so
    # nothing needs reloading when VLC exits and control comes back.
    video_engine: str = "gstreamer"       # or "vlc"
    kenburns: bool = False
    kenburns_zoom: float = 1.12
    #: Which transitions ``transition: random`` may draw from.  Empty means the
    #: standard pool; name a few to keep only the ones you like.
    transition_choices: list[str] = field(default_factory=list)


@dataclass
class ViewerConfig:
    fit: str = "auto"                     # auto | cover | contain | blur | mat
    #: Detect faces (not whose -- just where) so a "cover" crop keeps the
    #: whole face, with hair, on screen instead of cropping to a fixed point
    #: that happens to cut through a forehead on a tall portrait. Needs
    #: OpenCV (the picframe3[faces] extra); harmlessly off without it. Runs
    #: once per photograph, in the background, the same way reverse-geocoding
    #: does -- not on every slide.
    detect_faces: bool = False
    #: What ``fit: auto`` may do with a picture whose shape does not match the
    #: panel.  One name treats every such picture the same; several means "pick
    #: one of these", chosen from the file path so a given photograph always
    #: looks the same.  A picture that already matches the panel is shown edge
    #: to edge whatever is listed here, because that crops nothing.
    fit_choices: list[str] = field(default_factory=lambda: ["mat"])
    blur_amount: float = 20.0
    blur_zoom: float = 1.06
    blur_dim: float = 0.55
    #: Never enlarge a small picture by more than this; beyond it the frame
    #: falls back to a blur-fill rather than showing a soft, stretched image.
    upscale_limit: float = 2.5
    #: Largest picture the frame will decode, in megapixels.  A JPEG or a HEIF
    #: is scaled down as it is decoded and almost never reaches this; it is the
    #: formats that cannot -- PNG, TIFF, BMP -- where a single huge scan would
    #: otherwise take the whole frame down with it.  0 turns the limit off.
    max_decode_megapixels: float = 64.0
    #: What happens to a picture past that limit: on, the frame opens it once
    #: in a child process and keeps a panel-sized copy, so the photograph is
    #: shown from then on and the original is never touched again.  Off leaves
    #: it out of the slideshow instead.
    shrink_oversized: bool = True
    mat_style: str = "single"             # or "random", or "" for all
    mat_tolerance: float = 0.01
    mat_outer_color: list[int] | None = None
    mat_inner_color: list[int] | None = None
    mat_outer_border: int = 75
    mat_inner_border: int = 22
    mat_texture: bool = True
    #: The inner mat is usually smooth card against the textured outer board.
    mat_inner_texture: bool = False
    #: False makes the inner mat a darker shade of the outer instead of its
    #: own colour taken from the photograph.
    mat_auto_inner_color: bool = True
    mat_bevel_width: int = 5

    font: str | None = None
    #: A library font name (media.fontlib.CHOICES) for the caption alone,
    #: instead of font above. "" (default) inherits font.
    text_font_family: str = ""
    show_text: list[str] = field(
        default_factory=lambda: ["title", "caption", "date", "location"]
    )
    text_size: int = 34
    #: Red, green, blue, 0-255. Caption text was always drawn at a hardcoded
    #: white before this existed; white stays the default.
    text_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    text_seconds: float = 16.0
    #: How long the caption stays up when it is *asked* for -- the Home
    #: Assistant button, the ``i`` key -- rather than the few seconds it gets
    #: by itself after each change.  A deliberate look wants longer than a
    #: glance, and picframe had one number for both: making the reveal last
    #: meant every slide's caption lasting too.  Setting ``text_seconds: 0``
    #: and leaving this long is the "captions only when I ask" frame.
    peek_seconds: float = 40.0
    text_justify: str = "L"
    text_opacity: float = 1.0
    text_margin_x: int = 64
    text_margin_y: int = 36
    #: Lift a portrait pair's caption band this many pixels clear of the very
    #: bottom edge of the screen. A pair's two captions always draw as one
    #: full-width band at the bottom regardless of text_position (see
    #: gfx.overlays.info_bar's docstring for why), which otherwise lands
    #: right on top of anything else anchored to the bottom of the screen --
    #: a weather or status overlay from a companion script, say. 0 (the
    #: original behaviour) leaves it flush with the bottom edge, same as a
    #: single photo's caption.
    pair_text_bottom_margin: int = 0
    text_scrim: float = 0.5
    #: A rounded translucent panel behind the caption text, sized to the text
    #: itself rather than the full width (unlike text_scrim's gradient band
    #: above) -- the same "widget on its own backdrop" look the calendar/
    #: clock/status overlay widgets use, for a caption that also needs to
    #: read over a busy photograph. 0 (default) draws no panel at all, so a
    #: frame that never touches this looks exactly as it did -- text_scrim
    #: alone (or nothing) behind the text. The two can be used together.
    text_panel_opacity: float = 0.0
    text_panel_color: list[int] = field(default_factory=lambda: [0, 0, 0])
    #: Red, green, blue, 0-255. A stroked outline around the caption panel
    #: above. Only matters when text_panel_opacity > 0.
    text_panel_border_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: Outline thickness in pixels. 0 (default) draws no outline.
    text_panel_border_width: int = 0
    #: 0 (invisible) - 1 (fully opaque). Only matters when
    #: text_panel_border_width > 0.
    text_panel_border_opacity: float = 0.9
    #: Gap, in pixels, between the caption text and the panel's left/right
    #: edge. Set separately from the vertical gap because a caption is
    #: usually wide and short, so the same pixel margin on every side reads
    #: as barely-there sideways and oversized top and bottom.
    text_panel_padding_x: int = 28
    text_panel_padding_y: int = 16
    #: Stretches the panel to at least this percent of the screen's width
    #: when the text itself is narrower -- the text stays aligned inside per
    #: text_justify, it is only the panel's own backdrop that widens. 0
    #: (default) leaves the panel hugging the text, as before this existed.
    #: Only matters when text_panel_opacity > 0.
    text_panel_min_width_pct: float = 0.0
    #: "" (default) keeps the caption exactly where it has always been: a
    #: full-width band anchored to the bottom of the screen. Set to one of
    #: TL/TC/TR/BL/BC/BR (the same scheme viewer.clock_position uses) to
    #: place it in a corner instead -- only for a single caption, not a
    #: portrait pair's two side-by-side ones, which always keep the
    #: full-width bottom band regardless of this setting.
    text_position: str = ""
    text_offset_pct: list[float] = field(default_factory=lambda: [3.0, 3.0])
    #: Written between the caption elements.  " · " reads as one line of
    #: information; "\n" puts each element on its own line.
    text_separator: str = "  ·  "
    date_format: str = "%-d %B %Y"
    #: Which language month and day names come out in: ``de_DE.UTF-8``,
    #: ``fr_FR.UTF-8``, ``en_GB.UTF-8``.  Empty leaves the process locale
    #: alone.  This is picframe's ``model.locale``.
    locale: str = ""

    show_clock: bool = False
    clock_format: str = "%H:%M"
    clock_size: int = 120
    clock_position: str = "TR"            # TL TC TR BL BC BR
    clock_opacity: float = 0.9
    clock_offset_pct: list[float] = field(default_factory=lambda: [3.0, 3.0])
    #: If this file exists its contents are shown under the time -- the same
    #: hook picframe had at /dev/shm/clock.txt, for a weather line or similar.
    clock_extra_file: str = "/dev/shm/picframe-clock.txt"

    overlay_image: str = "/dev/shm/picframe-overlay.png"

    #: Shown when there is nothing to show.  Empty is the picture that ships
    #: with the frame; a path is your own; "none" draws a plain screen with the
    #: folders it looked in.  picframe called this model.no_files_img.
    no_files_img: str = ""


@dataclass
class LibraryConfig:
    picture_folders: list[str] = field(default_factory=lambda: ["~/Pictures"])
    database: str = "~/.local/share/picframe3/library.db3"
    follow_links: bool = False
    include_videos: bool = True
    ignore_hidden: bool = True
    exclude: list[str] = field(default_factory=lambda: ["@eaDir", ".thumbnails", "#recycle"])
    watch: bool = True                    # inotify
    rescan_interval: float = 3600.0       # belt and braces behind inotify
    scan_on_start: bool = True
    deleted_folder: str = "~/.local/share/picframe3/deleted"
    #: Where a photo's original bytes go the moment the library grid's
    #: editor rotates or crops it -- one copy per file, made just before the
    #: first edit, so "Undo edit" always has something to put back. The
    #: *second* edit to the same file without an undo in between does not
    #: make a second backup: there is nowhere further back to go than the
    #: original, and keeping every intermediate step would turn a stack of
    #: ordinary crops into a slow leak of disk space.
    edit_backup_folder: str = "~/.local/share/picframe3/edit-originals"
    subfolder: str = ""                   # live filter, changeable at runtime
    #: The largest share of the index a single scan may delete.  A picture
    #: folder on a USB stick or a network share is simply *absent* for a
    #: moment now and then, and every file under it then looks deleted; without
    #: this guard one unlucky scan throws away the play history, the hidden
    #: flags and every geocoded place name.  0 switches the guard off.
    prune_max_fraction: float = 0.2


@dataclass
class SyncConfig:
    """Syncthing: photographs that arrive by themselves.

    The frame owns very little of this.  Which folder is kept in step, who it
    is paired with and where Syncthing's own page listens are all settings
    Syncthing keeps; these are the handful the frame has an opinion about, so
    that a fresh install can be set up without ever opening Syncthing itself.
    """

    #: Whether this frame runs Syncthing at all.  Switching it on installs the
    #: package if it is missing and starts it with the Pi; switching it off
    #: stops it and leaves everything -- folder, pairings, photographs -- in
    #: place.
    enabled: bool = False
    #: The folder Syncthing keeps in step.  Empty means the first picture
    #: folder, which is what anyone actually wants.
    folder_path: str = ""
    #: Its name on the other machines, and the internal id the two sides agree
    #: on.  The id is awkward to change afterwards, so it has a dull default.
    folder_label: str = "Picture Frame"
    folder_id: str = "picframe3-pictures"
    #: ``sendreceive`` is two-way: photographs arrive, and anything the frame
    #: does to them travels back.  ``receiveonly`` takes photographs and never
    #: sends a change of its own.
    folder_type: str = "sendreceive"
    #: Days Syncthing keeps a copy of anything deleted or overwritten in that
    #: folder.  0 switches the trash can off.
    versioning_days: int = 30
    #: Where Syncthing's own web interface listens.  It binds to localhost out
    #: of the box, which on a frame with no browser means nobody can open it.
    gui_lan: bool = True
    gui_port: int = 8384


@dataclass
class GeoConfig:
    enabled: bool = False
    contact: str = ""                     # required by the Nominatim usage policy
    language: str = "en"
    cache: str = "~/.local/share/picframe3/geocache.db3"
    #: How much of the address to write. A preset name, or "custom" to use
    #: the key_order list below verbatim.
    detail: str = "full"
    #: Names never to show — your own country, say, or a county nobody uses.
    suppress: list[str] = field(default_factory=list)
    key_order: list[list[str]] = field(default_factory=lambda: [
        ["tourism", "attraction", "amenity", "isolated_dwelling"],
        ["neighbourhood", "suburb", "village", "town"],
        ["city", "municipality", "county"],
        ["state", "province", "region"],
        ["country"],
    ])


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls_ca: str = ""
    tls_insecure: bool = False
    device_id: str = "picframe"
    device_name: str = "Picture Frame"
    discovery_prefix: str = "homeassistant"
    topic_prefix: str = "picframe"
    publish_interval: float = 30.0
    publish_image: bool = True
    image_width: int = 1280
    image_quality: int = 82


@dataclass
class AppearanceConfig:
    """How the web interface itself looks -- the settings page, the library
    grid, the control panel -- not the frame's own screen (that's `viewer`).
    Defaults to Arial and a blue accent rather than the page's own built-in
    look; blank either field (or the whole page's own look, with both
    blank) to go back to it. Either way, a change here is live -- no
    restart needed, since it's just a couple of CSS variables the page
    itself applies.
    """

    #: A CSS font-family value, e.g. "Arial, sans-serif". Empty keeps the
    #: page's own built-in typeface.
    font: str = "Arial, sans-serif"
    #: Red, green, blue, 0-255. Replaces the accent colour used for links,
    #: buttons and highlights throughout the page; the hover, ink-on-accent
    #: and tint shades the page needs alongside it are worked out from this
    #: one colour. Empty keeps the page's own built-in accent.
    accent_color: list[int] = field(default_factory=lambda: [37, 99, 235])


@dataclass
class HttpConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9000
    auth_user: str = ""
    auth_password: str = ""
    #: Whether the Remove button may work from the web interface and from
    #: MQTT.  On by default, because removing a picture from the frame is one
    #: of the things the frame is for; turning it off leaves removal to the
    #: keyboard and the GPIO buttons, which are in the room.
    allow_delete: bool = True
    cors_origins: list[str] = field(default_factory=list)
    #: Extra names the frame will answer to, beyond loopback, the private
    #: address ranges and its own hostname.  Only needed behind a reverse
    #: proxy or on a router with an unusual local domain.
    allowed_hosts: list[str] = field(default_factory=list)
    #: PEM certificate and private key for serving this page over https://
    #: instead of http://.  Both empty (the default) means plain HTTP;
    #: `picframe3 enable-https` writes a self-signed pair and fills these
    #: in, or point them at your own certificate (e.g. one a reverse proxy
    #: already has) by hand. Leaving only one set is treated as neither
    #: being set -- a half-configured pair would otherwise fail at startup
    #: with a stack trace instead of a clear message.
    tls_cert: str = ""
    tls_key: str = ""


@dataclass
class InputConfig:
    keyboard: bool = True
    touch: bool = True
    mouse: bool = False
    gpio_buttons: dict = field(default_factory=dict)   # {"next": 17, "pause": 27}
    gpio_pull_up: bool = True
    wake_on_input: bool = True
    keymap: dict = field(default_factory=lambda: {
        "next": ["KEY_RIGHT", "KEY_SPACE", "KEY_DOWN"],
        "previous": ["KEY_LEFT", "KEY_UP"],
        "pause": ["KEY_P"],
        "display_toggle": ["KEY_O"],
        "info_toggle": ["KEY_I"],
        "clock_toggle": ["KEY_C"],
        "delete": ["KEY_DELETE"],
        "quit": ["KEY_ESC", "KEY_Q"],
    })


@dataclass
class PowerConfig:
    #: Blank the screen on this weekly schedule.  "22:30-07:00" style ranges,
    #: per weekday name or "all".
    schedule: dict = field(default_factory=dict)
    dim_schedule: dict = field(default_factory=dict)   # {"21:00-23:00": 0.4}
    #: Named ``enabled`` rather than ``on`` on purpose: YAML 1.1 reads a bare
    #: ``on:`` key as the boolean True, which silently loses the setting.
    enabled: bool = True


@dataclass
class HealthConfig:
    """What the frame reports about the Pi it is running on."""

    #: Off removes the diagnostic sensors from Home Assistant and the line from
    #: the web interface; nothing is measured either.
    enabled: bool = True
    #: Seconds between readings.  Taken on a background thread, never in the
    #: render loop.
    interval: float = 30.0
    #: Which filesystem the free-space reading is about.  Empty means the first
    #: picture folder, which is the disk that actually fills up.
    disk_path: str = ""


@dataclass
class NetworkConfig:
    """Watching the frame's own link to the house, and mending it."""

    #: Off means nothing is pinged and nothing is ever restarted.
    enabled: bool = True
    #: What has to answer for the network to count as up.  Empty means the
    #: default gateway, read fresh from the routing table on every check --
    #: which is the right target in any house and needs no configuration.
    #: Never make this an address on the internet: a frame that restarts its
    #: Wi-Fi because a far-away server dropped a packet is worse than no
    #: watchdog at all.
    target: str = ""
    #: Which interface to reconnect.  Empty means the one the default route
    #: goes through.
    interface: str = ""
    #: Seconds between checks.
    interval: float = 60.0
    #: Ping runs, each of several packets, before one check counts as failed.
    attempts: int = 3
    #: Seconds to wait for a reply.
    timeout: float = 3.0
    #: Failed checks in a row before anything is repaired.  Three, an interval
    #: apart, is several minutes of genuine silence -- not a lost packet.
    failures: int = 3
    #: False watches and reports but never touches the connection.
    repair: bool = True
    #: Never repair more often than this.  The single most important setting
    #: here: without it a repair that makes the next check fail becomes a loop.
    cooldown: float = 1800.0
    #: Seconds to let the link settle after a repair before checking again.
    settle: float = 45.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = ""
    journald: bool = True


@dataclass
class MotionConfig:
    """The PIR-driven screen sleep/wake companion (``motion_display.py``).

    This is not part of the main frame process -- it is a separate service
    that blanks the screen after a period with no motion and wakes it again
    the moment the sensor fires.  It polls this file on its own schedule, so
    a change here reaches it without a restart.
    """

    #: Seconds with no detected motion before the screen is put to sleep.
    idle_timeout: float = 900.0


@dataclass
class VideoProxyConfig:
    """The background watcher that keeps videos inside what a Pi 4 can play
    smoothly (``video_proxy.py``).

    A Raspberry Pi 4's memory bandwidth, not its codec, is what limits video
    to roughly 1080p -- see the frame's own notes on this in ``media/video.py``.
    This watcher transcodes anything larger down to fit, in place, using the
    Pi's hardware encoder when it is available and falling back to software
    when it is not.
    """

    #: Off leaves every video exactly as it is; proxies already made are kept.
    enabled: bool = True
    #: The longer edge of the box a video must fit inside, in pixels.
    long_side_max: int = 1920
    #: The shorter edge of that same box -- together these describe one
    #: 1920x1080-equivalent box, whichever way a clip is oriented.
    short_side_max: int = 1080
    #: Target video bitrate passed straight to ffmpeg, e.g. ``8M``.
    video_bitrate: str = "8M"
    #: Target audio bitrate passed straight to ffmpeg, e.g. ``160k``.
    audio_bitrate: str = "160k"
    #: Seconds between sweeps of the picture library looking for new or
    #: oversized videos.
    scan_interval: float = 300.0


@dataclass
class OverlayConfig:
    """How the weather-caption idle-countdown dot grid looks
    (drawn by ``dht_mqtt.py`` into the frame's own ``viewer.overlay_image``).

    The grid always drains in step with :class:`MotionConfig`'s
    ``idle_timeout`` -- these settings only change how it is drawn, not how
    long it takes to empty.
    """

    #: Dots in the countdown grid. Arranged as a square, so this should be a
    #: perfect square (4, 9, 16, 25...); 16 draws a 4x4 grid.
    dot_count: int = 16
    #: Gap, in pixels, between the dot grid and the weather caption text it
    #: sits beside.
    gap_to_text: int = 14
    #: Opacity of a dot still representing time left, 0-255.
    filled_alpha: int = 235
    #: Opacity of a dot already counted down, 0-255.
    unfilled_alpha: int = 60
    #: Red, green, blue, 0-255. The panel drawn behind the weather caption
    #: and its dot grid together, so they stay legible over a busy
    #: photograph -- the same look the calendar/clock/status widgets have.
    background_color: list[int] = field(default_factory=lambda: [0, 0, 0])
    #: 0 (no panel at all -- the original drop-shadowed text, no box) - 1
    #: (solid panel). Matches the other widgets' default.
    background_opacity: float = 0.35
    #: Gap, in pixels, between the caption text/dot grid and the panel's
    #: own edge. Only matters when background_opacity > 0.
    background_padding: int = 20
    #: A library font name (media.fontlib.CHOICES) for this widget alone,
    #: instead of the frame-wide viewer.font. "" (default) inherits it.
    font_family: str = ""
    #: Red, green, blue, 0-255. A stroked outline around the panel, the
    #: same rounded rectangle its fill uses.
    border_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: Outline thickness in pixels. 0 (default) draws no outline.
    border_width: int = 0
    #: 0 (invisible) - 1 (fully opaque). Only matters when border_width > 0.
    border_opacity: float = 0.9
    #: Left/centre/right justification of the caption+dot-grid block within
    #: its own panel. Only visible once min_width_pct stretches the panel
    #: beyond the content's own width, and only matters when
    #: background_opacity > 0 -- there is no panel to align within in the
    #: original drop-shadowed look.
    align: str = "L"                      # L | C | R
    #: Stretches the panel to at least this percent of the screen's width
    #: when the content is narrower. 0 (default) leaves the panel hugging
    #: the content, as before this existed. Only matters when
    #: background_opacity > 0.
    min_width_pct: float = 0.0


@dataclass
class CalendarConfig:
    """The upper-left calendar widget drawn by ``dht_mqtt.py`` onto the same
    overlay image as the weather caption and idle-countdown dots.

    Today's date, and a small month grid with today highlighted, below it.
    Font tracks ``viewer.font`` the same way the weather caption does, so a
    frame-wide font change reaches this too. A later version is meant to
    pull in holidays from an online calendar; nothing here does that yet.
    """

    #: Off removes the widget entirely; nothing is drawn or measured.
    enabled: bool = True
    #: Inset from the top-left corner, percent of screen width/height --
    #: [x, y]. Mirrors viewer.clock_offset_pct, which insets from the
    #: bottom-right for the weather caption.
    offset_pct: list[float] = field(default_factory=lambda: [3.0, 3.0])
    #: Size of the "today" line ("Saturday, 17 October"), in real screen
    #: pixels (this widget is drawn by dht_mqtt.py directly at your screen's
    #: own resolution, not through PicFrame3's own scaled renderer).
    size: int = 34
    #: Size of the month grid's own day numbers and weekday-header row (M T
    #: W T F S S), in real screen pixels -- independent of `size` above, so
    #: the two can be tuned separately.
    grid_size: int = 20
    #: Extra vertical space, in pixels, between every row of the grid --
    #: between the weekday-header row (M T W T F S S) and the first week,
    #: and between each week after that. 0 packs the rows at their natural
    #: (font-driven) height, with no added gap.
    grid_row_gap: int = 0
    #: How many pixels smaller the "today" highlight square is than its
    #: grid cell, per side -- 0 fills the cell edge to edge, a larger
    #: number leaves a visible gap around the mark instead of it touching
    #: the cell's own boundary.
    square_padding: int = 1
    #: Red, green, blue, 0-255. The date text, weekday headers and day
    #: numbers.
    text_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: 0 (invisible) - 1 (fully opaque).
    text_opacity: float = 0.9
    #: Red, green, blue, 0-255. The panel drawn behind the whole widget, so
    #: the text stays legible over a busy photograph.
    background_color: list[int] = field(default_factory=lambda: [0, 0, 0])
    #: 0 (no panel at all -- text floats directly over the picture, the
    #: original look) - 1 (solid panel).
    background_opacity: float = 0.35
    #: Colour of Sundays, holiday dates in the grid, and the "D: Festival
    #: Name" lines below it -- one of HOLIDAY_PALETTE's names (dht_mqtt.py).
    #: A plain dropdown rather than a 3-number RGB field, so it reads as a
    #: palette swatch pick rather than a colour-mixing exercise.
    holiday_color: str = "dull_red"
    #: A library font name (media.fontlib.CHOICES) for this widget alone,
    #: instead of the frame-wide viewer.font. "" (default) inherits it.
    font_family: str = ""
    #: Red, green, blue, 0-255. A stroked outline around the whole widget's
    #: panel, the same rounded rectangle its fill uses.
    border_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: Outline thickness in pixels. 0 (default) draws no outline.
    border_width: int = 0
    #: 0 (invisible) - 1 (fully opaque). Only matters when border_width > 0.
    border_opacity: float = 0.9
    #: Thickness, in pixels, of a line separating the "today" line from the
    #: month grid below it. 0 (default) draws none.
    divider_width: int = 0
    #: Red, green, blue, 0-255.
    divider_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: 0 (invisible) - 1 (fully opaque). Only matters when divider_width > 0.
    divider_opacity: float = 0.5
    #: Where a day number sits across its cell, left to right.
    grid_align_h: str = "center"              # left | center | right
    #: Where a day number sits within its cell, top to bottom.
    grid_align_v: str = "center"              # top | center | bottom
    #: Red, green, blue, 0-255. The "today" highlight square's own fill --
    #: independent of text_color, which used to double as this.
    today_fill_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: 0 (invisible) - 1 (fully opaque).
    today_fill_opacity: float = 0.92
    #: Red, green, blue, 0-255. A stroked outline around the "today" square
    #: alone, separate from the whole widget's own border above.
    today_border_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: Outline thickness in pixels. 0 (default) draws no outline.
    today_border_width: int = 0
    #: 0 (invisible) - 1 (fully opaque). Only matters when
    #: today_border_width > 0.
    today_border_opacity: float = 0.9
    #: Left/centre/right justification of the "D: Festival Name" lines
    #: below the grid, within the widget's own full width.
    holiday_align: str = "L"              # L | C | R


@dataclass
class ClockConfig:
    """A custom-drawn clock, also on the same overlay image dht_mqtt.py
    already owns, with a translucent panel behind it like the calendar
    widget -- PicFrame3's own built-in clock (viewer.show_clock) is
    rendered by the core app, not this overlay, so it can't get a panel;
    this is a separate, from-scratch replacement for it. Turn off
    viewer.show_clock if you enable this, so the two don't double up.
    """

    #: Off removes the widget entirely; nothing is drawn or measured.
    enabled: bool = False
    #: Which corner it's inset from. Same six positions as
    #: viewer.clock_position: TL TC TR BL BC BR.
    position: str = "TR"
    #: Inset from that corner, percent of screen width/height -- [x, y].
    offset_pct: list[float] = field(default_factory=lambda: [3.0, 3.0])
    #: Size of the time text, in real screen pixels.
    size: int = 64
    #: 24-hour "14:07" when on, 12-hour "2:07 PM" when off.
    format_24h: bool = True
    #: Red, green, blue, 0-255.
    text_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: 0 (invisible) - 1 (fully opaque).
    text_opacity: float = 0.9
    #: Red, green, blue, 0-255. The panel drawn behind the time text, so it
    #: stays legible over a busy photograph.
    background_color: list[int] = field(default_factory=lambda: [0, 0, 0])
    #: 0 (no panel at all) - 1 (solid panel).
    background_opacity: float = 0.35
    #: Gap, in pixels, between the time text and the panel's left/right
    #: edge, and between it and the top/bottom edge. Only matters when
    #: background_opacity > 0. Split in two because the clock's text is
    #: wide but only one line tall, so an equal pixel margin on every side
    #: reads as barely-there sideways and oversized top and bottom.
    panel_padding_x: int = 40
    panel_padding_y: int = 16
    #: A library font name (media.fontlib.CHOICES) for this widget alone,
    #: instead of the frame-wide viewer.font. "" (default) inherits it.
    font_family: str = ""
    #: Red, green, blue, 0-255. A stroked outline around the panel, the
    #: same rounded rectangle its fill uses.
    border_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: Outline thickness in pixels. 0 (default) draws no outline.
    border_width: int = 0
    #: 0 (invisible) - 1 (fully opaque). Only matters when border_width > 0.
    border_opacity: float = 0.9
    #: Left/centre/right justification of the time text within its own
    #: panel. Only visible once min_width_pct stretches the panel beyond
    #: the text's own width -- at min_width_pct 0 the panel always hugs
    #: the text exactly and this has nothing to align within.
    align: str = "L"                      # L | C | R
    #: Stretches the panel to at least this percent of the screen's width
    #: when the time text is narrower. 0 (default) leaves the panel
    #: hugging the text, as before this existed.
    min_width_pct: float = 0.0


@dataclass
class StatusConfig:
    """A small horizontal status strip on the same overlay image, also with
    a translucent panel. Today just the outdoor Pico W's link state (is
    dht_mqtt.py still hearing from it, or has it gone quiet and the frame
    fallen back to OpenWeather); more lines -- a Time Machine backup state
    from a reporter running on the Mac, say -- are meant to join it later.
    """

    #: Off removes the strip entirely; nothing is drawn or measured.
    enabled: bool = True
    #: Which corner it's inset from: TL TC TR BL BC BR.
    position: str = "TR"
    #: Inset from that corner, percent of screen width/height -- [x, y].
    offset_pct: list[float] = field(default_factory=lambda: [3.0, 3.0])
    #: Size of the status text, in real screen pixels.
    size: int = 26
    #: Red, green, blue, 0-255.
    text_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: 0 (invisible) - 1 (fully opaque).
    text_opacity: float = 0.9
    #: Red, green, blue, 0-255. The panel drawn behind the strip.
    background_color: list[int] = field(default_factory=lambda: [0, 0, 0])
    #: 0 (no panel at all) - 1 (solid panel).
    background_opacity: float = 0.35
    #: A library font name (media.fontlib.CHOICES) for this widget alone,
    #: instead of the frame-wide viewer.font. "" (default) inherits it.
    font_family: str = ""
    #: Red, green, blue, 0-255. A stroked outline around the panel, the
    #: same rounded rectangle its fill uses.
    border_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    #: Outline thickness in pixels. 0 (default) draws no outline.
    border_width: int = 0
    #: 0 (invisible) - 1 (fully opaque). Only matters when border_width > 0.
    border_opacity: float = 0.9
    #: Left/centre/right justification of the strip's rows within its own
    #: panel. Only visible once min_width_pct stretches the panel beyond
    #: the rows' own width.
    align: str = "L"                      # L | C | R
    #: Stretches the panel to at least this percent of the screen's width
    #: when the strip is narrower. 0 (default) leaves the panel hugging
    #: the content, as before this existed.
    min_width_pct: float = 0.0


@dataclass
class BrandConfig:
    """The small animated mark in a corner of the screen -- the frame's own
    maker's mark, not a photo caption or status readout. Drawn transparent
    apart from the mark itself and a thin outline around it, so it sits on
    top of whatever photo is showing rather than behind a panel like the
    clock or status strip.
    """

    #: Off removes the mark entirely; nothing is drawn or animated.
    enabled: bool = True
    #: Which corner it's inset from: TL TC TR BL BC BR.
    position: str = "BR"
    #: Inset from that corner, percent of screen width/height -- [x, y].
    offset_pct: list[float] = field(default_factory=lambda: [3.0, 3.0])
    #: Size of the mark, in real screen pixels (its longer side).
    size: int = 160
    #: 0 (invisible) - 1 (fully opaque). Applied to the whole mark, outline
    #: included, so it can be faded back to sit quietly in the corner.
    opacity: float = 0.9
    #: Off draws the mark as a plain static outline with no motion at all;
    #: on, it runs through its animated forming/dissolving cycle.
    animated: bool = True
    #: Which animation cycles are in rotation -- one is picked at random
    #: each time the previous one finishes. Names from
    #: gfx.brand.BRAND_STYLE_NAMES ("dots", "scanline", "pop", "glitch",
    #: "focus"). Empty is treated the same as every style being on, so a
    #: frame mid-upgrade with a blank list still shows something rather
    #: than nothing. Only looked at when `animated` is on.
    styles: list[str] = field(default_factory=lambda: [
        "dots", "scanline", "pop", "glitch", "focus"])


@dataclass
class Config:
    display: DisplayConfig = field(default_factory=DisplayConfig)
    slideshow: SlideshowConfig = field(default_factory=SlideshowConfig)
    viewer: ViewerConfig = field(default_factory=ViewerConfig)
    library: LibraryConfig = field(default_factory=LibraryConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    geo: GeoConfig = field(default_factory=GeoConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    appearance: AppearanceConfig = field(default_factory=AppearanceConfig)
    input: InputConfig = field(default_factory=InputConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    video_proxy: VideoProxyConfig = field(default_factory=VideoProxyConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    clock: ClockConfig = field(default_factory=ClockConfig)
    status: StatusConfig = field(default_factory=StatusConfig)
    brand: BrandConfig = field(default_factory=BrandConfig)

    #: Where this config was loaded from, for ``save()``.
    source_path: str | None = None

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> Config:
        candidates = [path] if path else list(DEFAULT_CONFIG_PATHS)
        for candidate in candidates:
            if not candidate:
                continue
            full = os.path.expanduser(candidate)
            if os.path.exists(full):
                return cls.from_file(full)
        if path:
            raise FileNotFoundError(f"config file not found: {path}")
        _log.info("no config file found; using built-in defaults")
        cfg = cls()
        cfg.source_path = os.path.expanduser(DEFAULT_CONFIG_PATHS[0])
        return cfg

    @classmethod
    def from_file(cls, path: str) -> Config:
        import yaml

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        cfg = cls.from_dict(data)
        cfg.source_path = path
        _log.info("loaded configuration from %s", path)
        return cfg

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        cfg = cls()
        for section_field in fields(cls):
            if section_field.name == "source_path":
                continue
            section = getattr(cfg, section_field.name)
            values = data.get(section_field.name)
            if values is None:
                continue
            if not isinstance(values, dict):
                _log.warning("config section %r should be a mapping; ignoring",
                             section_field.name)
                continue
            _apply(section, values, section_field.name)
        unknown = set(data) - {f.name for f in fields(cls)}
        for name in sorted(unknown):
            _log.warning("ignoring unknown config section %r", name)
        return cfg

    # -- saving ------------------------------------------------------------
    def as_dict(self) -> dict:
        out = {f.name: asdict(getattr(self, f.name))
               for f in fields(self) if f.name != "source_path"}
        return out

    def save(self, path: str | None = None) -> str:
        import yaml

        target = os.path.expanduser(path or self.source_path or DEFAULT_CONFIG_PATHS[0])
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.as_dict(), fh, sort_keys=False, allow_unicode=True,
                           default_flow_style=False)
            # os.replace is atomic against a *reader*, but says nothing about
            # power.  A picture frame lives on a wall socket, so losing power
            # between the write and the flush is the normal case rather than
            # the exotic one, and ext4's delayed allocation would leave a
            # zero-byte config behind.  Flush the file, then the directory
            # entry that now points at it.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)            # atomic: never a half-written config
        try:
            dir_fd = os.open(os.path.dirname(target) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:                    # pragma: no cover - not all filesystems
            pass
        self.source_path = target
        _log.info("configuration written to %s", target)
        return target

    # -- dotted access -----------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if is_dataclass(node) and hasattr(node, part):
                node = getattr(node, part)
            elif isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, dotted: str, value: Any) -> Any:
        """Set one setting, coercing the value to the declared type."""
        parts = dotted.split(".")
        node: Any = self
        for part in parts[:-1]:
            if is_dataclass(node) and hasattr(node, part):
                node = getattr(node, part)
            elif isinstance(node, dict):
                node = node.setdefault(part, {})
            else:
                raise KeyError(dotted)
        leaf = parts[-1]
        if is_dataclass(node):
            if leaf not in {f.name for f in fields(node)}:
                raise KeyError(dotted)
            value = _coerce(value, _hints(type(node)).get(leaf), dotted)
            value = _vet(dotted, value)
            setattr(node, leaf, value)
        elif isinstance(node, dict):
            node[leaf] = value
        else:
            raise KeyError(dotted)
        return value

    def copy(self) -> Config:
        return copy.deepcopy(self)


# --------------------------------------------------------------------------
# Locale
# --------------------------------------------------------------------------

def set_time_locale(name: str) -> bool:
    """Make ``%B``, ``%b`` and ``%A`` come out in the owner's language.

    A frame writes one date per picture, so "7 September 2026" on a German
    wall is the kind of small wrongness that never stops being noticed.
    picframe had ``model.locale``; picframe3 inherited the process locale
    instead, which under systemd on Raspberry Pi OS Lite is ``C`` -- English,
    whatever ``raspi-config`` was told, because nothing exports ``LANG`` to a
    service.

    Returns whether the locale is now in force.  A locale the system has not
    generated cannot be set, and saying so beats leaving the dates in English
    with no explanation -- ``doctor`` reports it too.
    """
    if not name:
        return True

    import locale as locale_module

    tried: list[str] = []
    for candidate in (name, name.replace("-", "_"),
                      f"{name}.UTF-8", f"{name}.utf8"):
        if candidate in tried:
            continue
        tried.append(candidate)
        try:
            locale_module.setlocale(locale_module.LC_TIME, candidate)
            _log.info("date and time names in %s", candidate)
            return True
        except locale_module.Error:
            continue
    _log.warning(
        "locale %r is not available on this system, so dates stay in the "
        "default language; build it with 'picframe3 setup --yes' (or add it "
        "to /etc/locale.gen and run 'sudo locale-gen')", name,
    )
    return False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@functools.cache
def _hints(cls: type) -> dict[str, Any]:
    """Resolved type hints.  ``from __future__ import annotations`` makes every
    annotation a string, so they have to be evaluated once, here."""
    import typing

    try:
        return typing.get_type_hints(cls)
    except Exception:  # pragma: no cover - exotic forward refs
        return {f.name: f.type for f in fields(cls)}


def _apply(section: Any, values: dict, prefix: str) -> None:
    known = _hints(type(section))
    valid = {f.name for f in fields(section)}
    for key, value in values.items():
        if key not in valid:
            _log.warning("ignoring unknown config key %s.%s", prefix, key)
            continue
        try:
            dotted = f"{prefix}.{key}"
            setattr(section, key, _vet(dotted, _coerce(value, known.get(key), dotted)))
        except (TypeError, ValueError) as exc:
            _log.warning("bad value for %s.%s (%s); keeping the default",
                         prefix, key, exc)


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _vet(dotted: str, value: Any) -> Any:
    """Reject a value the frame cannot draw, and clamp one it can only survive.

    Type coercion alone is not enough.  ``viewer.clock_position = ""`` is a
    perfectly good string and puts an ``IndexError`` in the middle of every
    frame; ``slideshow.interval = 0`` is a perfectly good float and turns the
    render loop into a busy loop that decodes twenty pictures a second.  Both
    arrive the same way -- one MQTT message, one line in a hand-edited file --
    so both are stopped here, at the one place every setting passes through.

    Choices raise (the caller reports "that is not a value for this"); ranges
    clamp with a log line, because "3600 seconds is the most I will wait" is
    more useful to somebody than a refusal.
    """
    from . import uischema

    allowed = uischema.CHOICES.get(dotted)
    if allowed and isinstance(value, str):
        match = {c.casefold(): c for c in allowed}.get(value.strip().casefold())
        if match is None:
            raise ValueError(f"{value!r} is not one of {', '.join(allowed)}")
        return match
    limits = uischema.LIMITS.get(dotted)
    if limits and isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = limits
        clamped = value
        if low is not None:
            clamped = max(low, clamped)
        if high is not None:
            clamped = min(high, clamped)
        if clamped != value:
            _log.warning("%s: %r is outside %s..%s; using %r",
                         dotted, value, low, high, clamped)
        return type(value)(clamped)
    return value


def _coerce(value: Any, annotation: Any, dotted: str) -> Any:
    """Best-effort conversion, tolerant of YAML's and MQTT's loose typing."""
    if value is None or annotation is None:
        return value
    origin = get_origin(annotation)
    if origin is not None:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        if origin in (list, set, tuple):
            items = list(value) if isinstance(value, (list, tuple, set)) else [value]
            # The elements matter as much as the container.  A colour typed
            # into a text box arrives as ["0", "0", "0", "1"], and four
            # strings in a list of floats do not fail here -- they fail much
            # later, inside the renderer or inside Pillow, with an error that
            # names neither the setting nor the value.
            inner = get_args(annotation)
            if inner and inner[0] is not Any:
                return [_coerce(item, inner[0], dotted) for item in items]
            return items
        if origin is dict:
            if isinstance(value, dict):
                return dict(value)
            # Silently turning a malformed schedule into {} deletes the
            # schedule and says nothing; the caller keeps the old value and
            # logs instead.
            raise ValueError(f"{value!r} is not a mapping")
        if len(args) == 1:
            return _coerce(value, args[0], dotted)
        return value
    if annotation is Any:
        return value
    if annotation is bool:
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(f"{value!r} is not a boolean")
        return bool(value)
    if annotation is int:
        return int(float(value))
    if annotation is float:
        return float(value)
    if annotation is str:
        return str(value)
    # A bare `dict` or `list` annotation -- power.schedule, input.keymap --
    # reaches here with no origin to inspect.  Letting anything through meant a
    # mistyped schedule was *stored* as a string and then quietly ignored by
    # everything that expected a mapping.
    if annotation is dict and not isinstance(value, dict):
        raise ValueError(f"{value!r} is not a mapping")
    if annotation is list and not isinstance(value, (list, tuple)):
        raise ValueError(f"{value!r} is not a list")
    return value


