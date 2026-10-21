import calendar as calendar_module
import datetime
import json
import math
import os
import time
import urllib.request
import board
import adafruit_dht
import paho.mqtt.client as mqtt
import yaml
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BROKER = "localhost"
PORT = 1883
TOPIC_TEMP = "home/dht11/temperature"
TOPIC_HUM = "home/dht11/humidity"
TOPIC_JSON = "home/dht11/state"
TOPIC_OUT = "home/outside/temperature"
TOPIC_OUTDOOR = "home/outdoor/temperature"   # published by the outdoor Pico W
TOPIC_OUTDOOR_HUM = "home/outdoor/humidity"  # published by the outdoor Pico W
TOPIC_INDOOR_PM25 = "home/indoor_pm25"       # published by pms5003_mqtt.py
TOPIC_INDOOR_PM10 = "home/indoor_pm10"       # published by pms5003_mqtt.py
TOPIC_TM_STATUS = "home/mac/timemachine_status"    # "ok"/"running"/"stale"/"error",
                                                    # published by mac_timemachine_status.py
TOPIC_TM_AGE_H = "home/mac/timemachine_age_hours"  # hours since the last successful backup
TOPIC_TM_PERCENT = "home/mac/timemachine_percent"  # 0-100, only while status == "running"
TOPIC_TM_GB_DONE = "home/mac/timemachine_gb_done"    # GB copied so far, only while running
TOPIC_TM_GB_TOTAL = "home/mac/timemachine_gb_total"  # GB this backup expects to copy, only while running
OUTDOOR_MAX_AGE = 30 * 60   # seconds; older than this -> fall back to OpenWeather
TM_REPORT_MAX_AGE = 3 * 60 * 60   # seconds; older than this -> treat as "no data from
                                   # the Mac" (asleep, off, or the launchd job stopped),
                                   # same idea as OUTDOOR_MAX_AGE above
WIFI_MAX_AGE = 2 * 60   # seconds; older than this -> hide the Wi-Fi line rather than
                        # show a reading that may no longer be true. PicFrame3 itself
                        # publishes its state roughly every 5s, so this only trips if
                        # the frame's own process (or the broker) has actually stopped.
INTERVAL = 10            # seconds between DHT reads


def _picframe_state_topic():
    """PicFrame3's own MQTT state topic, "<topic_prefix>/<device_id>/state"
    (default "picframe/picframe/state" -- see control/mqtt.py's MqttBridge),
    read once at startup from the same config.yaml this script already
    reads everything else from. mqtt.topic_prefix/device_id aren't treated
    as "live" settings anywhere else in picframe3 either -- changing them
    needs a restart, since they're also baked into Home Assistant discovery
    payloads -- so reading them once here rather than on every call matches
    that."""
    prefix, device_id = "picframe", "picframe"
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        mqtt_cfg = cfg.get("mqtt") or {}
        prefix = mqtt_cfg.get("topic_prefix", prefix)
        device_id = mqtt_cfg.get("device_id", device_id)
    except OSError:
        pass
    return f"{prefix}/{device_id}/state"
WEATHER_INTERVAL = 600   # seconds between weather fetches (10 min)

EXTRA_FILE = os.path.expanduser("~/clock_extra.txt")
COUNTDOWN_FILE = os.path.expanduser("~/display_countdown.txt")  # written by motion_display.py

OWM_KEY = "__OWM_KEY__"
LAT, LON = __LAT__, __LON__
WEATHER_URL = (
    "https://api.openweathermap.org/data/2.5/weather"
    f"?lat={LAT}&lon={LON}&units=metric&appid={OWM_KEY}"
)

def write_extra(text):
    tmp = EXTRA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, EXTRA_FILE)


# ---------------------------------------------------------------------
# Dot-grid + weather-line overlay image, plus the upper-left calendar
# widget (today's date and a small month grid, settings under Config
# section "calendar" / web Settings page "Calendar"). Both live on the
# same PNG, at opposite corners, so they share one viewer.overlay_image
# slot instead of needing a second one from PicFrame3 itself.
#
# PicFrame3's native clock_extra_file mechanism draws text with its own
# bundled font, which turned out to have very sparse glyph coverage (only
# a handful of symbols render; most Unicode dot/circle glyphs show as
# tofu boxes). So instead of feeding text through that path, this writes
# clock_extra_file as an empty string (silencing PicFrame3's own second
# line) and draws the ENTIRE weather line -- text and the idle-countdown
# dot grid together -- as one PNG, via PicFrame3's separate
# viewer.overlay_image mechanism (app.py: _tick_user_overlay(), which
# just watches a PNG's mtime and alpha-composites it full-screen).
#
# The positioning math below is a deliberate replica of PicFrame3's own
# gfx/overlays.py:clock() and gfx/textstyle.py:render_text(), using the
# SAME font, size formula, wrap algorithm and padding/shadow geometry, so
# our line lands pixel-for-pixel where the native text would have been.
# If PicFrame3 changes that layout code in a future update, this will
# need to be re-synced by hand -- it is not calling into their code.
#
# One useful derived identity (not obvious from their source, worth
# keeping documented here): the caption/extra line's BOTTOM edge in their
# native layout always equals screen_height - bottom_margin, regardless
# of the time line's own height or the negative "gap" the two lines are
# merged with -- those terms cancel exactly in the compositing math. So
# we don't need the time line's own metrics at all to place our line.
# ---------------------------------------------------------------------

_cfg_path = os.path.expanduser("~/.config/picframe3/config.yaml")
try:
    with open(_cfg_path, encoding="utf-8") as f:
        _cfg = yaml.safe_load(f) or {}
except OSError:
    _cfg = {}
_viewer_cfg = (_cfg.get("viewer") or {})

FONT_PATH = _viewer_cfg.get("font") or "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

#: media.fontlib's catalog, copied by hand rather than imported -- this
#: script runs in its own standalone venv (packaging/install_companions.sh),
#: with no picframe3 install to import from, the same reason _wrap_in_panel
#: below is its own copy of gfx/overlays.py's _panel_wrap rather than a
#: shared import. Keep this dict in step with
#: src/picframe3/media/fontlib.py's _LIBRARY by hand.
_FONT_LIBRARY = {
    "DejaVu Sans": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",),
    "DejaVu Sans Bold": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",),
    "DejaVu Sans Oblique": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",),
    "DejaVu Sans Condensed": ("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",),
    "DejaVu Serif": ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",),
    "DejaVu Serif Bold": ("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",),
    "DejaVu Sans Mono": ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",),
    "Liberation Sans": ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    "Liberation Sans Bold": ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                             "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    "Liberation Sans Italic": ("/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
                               "/usr/share/fonts/truetype/liberation2/LiberationSans-Italic.ttf"),
    "Liberation Serif": ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
                         "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf"),
    "Liberation Serif Bold": ("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
                              "/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf"),
    "Liberation Mono": ("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf"),
    "Noto Sans": ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",),
    "Noto Sans Bold": ("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",),
    "Noto Serif": ("/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf",),
    "FreeSans": ("/usr/share/fonts/truetype/freefont/FreeSans.ttf",),
}


def _resolve_font(name):
    """The first installed path for a media.fontlib catalog *name*, or
    FONT_PATH (the frame-wide default) when name is empty, unknown, or none
    of its candidate files exist here -- a bad per-widget font choice falls
    back quietly rather than taking a redraw down."""
    for path in _FONT_LIBRARY.get(name or "", ()):
        if os.path.exists(path):
            return path
    return FONT_PATH
CLOCK_SIZE = int(_viewer_cfg.get("clock_size", 48))
CLOCK_OFFSET_PCT = tuple(_viewer_cfg.get("clock_offset_pct", [3.0, 3.0]))
CLOCK_OPACITY = float(_viewer_cfg.get("clock_opacity", 1.0))
OVERLAY_IMAGE = _viewer_cfg.get("overlay_image") or "/dev/shm/picframe-overlay.png"

