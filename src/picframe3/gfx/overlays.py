"""Builders for the standard overlay layers: info bar, clock, notices.

Each returns a PIL RGBA image plus where to put it, which the renderer uploads
as a texture.  Kept separate from the renderer so the layouts can be unit
tested without a GL context.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from PIL import Image, ImageDraw

from . import textstyle
from .textstyle import TextStyle

_log = logging.getLogger(__name__)

DEFAULT_SEPARATOR = "  ·  "

#: Every caption element picframe3 knows how to write, with the wording the
#: settings page uses.  The frame writes the chosen elements in the order the
#: user puts them in, which is why ``viewer.show_text`` is a list and not a
#: set of switches.
CAPTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("title",    "Title"),
    ("caption",  "Caption or description"),
    ("name",     "File name"),
    ("date",     "Date taken"),
    ("location", "Place"),
    ("folder",   "Folder"),
    ("camera",   "Camera"),
    ("exposure", "Exposure (f-stop, speed, ISO, focal length)"),
)

#: Just the names, for validation.
CAPTION_FIELD_NAMES = tuple(name for name, _ in CAPTION_FIELDS)


#: Where the clock goes when the configured position makes no sense.
DEFAULT_POSITION = "TR"


def normalise_position(position: str, default: str = DEFAULT_POSITION) -> str:
    """One of TL TC TR BL BC BR, whatever the configuration file said.

    The layout code indexes ``position[0]`` and ``position[1]``, so an empty
    string or a stray "top-right" from a hand-edited config used to take the
    draw path down with an IndexError -- several frames after the setting was
    changed, which makes it look like anything but a bad value.  Configuration
    is validated elsewhere; the draw path still has to survive what reaches it.
    """
    value = str(position or "").strip().upper()
    if len(value) == 2 and value[0] in "TB" and value[1] in "LCR":
        return value
    _log.warning("unusable overlay position %r; using %s", position, default)
    return default


@dataclass
class Placement:
    image: Image.Image
    x: int
    y: int


def info_bar(
    lines: Sequence[str] | Sequence[Sequence[str]],
    screen: tuple[int, int],
    style: TextStyle,
    *,
    scrim_fraction: float = 0.26,
    scrim_opacity: float = 0.55,
    separator: str | None = None,
    panel_opacity: float = 0.0,
    panel_color: tuple[int, int, int] = (0, 0, 0),
    panel_padding_x: int = 28,
    panel_padding_y: int = 16,
    panel_min_width_pct: float = 0.0,
    position: str = "",
    offset_pct: tuple[float, float] = (3.0, 3.0),
) -> Placement | None:
    """Caption block, normally anchored to the bottom of the screen over a
    gradient -- unless ``position`` says otherwise (see below).

    ``lines`` is either a list of strings (one caption across the full width)
    or a list of *two* such lists, in which case each caption is centred under
    its own half of a portrait pair -- two photographs side by side deserve two
    captions, not one describing only the left-hand picture.

    ``panel_opacity`` (0 by default -- no change from before this existed)
    additionally wraps each column's own rendered text in a rounded
    translucent panel sized to that text, the same "widget on its own
    backdrop" look as the calendar/clock/status overlay widgets, rather than
    the full-width gradient ``scrim_opacity`` draws. The two can be used
    together; the panel is applied first, so the band height/centring below
    sees the panel-wrapped image as the "body" and lays it out exactly as it
    would the bare text. ``panel_min_width_pct`` (0 by default -- the panel
    then just hugs the text, as before this existed) stretches the panel to
    at least that percent of the screen's width, with the text kept aligned
    inside it per ``style.justify``; it only ever widens the panel, never the
    text wrapping itself.

    ``position`` ("" by default, the original full-width bottom band) can
    instead be one of TL/TC/TR/BL/BC/BR, same scheme as the clock widget, to
    place a *single* caption anywhere on screen rather than always at the
    bottom -- ``offset_pct`` is its inset from that corner. This only applies
    to the unpaired case: a portrait pair's two captions keep the full-width
    bottom band regardless, since "a corner" does not make sense split across
    two side-by-side pictures.
    """
    columns = _as_columns(lines)
    joiner = DEFAULT_SEPARATOR if separator is None else separator
    texts = [joiner.join(t for t in col if t) for col in columns]
    if not any(texts):
        return None

    w, h = screen
    paired = len(texts) > 1
    column_w = (w // len(texts)) - style.margin_x
    bodies = [
        textstyle.render_text(text, style, max_width=max(80, column_w),
                              align="C" if paired else None) if text else None
        for text in texts
    ]
    if panel_opacity > 0:
        min_width = round(w * panel_min_width_pct / 100) if panel_min_width_pct > 0 else 0
        bodies = [
            _panel_wrap(b, (panel_padding_x, panel_padding_y), panel_color, panel_opacity,
                       min_width=min_width, justify=style.justify)
            if b is not None else None
            for b in bodies
        ]
    rendered = [b for b in bodies if b is not None]
    if not rendered:
        return None

    if not paired and position:
        body = rendered[0]
        pos = normalise_position(position)
        return Placement(body, *_anchor_xy(pos, offset_pct, (body.width, body.height), screen))

    band_h = min(h, max(b.height for b in rendered) + style.margin_y * 2)
    layer = Image.new("RGBA", (w, band_h), (0, 0, 0, 0))
    if scrim_opacity > 0:
        layer = Image.alpha_composite(
            layer,
            textstyle.scrim((w, band_h), height_fraction=1.0, opacity=scrim_opacity),
        )

    for index, body in enumerate(bodies):
        if body is None:
            continue
        if paired:
            centre = w * (2 * index + 1) // (2 * len(texts))
            x = centre - body.width // 2
        else:
            justify = style.justify.upper()
            if justify == "C":
                x = (w - body.width) // 2
            elif justify == "R":
                x = w - body.width - style.margin_x
            else:
                x = style.margin_x
        layer.alpha_composite(body, (max(0, min(x, w - body.width)),
                                     (band_h - body.height) // 2))
    return Placement(layer, 0, h - band_h)


def _panel_wrap(content: Image.Image, pad: int | tuple[int, int], color: tuple[int, int, int],
                opacity: float, *, min_width: int = 0, justify: str = "L") -> Image.Image:
    """Rounded translucent panel sized to `content` plus `pad` on every
    side, with `content` composited on top -- the overlays.py counterpart of
    dht_mqtt.py's _wrap_in_panel, kept as its own small copy here rather than
    a shared import since the two live in separate, independently deployed
    files.

    `pad` is either a single number (same margin on every side) or an
    (x, y) pair -- horizontal margin, vertical margin -- for a caption like
    a date/location line that is wide but only one or two lines tall, where
    an equal pixel margin on every side reads as barely-there sideways and
    oversized top and bottom.

    `min_width` (0 by default) stretches the panel to at least that many
    pixels wide when the text itself is narrower, with `content` kept
    aligned inside per `justify` (L/C/R) rather than centred unconditionally
    -- a left-justified caption gets a wide panel with its text still
    hugging the left edge, not floating in the middle of it.
    """
    pad_x, pad_y = pad if isinstance(pad, (tuple, list)) else (pad, pad)
    pad_x, pad_y = max(0, int(pad_x)), max(0, int(pad_y))
    inner_w = max(content.width, max(0, min_width - pad_x * 2))
    panel_w = inner_w + pad_x * 2
    panel_h = content.height + pad_y * 2
    panel = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
    alpha = round(255 * max(0.0, min(1.0, opacity)))
    # Scaled off the panel's own shorter side, not the padding -- tying it
    # to padding made the corners barely round at typical padding values
    # even on a big panel. Capped so a very wide/tall panel doesn't turn
    # into a stadium shape.
    radius = max(8, min(60, round(min(panel_w, panel_h) * 0.28)))
    ImageDraw.Draw(panel).rounded_rectangle(
        [0, 0, panel_w - 1, panel_h - 1], radius=radius, fill=(*color, alpha))
    justify = justify.upper()
    if justify == "C":
        cx = pad_x + (inner_w - content.width) // 2
    elif justify == "R":
        cx = pad_x + (inner_w - content.width)
    else:
        cx = pad_x
    panel.alpha_composite(content, (cx, pad_y))
    return panel


def _anchor_xy(position: str, offset_pct: tuple[float, float],
               size: tuple[int, int], screen: tuple[int, int]) -> tuple[int, int]:
    """Top-left (x, y) to place a widget of `size` inset from the named
    corner -- TL/TC/TR/BL/BC/BR -- by offset_pct percent of the screen.
    `position` must already be normalised (normalise_position)."""
    w, h = screen
    bw, bh = size
    dx = int(w * offset_pct[0] / 100)
    dy = int(h * offset_pct[1] / 100)
    vert, horiz = position[0], position[1]
    y = dy if vert == "T" else h - bh - dy
    if horiz == "L":
        x = dx
    elif horiz == "C":
        x = (w - bw) // 2
    else:
        x = w - bw - dx
    return x, y


def _as_columns(lines) -> list[list[str]]:
    items = list(lines or [])
    if items and all(isinstance(item, (list, tuple)) for item in items):
        return [list(col) for col in items][:2]
    return [[str(item) for item in items]]


def clock(
    screen: tuple[int, int],
    style: TextStyle,
    *,
    fmt: str = "%H:%M",
    extra: str = "",
    position: str = "TR",
    offset_pct: tuple[float, float] = (3.0, 3.0),
    now: float | None = None,
    extra_scale: float = 0.34,
) -> Placement | None:
    """Large time readout.  ``position`` is one of TL TC TR BL BC BR.

    ``extra`` is a second line — a weather summary, a countdown, whatever
    writes to the file — and it is set much smaller than the time.  Matching
    the clock's size, as picframe did, puts a sentence across the picture in
    120-point type.
    """
    position = normalise_position(position)
    align = _align_for(position)
    text = datetime.fromtimestamp(now or time.time()).strftime(fmt)
    w, h = screen
    face = textstyle.render_text(text, style, max_width=w, align=align)
    if face is None:
        return None

    body = face
    if extra:
        small = replace(style, size=max(11, int(style.size * extra_scale)))
        caption = textstyle.render_text(extra, small, max_width=int(w * 0.45),
                                        align=align)
        if caption is not None:
            gap = max(2, style.size // 12)
            width = max(face.width, caption.width)
            body = Image.new("RGBA", (width, face.height + gap + caption.height),
                             (0, 0, 0, 0))
            body.alpha_composite(face, (_offset(width, face.width, align), 0))
            body.alpha_composite(caption,
                                 (_offset(width, caption.width, align),
                                  face.height + gap))

    dx = int(w * offset_pct[0] / 100)
    dy = int(h * offset_pct[1] / 100)
    horiz, vert = position[1].upper(), position[0].upper()
    if horiz == "L":
        x = dx
    elif horiz == "C":
        x = (w - body.width) // 2
    else:
        x = w - body.width - dx
    y = dy if vert == "T" else h - body.height - dy
    return Placement(body, x, y)


def _offset(total: int, part: int, align: str) -> int:
    if align == "R":
        return total - part
    if align == "C":
        return (total - part) // 2
    return 0


def _align_for(position: str) -> str:
    return {"L": "L", "C": "C", "R": "R"}.get(normalise_position(position)[1], "L")


def notice(
    text: str,
    screen: tuple[int, int],
    style: TextStyle,
) -> Placement | None:
    """Transient centred message: PAUSED, display off, errors."""
    body = textstyle.render_text(text, style, max_width=int(screen[0] * 0.8), align="C")
    if body is None:
        return None
    return Placement(body, (screen[0] - body.width) // 2, (screen[1] - body.height) // 2)


def format_info_lines(
    meta,
    fields: Iterable[str],
    *,
    date_format: str = "%-d %B %Y",
    location: str | None = None,
    paused: bool = False,
) -> list[str]:
    """Pick and format the metadata fields the user asked to see."""
    import os

    out: list[str] = []
    for field_name in fields:
        value: str | None = None
        if field_name == "title":
            value = meta.title
        elif field_name == "caption":
            value = meta.caption
        elif field_name == "name":
            value = os.path.splitext(os.path.basename(meta.path))[0]
        elif field_name == "folder":
            value = os.path.basename(os.path.dirname(meta.path))
        elif field_name == "date" and meta.taken_at:
            try:
                value = datetime.fromtimestamp(meta.taken_at).strftime(date_format)
            except ValueError:  # %-d is not portable everywhere
                value = datetime.fromtimestamp(meta.taken_at).strftime("%d %B %Y")
        elif field_name == "location":
            value = location
        elif field_name == "camera":
            parts = [p for p in (meta.make, meta.model) if p]
            if parts:
                # "Canon Canon EOS R6" -> "Canon EOS R6"
                if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
                    parts = [parts[1]]
                value = " ".join(parts)
        elif field_name == "exposure":
            bits = []
            if meta.f_number:
                bits.append(f"f/{meta.f_number:g}")
            if meta.exposure_time:
                bits.append(meta.exposure_time)
            if meta.iso:
                bits.append(f"ISO {meta.iso}")
            if meta.focal_length:
                bits.append(f"{meta.focal_length:g}mm")
            value = " ".join(bits) or None
        if value:
            out.append(value.strip())
    if paused:
        out.append("PAUSED")
    return out