# Screen resolution -- PicFrame3 resizes the overlay PNG to fill the real
# screen regardless of the canvas size we draw at, but drawing at the
# WRONG aspect ratio would stretch our circles into ellipses. Confirmed
# via `fbset -s` on this Pi; update here if the display ever changes.
SCREEN_W, SCREEN_H = __SCREEN_W__, __SCREEN_H__

EXTRA_SCALE = 0.34          # overlays.py: clock(..., extra_scale=0.34)
LINE_SPACING = 1.25         # textstyle.TextStyle.line_spacing default
SHADOW_RADIUS = 3.0         # textstyle.TextStyle.shadow_radius default
SHADOW_OPACITY = 0.75       # textstyle.TextStyle.shadow_opacity default
PAD = int(SHADOW_RADIUS * 3)   # textstyle.render_text(): pad if shadow else 2
MAX_WIDTH_FRAC = 0.45       # overlays.py: max_width=int(w * 0.45)

#: Used only if config.yaml has no "overlay" section yet.
DEFAULT_OVERLAY_SETTINGS = {
    "dot_count": 16,          # 4x4 grid
    "gap_to_text": 14,        # px between the dot grid and the start of the text
    "filled_alpha": 235,
    "unfilled_alpha": 60,
    "background_color": [0, 0, 0],
    "background_opacity": 0.0,   # 0 = old drop-shadowed look, no panel
    "background_padding": 20,
    "font_family": "",
    "border_color": [255, 255, 255],
    "border_width": 0,
    "border_opacity": 0.9,
    "align": "L",
    "min_width_pct": 0.0,
}


def load_overlay_settings():
    """overlay.* from config.yaml, read fresh every call so a change made
    on the web Settings page (section "Weather overlay") reaches this
    already-running process without a restart."""
    settings = dict(DEFAULT_OVERLAY_SETTINGS)
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        return settings
    settings.update(cfg.get("overlay") or {})
    return settings


#: Used only if config.yaml has no "calendar" section yet.
DEFAULT_CALENDAR_SETTINGS = {
    "enabled": True,
    "offset_pct": [3.0, 3.0],
    "size": 34,
    "grid_size": 20,
    "grid_row_gap": 0,
    "square_padding": 1,
    "text_color": [255, 255, 255],
    "text_opacity": 0.9,
    "background_color": [0, 0, 0],
    "background_opacity": 0.35,
    "holiday_color": "dull_red",
    "font_family": "",
    "border_color": [255, 255, 255],
    "border_width": 0,
    "border_opacity": 0.9,
    "divider_width": 0,
    "divider_color": [255, 255, 255],
    "divider_opacity": 0.5,
    "grid_align_h": "center",
    "grid_align_v": "center",
    "today_fill_color": [255, 255, 255],
    "today_fill_opacity": 0.92,
    "today_border_color": [255, 255, 255],
    "today_border_width": 0,
    "today_border_opacity": 0.9,
    "holiday_align": "L",
}


def load_calendar_settings():
    """calendar.* from config.yaml, read fresh every call so a change made
    on the web Settings page (section "Calendar") reaches this already-
    running process without a restart."""
    settings = dict(DEFAULT_CALENDAR_SETTINGS)
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        return settings
    settings.update(cfg.get("calendar") or {})
    return settings


# Monday=0 .. Sunday=6, matching Python's calendar module. The grid always
# starts the week on Monday -- there is no setting for this yet.
_WEEKDAY_HEADERS = ["M", "T", "W", "T", "F", "S", "S"]

#: Gazetted ("Closed") holidays, copied by hand from the Army Postal Service
#: Corps 2026 calendar (Delhi). Keyed by (month, day) -> festival name. This
#: is the static stand-in for calendar.py's long-deferred "sync to some
#: online calendar to get Indian holidays" -- there's no live source yet,
#: so a new year's dates need adding here by hand until there is one.
HOLIDAYS_BY_YEAR = {
    2026: {
        (1, 26): "Republic Day",
        (3, 4): "Holi",
        (3, 21): "Id-ul-Fitr",
        (3, 26): "Ram Navami",
        (3, 31): "Mahavir Jayanti",
        (4, 3): "Good Friday",
        (5, 1): "Buddha Purnima",
        (5, 27): "Id-ul-Zuha (Bakrid)",
        (6, 26): "Muharram",
        (8, 15): "Independence Day",
        (8, 26): "Milad-un-Nabi",
        (9, 4): "Janmashtami",
        (10, 2): "Gandhi Jayanti",
        (10, 20): "Dussehra",
        (11, 8): "Diwali",
        (11, 24): "Guru Nanak's Birthday",
        (12, 25): "Christmas Day",
    },
}

#: The palette calendar.holiday_color picks from (web Settings page,
#: section "Calendar", "Holiday colour") -- a dropdown of named swatches
#: rather than a 3-number RGB field, so it reads as "pick a colour" rather
#: than "mix one". Keep this in lock-step with CHOICES["calendar.holiday_color"]
#: in src/picframe3/uischema.py -- that's what the Settings page offers, and
#: a name picked there that isn't a key here falls back to "dull_red" below.
HOLIDAY_PALETTE = {
    "dull_red": (168, 76, 70),
    "crimson": (196, 46, 58),
    "orange": (214, 115, 40),
    "amber": (209, 155, 40),
    "olive": (121, 128, 56),
    "forest_green": (63, 125, 88),
    "teal": (52, 128, 128),
    "sky_blue": (70, 130, 190),
    "indigo": (90, 86, 180),
    "violet": (140, 86, 180),
    "magenta": (186, 76, 150),
    "slate_grey": (120, 128, 138),
    "white": (255, 255, 255),
}


#: Breathing room (px) between the text/grid content and the translucent
#: panel's own edge, scaled with the widget's own size setting below.
_PANEL_PAD_FRAC = 0.45

#: How much lighter/darker than the background highlight circle has to read,
#: on a 0-255 luminance scale, to pick black vs. white for the "today"
#: number -- lets the circle itself always be drawn in text_color while the
#: number on top of it stays legible against either a light or a dark pick.
def _contrast_text(rgb):
    r, g, b = rgb
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return (0, 0, 0, 255) if luminance > 150 else (255, 255, 255, 255)


def _calendar_widget_image(today, font_path, size, grid_size, grid_row_gap,
                           square_padding, text_color, text_opacity, holiday_color, *,
                           divider_width=0, divider_color=(255, 255, 255), divider_opacity=0.5,
                           grid_align_h="center", grid_align_v="center",
                           today_fill_color=None, today_fill_opacity=0.92,
                           today_border_color=None, today_border_width=0,
                           today_border_opacity=0.9, holiday_align="L"):
    """Today's date/day-name, and a small month grid below it with today
    highlighted. One RGBA image, sized to its own content -- the caller
    places it, so this knows nothing about screen position or the
    translucent background panel (that's write_calendar's job, below).

    size and grid_size are independent: size is the "today" line's own
    point size, grid_size the day-numbers'/weekday-header's. grid_row_gap
    adds extra vertical space between every row of the grid -- between the
    weekday-header row and the first week, and between each week after
    that. text_color is an (r, g, b) tuple and text_opacity 0-1; both apply
    to the date line, the weekday headers and the day numbers alike.
    holiday_color is an (r, g, b) tuple too, already resolved from the
    calendar.holiday_color palette name by the caller.

    divider_width > 0 draws a horizontal rule, in divider_color at
    divider_opacity, across the gap between the "today" line and the month
    grid below it -- 0 (default) draws nothing there, as before this
    existed.

    grid_align_h/grid_align_v ("center"/"center" by default, matching the
    original always-centred placement exactly) say where each day number
    sits within its own cell: left/center/right across, top/center/bottom
    down.

    The "today" highlight square's fill and outline are independent of
    text_color/holiday_color -- today_fill_color defaults to text_color at
    today_fill_opacity 0.92 (235/255, the exact look this had before these
    existed), and today_border_width > 0 adds a stroked outline around the
    square alone in today_border_color/today_border_opacity. Its own
    day-number is still switched to black or white for contrast against
    whatever today_fill_color ends up being. square_padding shrinks the
    square by this many pixels on every side, so it reads as a mark inside
    its cell rather than a tile that fills it edge to edge.

    holiday_align ("L" by default, matching the original always-flush-left
    placement exactly) justifies the "D: Festival Name" lines below the
    grid -- L/C/R -- within the widget's own full width."""
    try:
        date_font = ImageFont.truetype(font_path, size)
        grid_font = ImageFont.truetype(font_path, grid_size)
    except OSError:
        fallback = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        date_font = ImageFont.truetype(fallback, size)
        grid_font = ImageFont.truetype(fallback, grid_size)

    text_alpha = max(0, min(255, round(255 * text_opacity)))
    text_rgba = (*text_color, text_alpha)
    header_rgba = (*text_color, round(text_alpha * 0.75))   # weekday row, a touch dimmer
    holiday_rgba = (*holiday_color, text_alpha)
    holiday_header_rgba = (*holiday_color, round(text_alpha * 0.75))
    today_fill_color = today_fill_color if today_fill_color is not None else text_color
    today_fill_rgba = (*today_fill_color, max(0, min(255, round(255 * today_fill_opacity))))
    today_border_color = today_border_color if today_border_color is not None else (255, 255, 255)
    today_border_rgba = (*today_border_color, max(0, min(255, round(255 * today_border_opacity))))
    today_number_rgba = _contrast_text(today_fill_color)

    holidays = HOLIDAYS_BY_YEAR.get(today.year, {})
    this_month_holidays = sorted(
        (day, name) for (month, day), name in holidays.items() if month == today.month)

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    date_line = today.strftime("%A, %-d %B")
    date_w = int(probe.textlength(date_line, font=date_font))
    date_ascent, date_descent = date_font.getmetrics()
    date_h = date_ascent + date_descent

    # One grid row's natural height: the font's own line height, with no
    # added padding -- grid_row_gap alone controls any extra breathing
    # room, so a setting of 0 really does pack the rows as tight as the
    # font allows, rather than leaving a fixed cushion around each row
    # regardless of what grid_row_gap says.
    cell_h = max(14, sum(grid_font.getmetrics()))
    grid_gap_y = round(size * 0.3)          # gap between the date line and the grid

    cal = calendar_module.Calendar(firstweekday=0)   # Monday first
    weeks = cal.monthdayscalendar(today.year, today.month)

    grid_w_natural = cell_h * 7
    # One "row pitch" (top of one row to the top of the next) covers the
    # weekday-header row and every week row alike -- grid_row_gap is the
    # extra space folded into each step, on top of the row's own cell_h.
    row_pitch = cell_h + grid_row_gap
    num_rows = len(weeks) + 1   # the header row, plus one per week
    grid_h = cell_h * num_rows + grid_row_gap * (num_rows - 1)

    # One entry per holiday this month, "D: Festival Name" -- reuses
    # grid_font, so it tracks the same size setting as the day numbers.
    # Two adjacent entries share one line, separated by " | ", whenever
    # they both fit within the panel's base width (the date line / grid's
    # own width, before any holiday text is considered) -- otherwise the
    # second one starts a fresh line of its own.
    holiday_entries = [f"{day}: {name}" for day, name in this_month_holidays]
    base_width = max(date_w, grid_w_natural)
    holiday_lines = []
    i = 0
    while i < len(holiday_entries):
        first = holiday_entries[i]
        if i + 1 < len(holiday_entries):
            combined = f"{first}   |   {holiday_entries[i + 1]}"
            if int(probe.textlength(combined, font=grid_font)) <= base_width:
                holiday_lines.append(combined)
                i += 2
                continue
        holiday_lines.append(first)
        i += 1
    holiday_line_h = sum(grid_font.getmetrics())
    holiday_gap_y = round(grid_size * 0.4) if holiday_lines else 0
    holiday_block_h = holiday_gap_y + holiday_line_h * len(holiday_lines)
    holiday_widths = [int(probe.textlength(line, font=grid_font)) for line in holiday_lines]

    width = max(date_w, grid_w_natural, *holiday_widths) if holiday_widths \
        else max(date_w, grid_w_natural)
    height = date_h + grid_gap_y + grid_h + holiday_block_h

    # The grid's columns stretch to fill the panel's full width (whichever
    # of the date line / natural 7-column grid is wider), rather than
    # staying at their natural size and floating centred with empty space
    # down the sides -- cell_w can come out wider than cell_h when the date
    # line is the wider of the two, which is the common case. The day-
    # number font itself stays at grid_size regardless (set above), so
    # widening the columns doesn't change how big the numbers read.
    cell_w = width / 7.0

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # If the date line itself is the narrower one (a short month name, a
    # small locale format), centre it over the full-width grid below it.
    date_x = (width - date_w) // 2

    draw.text((date_x, 0), date_line, font=date_font, fill=text_rgba)

    if divider_width > 0:
        divider_alpha = max(0, min(255, round(255 * divider_opacity)))
        divider_y = date_h + grid_gap_y / 2
        draw.line([(0, divider_y), (width, divider_y)],
                 fill=(*divider_color, divider_alpha), width=divider_width)

    grid_top = date_h + grid_gap_y
    for col, label in enumerate(_WEEKDAY_HEADERS):
        cx = col * cell_w + cell_w / 2
        lw = probe.textlength(label, font=grid_font)
        is_sunday = col == 6
        draw.text((cx - lw / 2, grid_top), label, font=grid_font,
                  fill=holiday_header_rgba if is_sunday else header_rgba)

    # The "today" highlight is a sharp-edged square (not stretched into a
    # rectangle even when columns are wider than tall), sized to the
    # smaller of the two and centred within its own column/row.
    # square_padding is per side, so it comes off twice.
    highlight_d = max(1, min(cell_w, cell_h) - 2 * square_padding)
    # Breathing room between a day number and its cell's own edge, when
    # grid_align_h/v pins it to a side rather than the centre.
    cell_pad = max(2, round(min(cell_w, cell_h) * 0.08))
    grid_align_h = (grid_align_h or "center").lower()
    grid_align_v = (grid_align_v or "center").lower()

    for row, week in enumerate(weeks):
        for col, day in enumerate(week):
            if day == 0:
                continue   # padding day from the previous/next month
            cy = grid_top + row_pitch * (row + 1)   # row 0 of the grid is the header
            cx = col * cell_w
            ccx, ccy = cx + cell_w / 2, cy + cell_h / 2   # column/row centre
            is_today = day == today.day
            is_sunday = col == 6
            is_holiday = (today.month, day) in holidays
            label = str(day)
            lw = probe.textlength(label, font=grid_font)
            if grid_align_h == "left":
                tx = cx + cell_pad
            elif grid_align_h == "right":
                tx = cx + cell_w - lw - cell_pad
            else:
                tx = ccx - lw / 2
            if grid_align_v == "top":
                ty = cy + cell_pad
            elif grid_align_v == "bottom":
                ty = cy + cell_h - grid_font.size - cell_pad
            else:
                ty = ccy - grid_font.size / 2
            if is_today:
                # Centred on the number's OWN anchor (tx/ty/lw), not the
                # cell's centre -- otherwise grid_align_h/v moves the day
                # number but leaves the highlight square behind at the
                # cell's middle, so the two drift apart the moment
                # alignment is anything but centred.
                hcx = tx + lw / 2
                hcy = ty + grid_font.size / 2
                box = [hcx - highlight_d / 2, hcy - highlight_d / 2,
                      hcx + highlight_d / 2, hcy + highlight_d / 2]
                draw.rectangle(box, fill=today_fill_rgba)
                if today_border_width > 0:
                    draw.rectangle(box, outline=today_border_rgba, width=today_border_width)
            if is_today:
                day_rgba = today_number_rgba
            elif is_sunday or is_holiday:
                day_rgba = holiday_rgba
            else:
                day_rgba = text_rgba
            draw.text((tx, ty), label, font=grid_font, fill=day_rgba)

    holiday_top = grid_top + grid_h + holiday_gap_y
    holiday_align = (holiday_align or "L").upper()
    for i, line in enumerate(holiday_lines):
        ly = holiday_top + i * holiday_line_h
        lw = holiday_widths[i]
        if holiday_align == "C":
            lx = (width - lw) / 2
        elif holiday_align == "R":
            lx = width - lw
        else:
            lx = 0
        draw.text((lx, ly), line, font=grid_font, fill=holiday_rgba)

    return img


def _wrap_in_panel(content, pad, bg_color, bg_opacity, *, radius_frac=0.6, size_based_radius=False,
                    border_color=None, border_width=0, border_opacity=1.0,
                    min_width=0, justify="L"):
    """Common look shared by the calendar, clock and status widgets: a
    rounded translucent panel sized to `content` plus `pad` on every side,
    with `content` composited on top. No panel at all (just `content` on
    its own transparent canvas) when bg_opacity is 0.

    `pad` is either a single number (same margin on every side) or an
    (x, y) pair -- horizontal margin, vertical margin -- for a widget like
    the clock whose text is wide but only one line tall, where an equal
    pixel margin on every side reads as barely-there sideways and
    disproportionate top/bottom.

    `radius_frac` scales the corner radius off min(pad_x, pad_y) by default
    (the calendar/status look, left exactly as it always was).

    `size_based_radius` (the clock) scales off the panel's own shorter side
    instead, ignoring padding and radius_frac entirely -- padding-based
    rounding breaks down whenever padding itself is set small/tight (e.g.
    panel_padding_y pulled down for a snug vertical fit), which otherwise
    floors the radius near-flat no matter how big the panel actually is.

    `border_width` > 0 strokes the same rounded rectangle's outline, in
    `border_color` at `border_opacity`, on top of the fill (or on its own,
    over a transparent panel, when bg_opacity is 0 -- an outline-only
    look). The stroke is drawn inset by half its own width so it doesn't
    get clipped at the panel's edge.

    `min_width` (0 by default -- the panel hugs `content` exactly, as
    before this existed) stretches the panel to at least that many pixels
    wide, with `content` kept aligned inside per `justify` (L/C/R) rather
    than left-packed unconditionally -- this is what makes `justify` mean
    anything at all; at the default min_width=0 the panel is always
    exactly content-sized and justify has nothing to do."""
    pad_x, pad_y = pad if isinstance(pad, (tuple, list)) else (pad, pad)
    inner_w = max(content.width, max(0, min_width - pad_x * 2))
    panel_w = inner_w + pad_x * 2
    panel_h = content.height + pad_y * 2
    widget = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
    if bg_opacity > 0 or border_width > 0:
        panel_draw = ImageDraw.Draw(widget)
        if size_based_radius:
            radius = max(8, min(60, round(min(panel_w, panel_h) * 0.28)))
        else:
            radius = max(4, min(60, round(min(pad_x, pad_y) * radius_frac)))
        if bg_opacity > 0:
            bg_alpha = round(255 * bg_opacity)
            panel_draw.rounded_rectangle(
                [0, 0, panel_w - 1, panel_h - 1], radius=radius,
                fill=(*bg_color, bg_alpha))
        if border_width > 0:
            inset = border_width / 2
            border_alpha = round(255 * border_opacity)
            panel_draw.rounded_rectangle(
                [inset, inset, panel_w - 1 - inset, panel_h - 1 - inset],
                radius=max(0, radius - round(inset)),
                outline=(*(border_color or (255, 255, 255)), border_alpha),
                width=border_width)
    justify = (justify or "L").upper()
    if justify == "C":
        cx = pad_x + (inner_w - content.width) // 2
    elif justify == "R":
        cx = pad_x + (inner_w - content.width)
    else:
        cx = pad_x
    widget.alpha_composite(content, (cx, pad_y))
    return widget


def _anchor_xy(position, offset_pct, widget_size):
    """Top-left (x, y) to composite a widget of widget_size so it sits
    inset from the named corner -- TL/TC/TR/BL/BC/BR, same scheme as
    viewer.clock_position -- by offset_pct percent of the screen."""
    x_pct, y_pct = offset_pct
    ox = int(SCREEN_W * float(x_pct) / 100)
    oy = int(SCREEN_H * float(y_pct) / 100)
    w, h = widget_size
    vert, horiz = position[0], position[1]
    y = oy if vert == "T" else SCREEN_H - oy - h
    if horiz == "L":
        x = ox
    elif horiz == "C":
        x = (SCREEN_W - w) // 2
    else:   # "R"
        x = SCREEN_W - ox - w
    return x, y


def write_calendar(canvas):
    """Composites the calendar widget onto `canvas` at its configured
    top-left inset, on top of its own translucent background panel, if
    calendar.enabled. Drawn independently of the weather line/dot grid --
    it shows even with no weather data."""
    settings = load_calendar_settings()
    if not settings["enabled"]:
        return

    size = int(settings["size"])
    grid_size = int(settings["grid_size"])
    grid_row_gap = int(settings["grid_row_gap"])
    square_padding = int(settings.get("square_padding", 1))
    text_color = tuple(int(v) for v in settings["text_color"])
    text_opacity = max(0.0, min(1.0, float(settings["text_opacity"])))
    bg_color = tuple(int(v) for v in settings["background_color"])
    bg_opacity = max(0.0, min(1.0, float(settings["background_opacity"])))
    holiday_color = HOLIDAY_PALETTE.get(settings["holiday_color"], HOLIDAY_PALETTE["dull_red"])
    font_path = _resolve_font(settings.get("font_family", ""))
    border_color = tuple(int(v) for v in settings.get("border_color", [255, 255, 255]))
    border_width = int(settings.get("border_width", 0))
    border_opacity = max(0.0, min(1.0, float(settings.get("border_opacity", 0.9))))
    divider_width = int(settings.get("divider_width", 0))
    divider_color = tuple(int(v) for v in settings.get("divider_color", [255, 255, 255]))
    divider_opacity = max(0.0, min(1.0, float(settings.get("divider_opacity", 0.5))))
    grid_align_h = settings.get("grid_align_h", "center")
    grid_align_v = settings.get("grid_align_v", "center")
    today_fill_color = tuple(int(v) for v in settings.get("today_fill_color", [255, 255, 255]))
    today_fill_opacity = max(0.0, min(1.0, float(settings.get("today_fill_opacity", 0.92))))
    today_border_color = tuple(int(v) for v in settings.get("today_border_color", [255, 255, 255]))
    today_border_width = int(settings.get("today_border_width", 0))
    today_border_opacity = max(0.0, min(1.0, float(settings.get("today_border_opacity", 0.9))))
    holiday_align = settings.get("holiday_align", "L")

    content = _calendar_widget_image(
        datetime.date.today(), font_path, size, grid_size, grid_row_gap, square_padding,
        text_color, text_opacity, holiday_color,
        divider_width=divider_width, divider_color=divider_color, divider_opacity=divider_opacity,
        grid_align_h=grid_align_h, grid_align_v=grid_align_v,
        today_fill_color=today_fill_color, today_fill_opacity=today_fill_opacity,
        today_border_color=today_border_color, today_border_width=today_border_width,
        today_border_opacity=today_border_opacity, holiday_align=holiday_align)

    pad = max(6, round(size * _PANEL_PAD_FRAC))
    widget = _wrap_in_panel(content, pad, bg_color, bg_opacity,
                            border_color=border_color, border_width=border_width,
                            border_opacity=border_opacity)

    x_pct, y_pct = settings["offset_pct"]
    x = int(SCREEN_W * float(x_pct) / 100)
    y = int(SCREEN_H * float(y_pct) / 100)
    canvas.alpha_composite(widget, (x, y))


#: Used only if config.yaml has no "clock" section yet.
DEFAULT_CLOCK_SETTINGS = {
    "enabled": False,
    "position": "TR",
    "offset_pct": [3.0, 3.0],
    "size": 64,
    "format_24h": True,
    "text_color": [255, 255, 255],
    "text_opacity": 0.9,
    "background_color": [0, 0, 0],
    "background_opacity": 0.35,
    "panel_padding_x": 40,
    "panel_padding_y": 16,
    "font_family": "",
    "border_color": [255, 255, 255],
    "border_width": 0,
    "border_opacity": 0.9,
    "align": "L",
    "min_width_pct": 0.0,
}


def load_clock_settings():
    """clock.* from config.yaml, read fresh every call so a change made on
    the web Settings page (section "Clock") reaches this already-running
    process without a restart."""
    settings = dict(DEFAULT_CLOCK_SETTINGS)
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        return settings
    settings.update(cfg.get("clock") or {})
    return settings


def write_clock(canvas):
    """Composites a custom-drawn clock onto `canvas`, on its own
    translucent panel, if clock.enabled. This exists because PicFrame3's
    own built-in clock (viewer.show_clock) is drawn by the core app, not
    this overlay, so it can't get a panel -- this is a from-scratch
    replacement for it, not something layered on top of it. Turn off
    viewer.show_clock if you enable this, so you don't get two clocks."""
    settings = load_clock_settings()
    if not settings["enabled"]:
        return

    size = int(settings["size"])
    text_color = tuple(int(v) for v in settings["text_color"])
    text_opacity = max(0.0, min(1.0, float(settings["text_opacity"])))
    bg_color = tuple(int(v) for v in settings["background_color"])
    bg_opacity = max(0.0, min(1.0, float(settings["background_opacity"])))
    font_path = _resolve_font(settings.get("font_family", ""))
    border_color = tuple(int(v) for v in settings.get("border_color", [255, 255, 255]))
    border_width = int(settings.get("border_width", 0))
    border_opacity = max(0.0, min(1.0, float(settings.get("border_opacity", 0.9))))
    align = settings.get("align", "L")
    min_width_pct = max(0.0, float(settings.get("min_width_pct", 0.0)))

    try:
        clock_font = ImageFont.truetype(font_path, size)
    except OSError:
        clock_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)

    now = datetime.datetime.now()
    time_fmt = "%H:%M" if settings["format_24h"] else "%-I:%M %p"
    time_text = now.strftime(time_fmt)

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    text_w = int(probe.textlength(time_text, font=clock_font))
    ascent, descent = clock_font.getmetrics()
    text_h = ascent + descent

    text_alpha = max(0, min(255, round(255 * text_opacity)))
    content = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
    ImageDraw.Draw(content).text((0, 0), time_text, font=clock_font,
                                  fill=(*text_color, text_alpha))

    pad_x = max(0, int(settings.get("panel_padding_x", 40)))
    pad_y = max(0, int(settings.get("panel_padding_y", 16)))
    min_width = round(SCREEN_W * min_width_pct / 100) if min_width_pct > 0 else 0
    widget = _wrap_in_panel(content, (pad_x, pad_y), bg_color, bg_opacity, size_based_radius=True,
                            border_color=border_color, border_width=border_width,
                            border_opacity=border_opacity, min_width=min_width, justify=align)

    x, y = _anchor_xy(settings["position"], settings["offset_pct"],
                      (widget.width, widget.height))
    canvas.alpha_composite(widget, (x, y))


#: Used only if config.yaml has no "status" section yet.
DEFAULT_STATUS_SETTINGS = {
    "enabled": True,
    "position": "TR",
    "offset_pct": [3.0, 3.0],
    "size": 26,
    "text_color": [255, 255, 255],
    "text_opacity": 0.9,
    "background_color": [0, 0, 0],
    "background_opacity": 0.35,
    "font_family": "",
    "border_color": [255, 255, 255],
    "border_width": 0,
    "border_opacity": 0.9,
    "align": "L",
    "min_width_pct": 0.0,
}


def load_status_settings():
    """status.* from config.yaml, read fresh every call so a change made on
    the web Settings page (section "Status strip") reaches this already-
    running process without a restart."""
    settings = dict(DEFAULT_STATUS_SETTINGS)
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        return settings
    settings.update(cfg.get("status") or {})
    return settings


def _wifi_icon(size, linked, rgba):
    """A small hand-drawn Wi-Fi glyph -- three nested arcs and a dot when
    linked, one dim arc crossed out when not. Drawn with PIL primitives
    rather than an emoji character, since the Pi's own fonts have no emoji
    glyphs and would just show a missing-character box."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = size / 2, size - size / 6
    width = max(2, round(size * 0.09))
    dot_r = max(2, round(size * 0.06))
    if linked:
        for rad in (size * 0.42, size * 0.30, size * 0.18):
            d.arc([cx - rad, cy - rad, cx + rad, cy + rad],
                  start=215, end=325, fill=rgba, width=width)
        d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=rgba)
    else:
        rad = size * 0.42
        d.arc([cx - rad, cy - rad, cx + rad, cy + rad],
              start=215, end=325, fill=rgba, width=width)
        d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=rgba)
        d.line([(size * 0.08, size * 0.12), (size * 0.92, size * 0.88)],
              fill=rgba, width=width)
    return img


def _signal_bars_icon(size, percent, rgba):
    """Four signal bars, like a phone's status bar, filled up to `percent`
    (0-100) of the Pi's own Wi-Fi link -- a separate glyph from _wifi_icon()
    above, which actually means "is the outdoor Pico W sensor's data
    fresh", not the Pi's own radio."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bars = 4
    filled = max(0, min(bars, math.ceil((percent or 0) / 100 * bars)))
    gap = max(1, round(size * 0.08))
    bar_w = (size - gap * (bars - 1)) / bars
    dim = (*rgba[:3], max(40, round(rgba[3] * 0.3))) if len(rgba) == 4 else rgba
    for i in range(bars):
        h = size * (i + 1) / bars
        x0 = i * (bar_w + gap)
        x1 = x0 + bar_w
        y1 = size
        y0 = size - h
        d.rectangle([x0, y0, x1, y1], fill=(rgba if i < filled else dim))
    return img


def _tm_status_text(tm):
    """The Time Machine line's text, from the last report relayed over MQTT
    by mac_timemachine_status.py. `tm` is the module-level tm_backup dict."""
    status, age_h, percent = tm["status"], tm["age_h"], tm["percent"]
    gb_done, gb_total = tm["gb_done"], tm["gb_total"]
    if status == "running":
        if percent is None:
            return "TM: backup running"
        if gb_done is not None and gb_total is not None:
            return f"TM: {percent:.0f}% ({gb_done:.1f}/{gb_total:.1f} GB)"
        return f"TM: {percent:.0f}%"
    if status == "error":
        return "TM: backup error"
    if age_h is None:
        return f"TM: {status}"
    age_text = f"{round(age_h)}h ago" if age_h >= 1 else f"{max(1, round(age_h * 60))}m ago"
    prefix = "stale, last " if status == "stale" else "last "
    return f"TM: {prefix}{age_text}"


def write_status(canvas):
    """Composites the status strip onto `canvas`, on its own translucent
    panel, if status.enabled. Rows, in order: whether the outdoor Pico W is
    still being heard from (same freshness check, OUTDOOR_MAX_AGE, the main
    loop already uses to decide whether to fall back to OpenWeather, so this
    always agrees with what's actually driving the outdoor reading); the
    Pi's own Wi-Fi SSID and signal strength, relayed from picframe3's own
    MQTT state topic (see STATE_TOPIC / on_message below), shown only once
    something has actually been heard from it and hidden again if that goes
    quiet for WIFI_MAX_AGE; and the Time Machine backup state relayed from
    the Mac, same "hide if stale/never heard from" treatment. A frame with
    neither Wi-Fi reporting nor a Mac script configured still shows just the
    one "Out sensor" line, unchanged."""
    settings = load_status_settings()
    if not settings["enabled"]:
        return

    size = int(settings["size"])
    text_color = tuple(int(v) for v in settings["text_color"])
    text_opacity = max(0.0, min(1.0, float(settings["text_opacity"])))
    bg_color = tuple(int(v) for v in settings["background_color"])
    bg_opacity = max(0.0, min(1.0, float(settings["background_opacity"])))
    font_path = _resolve_font(settings.get("font_family", ""))
    border_color = tuple(int(v) for v in settings.get("border_color", [255, 255, 255]))
    border_width = int(settings.get("border_width", 0))
    border_opacity = max(0.0, min(1.0, float(settings.get("border_opacity", 0.9))))
    align = settings.get("align", "L")
    min_width_pct = max(0.0, float(settings.get("min_width_pct", 0.0)))

    try:
        status_font = ImageFont.truetype(font_path, size)
    except OSError:
        status_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)

    linked = (outdoor["temp"] is not None
             and time.monotonic() - outdoor["time"] <= OUTDOOR_MAX_AGE)

    show_wifi = (wifi["ssid"] is not None
                and time.monotonic() - wifi["time"] <= WIFI_MAX_AGE)
    wifi_percent = wifi["signal_percent"] if show_wifi else None

    show_tm = (tm_backup["status"] is not None
              and time.monotonic() - tm_backup["time"] <= TM_REPORT_MAX_AGE)

    # Each row as (icon_kind, text); icon_kind picks which helper draws that
    # row's glyph once the real icon size (line_h) is known below. Keeping
    # this a plain list, rather than the previous hardcoded line1/line2, is
    # what lets a third row (or a frame with no Mac script, or no Wi-Fi
    # report yet) fit without special-casing every combination.
    text_alpha = max(0, min(255, round(255 * text_opacity)))
    icon_rgba = (*text_color, text_alpha)
    rows = [("linked", f"Out sensor {'OK' if linked else 'down'}")]
    if show_wifi:
        wifi_text = wifi["ssid"]
        if wifi_percent is not None:
            wifi_text += f"  {round(wifi_percent)}%"
        rows.append(("signal", wifi_text))
    if show_tm:
        rows.append((None, _tm_status_text(tm_backup)))

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    ascent, descent = status_font.getmetrics()
    line_h = ascent + descent
    icon_size = line_h
    icon_gap = max(4, round(size * 0.3))
    row_gap = max(2, round(size * 0.3))
    text_x = icon_size + icon_gap

    content_w = 0
    for _, text in rows:
        text_w = int(probe.textlength(text, font=status_font))
        content_w = max(content_w, text_x + text_w)
    content_h = line_h * len(rows) + row_gap * (len(rows) - 1)

    content = Image.new("RGBA", (content_w, content_h), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(content)
    for i, (icon_kind, text) in enumerate(rows):
        y = i * (line_h + row_gap)
        if icon_kind == "linked":
            content.alpha_composite(_wifi_icon(icon_size, linked, icon_rgba), (0, y))
        elif icon_kind == "signal":
            content.alpha_composite(
                _signal_bars_icon(icon_size, wifi_percent, icon_rgba), (0, y))
        cdraw.text((text_x, y), text, font=status_font, fill=(*text_color, text_alpha))

    pad = max(6, round(size * _PANEL_PAD_FRAC))
    min_width = round(SCREEN_W * min_width_pct / 100) if min_width_pct > 0 else 0
    widget = _wrap_in_panel(content, pad, bg_color, bg_opacity,
                            border_color=border_color, border_width=border_width,
                            border_opacity=border_opacity, min_width=min_width, justify=align)

    x, y = _anchor_xy(settings["position"], settings["offset_pct"],
                      (widget.width, widget.height))
    canvas.alpha_composite(widget, (x, y))


def _wrap_text(draw, text, font, max_width):
    """Exact replica of picframe3's gfx/textstyle.py _wrap()."""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        words = paragraph.split(" ")
        line = ""
        for word in words:
            probe = f"{line} {word}".strip()
            if draw.textlength(probe, font=font) <= max_width or not line:
                line = probe
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _caption_box(text, font):
    """Where PicFrame3's native caption line would be drawn (BR position,
    which is what this config uses), replicating gfx/overlays.py:clock()."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    max_width = int(SCREEN_W * MAX_WIDTH_FRAC)
    lines = _wrap_text(probe, text, font, max_width)
    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * LINE_SPACING)
    width = max(1, int(max(probe.textlength(ln, font=font) for ln in lines))) + PAD * 2
    height = line_h * len(lines) + PAD * 2
    dx = int(SCREEN_W * CLOCK_OFFSET_PCT[0] / 100)
    dy = int(SCREEN_H * CLOCK_OFFSET_PCT[1] / 100)
    right_edge = SCREEN_W - dx
    bottom_edge = SCREEN_H - dy   # derived identity -- see module docstring above
    left = right_edge - width
    top = bottom_edge - height
    return left, top, width, height, lines, line_h


def _dot_grid_image(filled, dot_d, gap, rows=4, cols=4, filled_alpha=235, unfilled_alpha=60):
    w = cols * dot_d + (cols - 1) * gap
    h = rows * dot_d + (rows - 1) * gap
    ss = 4   # supersample for anti-aliased circles
    hi = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(hi)
    d, g = dot_d * ss, gap * ss
    for i in range(rows * cols):
        row, col = divmod(i, cols)
        x0, y0 = col * (d + g), row * (d + g)
        color = (255, 255, 255, filled_alpha) if i < filled else (255, 255, 255, unfilled_alpha)
        draw.ellipse([x0, y0, x0 + d, y0 + d], fill=color)
    return hi.resize((w, h), Image.LANCZOS)


#: The last canvas actually written, as raw pixel bytes -- compared before
#: every save so a cycle where nothing visibly changed (the common case:
#: the DHT reading is flat, the dot grid hasn't ticked down, no new minute)
#: does not touch OVERLAY_IMAGE's mtime at all. PicFrame3's own
#: _tick_user_overlay() watches that mtime and reloads + full-screen
#: LANCZOS-resizes the PNG the instant it changes, on its main render
#: thread -- a real stall if it fires every INTERVAL seconds forever,
#: video or no video, for a file that is usually pixel-identical to the
#: one before it anyway.
_last_canvas_bytes = None


def write_overlay(line_text, filled_dots):
    """Draws the weather line + idle-countdown dot grid as one PNG and
    writes it (atomically) to OVERLAY_IMAGE for PicFrame3 to pick up --
    but only when the rendered canvas actually differs from the last one
    written (see _last_canvas_bytes above)."""
    global _last_canvas_bytes
    overlay_settings = load_overlay_settings()
    caption_font_path = _resolve_font(overlay_settings.get("font_family", ""))
    try:
        caption_font = ImageFont.truetype(caption_font_path, max(11, int(CLOCK_SIZE * EXTRA_SCALE)))
    except OSError:
        caption_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            max(11, int(CLOCK_SIZE * EXTRA_SCALE)))

    canvas = Image.new("RGBA", (SCREEN_W, SCREEN_H), (0, 0, 0, 0))
    alpha = int(255 * max(0.0, min(1.0, CLOCK_OPACITY)))

    write_calendar(canvas)
    write_clock(canvas)
    write_status(canvas)

    if line_text:
        bg_opacity = max(0.0, min(1.0, float(overlay_settings.get("background_opacity", 0.0))))

        if bg_opacity <= 0:
            # Original look -- drop-shadowed text, no panel, dot grid
            # positioned to its left. Unchanged, so an install that never
            # touches overlay.background_opacity sees no visual change.
            left, top, width, height, lines, line_h = _caption_box(line_text, caption_font)

            cap_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            cdraw = ImageDraw.Draw(cap_img)
            for i, ln in enumerate(lines):
                cdraw.text((PAD, PAD + i * line_h), ln, font=caption_font, fill=(255, 255, 255, alpha))
            shadow_mask = cap_img.split()[-1].filter(ImageFilter.GaussianBlur(SHADOW_RADIUS))
            shadow = Image.new("RGBA", cap_img.size, (0, 0, 0, 0))
            shadow.putalpha(shadow_mask.point(lambda v: int(v * SHADOW_OPACITY)))
            composed = Image.alpha_composite(shadow, cap_img)
            canvas.alpha_composite(composed, (left, top))

            if filled_dots is not None:
                side = max(1, round(overlay_settings["dot_count"] ** 0.5))
                # Size the grid to the actual pixel height of a capital "O"
                # in this font/size -- NOT line_h, which also reserves room
                # for ascenders/descenders/line-spacing and made the grid
                # come out roughly 2x taller than any single letter.
                cap_bbox = caption_font.getbbox("O")
                cap_h = cap_bbox[3] - cap_bbox[1]
                gap = max(2, round(cap_h * 0.12))
                dot_d = max(4, round((cap_h - (side - 1) * gap) / side))
                grid = _dot_grid_image(filled_dots, dot_d, gap, rows=side, cols=side,
                                       filled_alpha=overlay_settings["filled_alpha"],
                                       unfilled_alpha=overlay_settings["unfilled_alpha"])
                grid_x = left - overlay_settings["gap_to_text"] - grid.width
                # Centered on the actual INK of the drawn text (cap_img's own
                # bbox of non-transparent pixels), not the nominal line_h/PAD
                # box -- that box reserves more room above and below the
                # glyphs than the glyphs themselves use (for line-spacing and
                # descenders), so centering on the box put the grid visibly
                # low relative to what a reader actually sees. Using the ink
                # bbox instead is correct for one line and for several.
                ink_bbox = cap_img.getbbox()
                if ink_bbox is not None:
                    ink_center = (ink_bbox[1] + ink_bbox[3]) // 2
                else:
                    ink_center = height // 2
                grid_y = top + ink_center - grid.height // 2
                canvas.alpha_composite(grid, (grid_x, grid_y))

        else:
            # Panelled look -- the caption text and the dot grid are
            # combined side by side into one content image, then the whole
            # thing gets the same rounded translucent panel as the
            # calendar/clock/status widgets, anchored to the same
            # bottom-right spot the old drop-shadowed text used.
            probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            max_width = int(SCREEN_W * MAX_WIDTH_FRAC)
            lines = _wrap_text(probe, line_text, caption_font, max_width)
            ascent, descent = caption_font.getmetrics()
            line_h = int((ascent + descent) * LINE_SPACING)
            text_w = max(1, int(max(probe.textlength(ln, font=caption_font) for ln in lines)))
            text_h = line_h * len(lines)

            text_img = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
            tdraw = ImageDraw.Draw(text_img)
            for i, ln in enumerate(lines):
                tdraw.text((0, i * line_h), ln, font=caption_font, fill=(255, 255, 255, alpha))

            grid_img = None
            if filled_dots is not None:
                side = max(1, round(overlay_settings["dot_count"] ** 0.5))
                cap_bbox = caption_font.getbbox("O")
                cap_top, cap_h = cap_bbox[1], cap_bbox[3] - cap_bbox[1]
                gap = max(2, round(cap_h * 0.12))
                dot_d = max(4, round((cap_h - (side - 1) * gap) / side))
                grid_img = _dot_grid_image(filled_dots, dot_d, gap, rows=side, cols=side,
                                           filled_alpha=overlay_settings["filled_alpha"],
                                           unfilled_alpha=overlay_settings["unfilled_alpha"])

            gap_to_text = overlay_settings["gap_to_text"]
            combined_w = (grid_img.width + gap_to_text + text_w) if grid_img is not None else text_w
            combined_h = max(text_h, grid_img.height if grid_img is not None else 0)

            content = Image.new("RGBA", (combined_w, combined_h), (0, 0, 0, 0))
            text_x = (grid_img.width + gap_to_text) if grid_img is not None else 0
            text_y = (combined_h - text_h) // 2
            content.alpha_composite(text_img, (text_x, text_y))
            if grid_img is not None:
                # Centered on the text's own ink (same reasoning as the
                # panel-off branch above), not on text_h's nominal box.
                ink_bbox = text_img.getbbox()
                if ink_bbox is not None:
                    ink_center = text_y + (ink_bbox[1] + ink_bbox[3]) // 2
                else:
                    ink_center = combined_h // 2
                grid_y = max(0, min(combined_h - grid_img.height, ink_center - grid_img.height // 2))
                content.alpha_composite(grid_img, (0, grid_y))

            pad = max(0, int(overlay_settings.get("background_padding", 20)))
            bg_color = tuple(int(v) for v in overlay_settings.get("background_color", [0, 0, 0]))
            border_color = tuple(int(v) for v in overlay_settings.get("border_color", [255, 255, 255]))
            border_width = int(overlay_settings.get("border_width", 0))
            border_opacity = max(0.0, min(1.0, float(overlay_settings.get("border_opacity", 0.9))))
            align = overlay_settings.get("align", "L")
            min_width_pct = max(0.0, float(overlay_settings.get("min_width_pct", 0.0)))
            min_width = round(SCREEN_W * min_width_pct / 100) if min_width_pct > 0 else 0
            widget = _wrap_in_panel(content, pad, bg_color, bg_opacity,
                                    border_color=border_color, border_width=border_width,
                                    border_opacity=border_opacity, min_width=min_width, justify=align)

            x, y = _anchor_xy("BR", list(CLOCK_OFFSET_PCT), (widget.width, widget.height))
            canvas.alpha_composite(widget, (x, y))

    data = canvas.tobytes()
    if data == _last_canvas_bytes:
        return
    _last_canvas_bytes = data

    tmp = OVERLAY_IMAGE + ".tmp.png"
    canvas.save(tmp)
    os.replace(tmp, OVERLAY_IMAGE)


def fetch_outside():
    try:
        with urllib.request.urlopen(WEATHER_URL, timeout=10) as r:
            data = json.load(r)
        return data["main"]["temp"]
    except Exception as e:
        print(f"Weather fetch failed: {e}", flush=True)
        return None

sensor = adafruit_dht.DHT11(board.D4)   # GPIO 4 = physical pin 7

outdoor = {"temp": None, "hum": None, "time": 0.0}   # last reading from the Pico W
pm25 = {"value": None, "time": 0.0}                  # last reading from the PMS5003
pm10 = {"value": None, "time": 0.0}                  # last reading from the PMS5003
# Last Time Machine report relayed from the Mac (mac_timemachine_status.sh via MQTT).
# "time" here is when WE last heard from it (time.monotonic()), not the backup's own
# timestamp -- that's what lets write_status() notice the Mac itself has gone quiet
# (asleep, off, launchd job stopped) even if "status" is stuck on a stale "ok".
tm_backup = {"status": None, "age_h": None, "percent": None,
            "gb_done": None, "gb_total": None, "time": 0.0}
# The Pi's own Wi-Fi, relayed from picframe3's own MQTT state topic (see
# STATE_TOPIC below) rather than read from /proc or `iw` directly here --
# picframe3's network.py (NetworkWatch) already does that polling and
# publishes the result, so this just listens for it.
wifi = {"ssid": None, "signal_percent": None, "online": None, "time": 0.0}
PM25_MAX_AGE = 5 * 60   # seconds; older than this -> treat as "no reading"

# CPCB (Indian) AQI breakpoints: (conc_lo, conc_hi, aqi_lo, aqi_hi).
# From CPCB's official AQI calculator (app.cpcbccr.com). This gives a live
# PM-only approximation -- the official AQI uses a 24-hour average and at
# least 3 pollutants; here it is instantaneous PM2.5/PM10 only.
PM25_BREAKPOINTS = [
    (0, 30, 0, 50), (31, 60, 51, 100), (61, 90, 101, 200),
    (91, 120, 201, 300), (121, 250, 301, 400), (251, 380, 401, 500),
]
PM10_BREAKPOINTS = [
    (0, 50, 0, 50), (51, 100, 51, 100), (101, 250, 101, 200),
    (251, 350, 201, 300), (351, 430, 301, 400), (431, 510, 401, 500),
]

def cpcb_sub_index(conc, breakpoints):
    if conc is None:
        return None
    conc = max(0, conc)
    for lo, hi, aqi_lo, aqi_hi in breakpoints:
        if lo <= conc <= hi:
            return round((aqi_hi - aqi_lo) / (hi - lo) * (conc - lo) + aqi_lo)
    return 500   # above the top breakpoint -- cap rather than extrapolate

def cpcb_aqi(pm25_val, pm10_val):
    candidates = [v for v in (
        cpcb_sub_index(pm25_val, PM25_BREAKPOINTS),
        cpcb_sub_index(pm10_val, PM10_BREAKPOINTS),
    ) if v is not None]
    return max(candidates) if candidates else None

def on_connect(client, userdata, flags, reason_code, properties):
    client.subscribe(TOPIC_OUTDOOR)      # re-subscribes after any reconnect
    client.subscribe(TOPIC_OUTDOOR_HUM)
    client.subscribe(TOPIC_INDOOR_PM25)
    client.subscribe(TOPIC_INDOOR_PM10)
    client.subscribe(TOPIC_TM_STATUS)
    client.subscribe(TOPIC_TM_AGE_H)
    client.subscribe(TOPIC_TM_PERCENT)
    client.subscribe(TOPIC_TM_GB_DONE)
    client.subscribe(TOPIC_TM_GB_TOTAL)
    client.subscribe(STATE_TOPIC)

def on_message(client, userdata, msg):
    if msg.topic == STATE_TOPIC:
        # PicFrame3's own retained state payload -- a JSON object, not a
        # bare number, so this has to be handled before the float parse
        # below (which would otherwise just reject it and return).
        try:
            state = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        network = state.get("network") or {}
        if "ssid" in network:
            wifi["ssid"] = network.get("ssid")
            wifi["signal_percent"] = network.get("signal_percent")
            wifi["online"] = network.get("online")
            wifi["time"] = time.monotonic()
        return
    if msg.topic == TOPIC_TM_STATUS:
        # A word, not a number -- handled before the float parse below, which
        # would otherwise reject it and silently drop every message.
        status = msg.payload.decode().strip()
        if status in ("ok", "running", "stale", "error"):
            tm_backup["status"] = status
            tm_backup["time"] = time.monotonic()
        return
    try:
        value = float(msg.payload.decode())
    except ValueError:
        return
    if msg.topic == TOPIC_OUTDOOR and -20 <= value <= 60:   # ignore bad readings
        outdoor["temp"] = value
        outdoor["time"] = time.monotonic()
    elif msg.topic == TOPIC_OUTDOOR_HUM and 0 <= value <= 100:
        outdoor["hum"] = value
    elif msg.topic == TOPIC_INDOOR_PM25 and 0 <= value <= 1000:
        pm25["value"] = value
        pm25["time"] = time.monotonic()
    elif msg.topic == TOPIC_INDOOR_PM10 and 0 <= value <= 2000:
        pm10["value"] = value
        pm10["time"] = time.monotonic()
    elif msg.topic == TOPIC_TM_AGE_H and value >= 0:
        tm_backup["age_h"] = value
        tm_backup["time"] = time.monotonic()
    elif msg.topic == TOPIC_TM_PERCENT and 0 <= value <= 100:
        tm_backup["percent"] = value
        tm_backup["time"] = time.monotonic()
    elif msg.topic == TOPIC_TM_GB_DONE and value >= 0:
        tm_backup["gb_done"] = value
        tm_backup["time"] = time.monotonic()
    elif msg.topic == TOPIC_TM_GB_TOTAL and value >= 0:
        tm_backup["gb_total"] = value
        tm_backup["time"] = time.monotonic()

STATE_TOPIC = _picframe_state_topic()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, PORT, keepalive=60)
client.loop_start()

outside = None
source = None
last_weather = -WEATHER_INTERVAL   # forces a fetch when the sensor is not available

try:
    while True:
        now = time.monotonic()
        if outdoor["temp"] is not None and now - outdoor["time"] <= OUTDOOR_MAX_AGE:
            if source != "outdoor sensor":
                print("Outside temperature: using the outdoor sensor", flush=True)
            outside, source = outdoor["temp"], "outdoor sensor"
            client.publish(TOPIC_OUT, f"{outside}", retain=True)
            if outdoor["hum"] is not None:
                client.publish(TOPIC_JSON, json.dumps(
                    {"outdoor_humidity": outdoor["hum"]}), retain=True)
        else:
            if source == "outdoor sensor":
                print("Outdoor sensor silent, falling back to OpenWeather", flush=True)
                outside, source = None, None
                last_weather = -WEATHER_INTERVAL   # fetch right away
            if now - last_weather >= WEATHER_INTERVAL:
                new = fetch_outside()
                if new is not None:
                    outside, source = new, "OpenWeather"
                    client.publish(TOPIC_OUT, f"{outside}", retain=True)
                last_weather = time.monotonic()

        try:
            temperature = sensor.temperature
            humidity = sensor.humidity
            if temperature is None or humidity is None:
                raise RuntimeError("Empty reading")

            client.publish(TOPIC_TEMP, f"{temperature}", retain=True)
            client.publish(TOPIC_HUM, f"{humidity}", retain=True)
            client.publish(TOPIC_JSON, json.dumps(
                {"temperature": temperature, "humidity": humidity}), retain=True)

            out_temp_txt = f"{outside:.0f}°C" if outside is not None else "--"
            out_hum_txt = "--"
            if source == "outdoor sensor" and outdoor["hum"] is not None:
                out_hum_txt = f"{outdoor['hum']:.0f}%"
            now_m = time.monotonic()
            pm25_fresh = pm25["value"] if (pm25["value"] is not None and now_m - pm25["time"] <= PM25_MAX_AGE) else None
            pm10_fresh = pm10["value"] if (pm10["value"] is not None and now_m - pm10["time"] <= PM25_MAX_AGE) else None
            aqi = cpcb_aqi(pm25_fresh, pm10_fresh)
            pm25_txt = f"{aqi}" if aqi is not None else "--"
            filled_dots = None
            try:
                with open(COUNTDOWN_FILE, "r", encoding="utf-8") as f:
                    filled_dots = int(f.read().strip())
            except (FileNotFoundError, ValueError):
                pass   # motion-display not running yet / countdown not written

            line = (f"Out {out_temp_txt}|Hum {out_hum_txt}|"
                    f"In {temperature}°C|Hum {humidity}%|AQI {pm25_txt}")

            # clock_extra_file is left empty: the weather line + dot grid
            # are drawn ourselves as one image (see write_overlay above),
            # not through PicFrame3's own sparse-glyph text renderer.
            write_extra("")
            write_overlay(line, filled_dots)
            print(f"Published: {line}  (dots={filled_dots})", flush=True)

        except RuntimeError as error:
            print(error.args[0], flush=True)
            time.sleep(2.0)
            continue

        time.sleep(INTERVAL)

except KeyboardInterrupt:
    pass
finally:
    sensor.exit()
    client.loop_stop()
    client.disconnect()
