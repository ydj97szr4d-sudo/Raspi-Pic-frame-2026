"""The frame's own maker's mark: a small animated badge drawn in a corner
of the screen itself (config.brand) -- not the web favicon, which lives
under ``web/`` and is a separate, static asset.

Five short cycles are built in -- the outline and the mark forming, holding
a moment, and leaving, each a different mechanic (dissolving into dots,
printing in by scanline, popping in with a spring and zooming out, a
glitchy slice-resolve, blurring into and out of focus). ``BrandAnimator``
picks at random, each time a cycle finishes, among whichever the owner has
turned on in ``config.brand.styles``, so the badge does not look the same
every time someone glances at the corner of the frame.

Each cycle's frames are built once per (style, size) and cached -- building
them is a few hundred milliseconds of PIL work, playing them back is just
indexing a list, which is what actually happens every tick of the render
loop.
"""

from __future__ import annotations

import math
import random
import time
from functools import lru_cache
from importlib import resources

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .overlays import _anchor_xy, normalise_position

#: Every animation style the badge knows, with the wording the settings
#: page uses. ``config.brand.styles`` names a subset of the first column.
BRAND_STYLES: tuple[tuple[str, str], ...] = (
    ("dots", "Dissolving dots"),
    ("scanline", "Scanline print-in"),
    ("pop", "Iris + spring pop"),
    ("glitch", "Glitch resolve"),
    ("focus", "Blur into focus"),
)
BRAND_STYLE_NAMES = tuple(name for name, _ in BRAND_STYLES)

#: Frames per second the badge itself plays at -- independent of (and much
#: lower than) the display's own fps_limit, since the mark is a small image
#: and does not need a full-screen refresh rate to read as smooth motion.
_FPS = 12
_INK = (238, 239, 242)


@lru_cache(maxsize=1)
def _bird_source() -> Image.Image:
    """The cleaned bird mark, alpha-matted, at its native resolution."""
    with resources.files(__package__).joinpath("assets/brand-mark.png").open("rb") as fh:
        return Image.open(fh).convert("RGBA").copy()


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _back_out(t: float, overshoot: float = 1.9) -> float:
    t = t - 1
    return t * t * ((overshoot + 1) * t + overshoot) + 1


def _mul_alpha(img: Image.Image, m: float) -> Image.Image:
    if m >= 0.999:
        return img
    a = img.split()[3].point(lambda v: int(v * max(0.0, m)))
    out = img.copy()
    out.putalpha(a)
    return out


class _Geometry:
    """Everything that only depends on the badge's pixel size, built once
    per size and shared by every style's frame builder."""

    def __init__(self, out: int):
        self.out = out
        bird = _bird_source()
        target_w = out * 0.60
        scale = target_w / bird.width
        rw, rh = max(1, int(bird.width * scale)), max(1, int(bird.height * scale))
        self.raster = bird.resize((rw, rh), Image.LANCZOS)
        self.rw, self.rh = rw, rh
        self.cx_off = int(out * 0.5 - rw / 2)
        self.cy_off = int(out * 0.52 - rh / 2)
        self.outline = self._build_outline()

    def _build_outline(self) -> Image.Image:
        out = self.out
        radius = int(out * 0.22)
        stroke_w = max(2, int(out * 0.028))
        inset = stroke_w // 2 + 2
        img = Image.new("RGBA", (out, out), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle(
            [inset, inset, out - 1 - inset, out - 1 - inset],
            radius=max(1, radius - inset), outline=(90, 92, 96, 255), width=stroke_w)
        return img

    def blank(self) -> Image.Image:
        return Image.new("RGBA", (self.out, self.out), (0, 0, 0, 0))

    def paste_centered(self, canvas: Image.Image, img: Image.Image, *,
                        scale: float = 1.0, dx: float = 0, dy: float = 0,
                        rot: float = 0.0) -> None:
        if img.width == 0 or img.height == 0:
            return
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        scaled = img.resize((w, h), Image.LANCZOS) if scale != 1.0 else img
        if rot:
            scaled = scaled.rotate(rot, resample=Image.BICUBIC, expand=True)
        x = self.cx_off + self.rw / 2 - scaled.width / 2 + dx
        y = self.cy_off + self.rh / 2 - scaled.height / 2 + dy
        canvas.alpha_composite(scaled, (int(x), int(y)))

    def with_outline(self, canvas: Image.Image, mask: Image.Image | None = None) -> None:
        om = self.outline.split()[3]
        if mask is not None:
            om = ImageChops.multiply(om, mask)
        ol = self.outline.copy()
        ol.putalpha(om)
        canvas.alpha_composite(ol)

    def outline_sweep_mask(self, p: float, start_angle: float = -math.pi / 2) -> Image.Image:
        out = self.out
        yy, xx = np.mgrid[0:out, 0:out]
        cx = cy = out / 2
        raw = np.arctan2(yy - cy, xx - cx)
        ang = np.mod(raw - start_angle, 2 * math.pi)
        keep = ang <= p * 2 * math.pi
        return Image.fromarray((keep * 255).astype(np.uint8), "L")

    def outline_iris_mask(self, p: float) -> Image.Image:
        out = self.out
        yy, xx = np.mgrid[0:out, 0:out]
        cx = cy = out / 2
        maxr = math.hypot(out, out) / 2 * 1.05
        r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        keep = r <= p * maxr
        return Image.fromarray((keep * 255).astype(np.uint8), "L")

    def full_mask(self) -> Image.Image:
        r = self.raster
        m = Image.new("L", (self.out, self.out), 0)
        m.paste(r.split()[3], (self.cx_off, self.cy_off))
        return m


def _n(dur: float) -> int:
    return max(2, int(_FPS * dur))


# -- style: dissolving dots -------------------------------------------------

def _dot_particles(geo: _Geometry, rng: random.Random) -> list:
    alpha = np.asarray(geo.raster.split()[3])
    ys, xs = np.where(alpha > 25)
    if len(xs) == 0:
        return []
    weights = alpha[ys, xs].astype(np.float64)
    weights /= weights.sum()
    n = min(260, len(xs))
    idx = np.random.default_rng(rng.randrange(1 << 30)).choice(
        len(xs), size=n, replace=False, p=weights)
    particles = []
    for i in idx:
        x, y = int(xs[i]), int(ys[i])
        ang = rng.uniform(0, 2 * math.pi)
        dist = rng.uniform(0.10, 0.30) * max(geo.rw, geo.rh)
        dx, dy = math.cos(ang) * dist, math.sin(ang) * dist
        stagger = rng.uniform(0.0, 0.55)
        size = rng.uniform(0.8, 2.2)
        particles.append((x, y, dx, dy, stagger, size))
    return particles


def _draw_particles(geo: _Geometry, canvas: Image.Image, t: float, mode: str,
                     particles: list) -> None:
    d = ImageDraw.Draw(canvas, "RGBA")
    for (x, y, dx, dy, stagger, size) in particles:
        dur = 1.0 - stagger
        p = 0.0 if dur <= 0 else max(0.0, min(1.0, (t - stagger) / dur))
        e = _smoothstep(p)
        if mode == "appear":
            px_, py_ = x - dx * (1 - e), y - dy * (1 - e)
            a = e
        else:
            px_, py_ = x + dx * e, y + dy * e
            a = 1 - e
        if a <= 0.01:
            continue
        rx, ry = geo.cx_off + px_, geo.cy_off + py_
        d.ellipse([rx - size, ry - size, rx + size, ry + size],
                  fill=(*_INK, int(255 * a)))


def _cycle_dots(geo: _Geometry, rng: random.Random) -> list[Image.Image]:
    particles = _dot_particles(geo, rng)
    frames = []
    for i in range(n := _n(1.1)):
        frames.append(("outline", _smoothstep(i / (n - 1)), None))
    for i in range(n := _n(1.4)):
        frames.append(("dots", i / (n - 1), "appear", particles))
    for _ in range(_n(1.0)):
        frames.append(("raster", 1.0))
    for i in range(n := _n(1.4)):
        frames.append(("dots", i / (n - 1), "dissolve", particles))
    frames += [("blank",)] * _n(0.35)
    out = []
    for f in frames:
        c = geo.blank()
        if f[0] == "outline":
            geo.with_outline(c, geo.outline_sweep_mask(f[1]))
        elif f[0] == "dots":
            geo.with_outline(c)
            _draw_particles(geo, c, f[1], f[2], f[3])
        elif f[0] == "raster":
            c.paste(geo.raster, (geo.cx_off, geo.cy_off), geo.raster)
            geo.with_outline(c)
        out.append(c)
    return out


# -- style: scanline print-in ------------------------------------------------

def _cycle_scanline(geo: _Geometry, rng: random.Random) -> list[Image.Image]:
    out = []
    for i in range(n := _n(1.1)):
        p = _smoothstep(i / (n - 1))
        c = geo.blank()
        geo.with_outline(c, geo.outline_sweep_mask(p))
        out.append(c)
    for i in range(n := _n(1.4)):
        t = i / (n - 1)
        rows = max(1, int(geo.rh * t))
        strip = geo.raster.crop((0, 0, geo.rw, rows))
        c = geo.blank()
        c.paste(strip, (geo.cx_off, geo.cy_off), strip)
        if rows < geo.rh:
            ImageDraw.Draw(c).line(
                [(geo.cx_off, geo.cy_off + rows), (geo.cx_off + geo.rw, geo.cy_off + rows)],
                fill=(255, 255, 255, 160), width=2)
        geo.with_outline(c)
        out.append(c)
    for _ in range(_n(1.0)):
        c = geo.blank()
        c.paste(geo.raster, (geo.cx_off, geo.cy_off), geo.raster)
        geo.with_outline(c)
        out.append(c)
    strips = 9
    sh = geo.rh / strips
    for i in range(n := _n(1.3)):
        t = i / (n - 1)
        c = geo.blank()
        for s in range(strips):
            y0, y1 = int(s * sh), int((s + 1) * sh) if s < strips - 1 else geo.rh
            strip = geo.raster.crop((0, y0, geo.rw, y1))
            direction = 1 if s % 2 == 0 else -1
            dx = direction * t * t * geo.out * 0.9
            a = max(0.0, 1 - t * 1.15)
            c.alpha_composite(_mul_alpha(strip, a), (geo.cx_off + int(dx), geo.cy_off + y0))
        geo.with_outline(c)
        out.append(c)
    out += [geo.blank()] * _n(0.35)
    return out


# -- style: iris + spring pop ------------------------------------------------

def _cycle_pop(geo: _Geometry, rng: random.Random) -> list[Image.Image]:
    out = []
    for i in range(n := _n(1.0)):
        p = i / (n - 1)
        c = geo.blank()
        geo.with_outline(c, geo.outline_iris_mask(_smoothstep(p)))
        out.append(c)
    for i in range(n := _n(1.0)):
        t = i / (n - 1)
        scale = max(0.0, _back_out(t))
        alpha = _smoothstep(min(1.0, t / 0.45))
        c = geo.blank()
        geo.paste_centered(c, _mul_alpha(geo.raster, alpha), scale=scale)
        geo.with_outline(c)
        out.append(c)
    for i in range(n := _n(1.3)):
        t = i / (n - 1)
        wob = math.sin(t * 2 * math.pi * 1.4) * 4.0
        bob = math.sin(t * 2 * math.pi * 1.4 * 2) * geo.out * 0.01
        c = geo.blank()
        geo.paste_centered(c, geo.raster, rot=wob, dy=bob)
        geo.with_outline(c)
        out.append(c)
    for i in range(n := _n(1.0)):
        t = i / (n - 1)
        scale = max(0.001, 1.0 - t * t)
        alpha = max(0.0, 1 - t * 1.2)
        c = geo.blank()
        geo.paste_centered(c, _mul_alpha(geo.raster, alpha), scale=scale, rot=t * 25)
        geo.with_outline(c)
        out.append(c)
    out += [geo.blank()] * _n(0.35)
    return out


# -- style: glitch resolve ---------------------------------------------------

def _cycle_glitch(geo: _Geometry, rng: random.Random) -> list[Image.Image]:
    out = []
    for i in range(n := _n(0.9)):
        p = _smoothstep(i / (n - 1))
        c = geo.blank()
        geo.with_outline(c, geo.outline_sweep_mask(p, start_angle=math.pi / 2))
        out.append(c)
    bands = 14
    bh = geo.rh / bands
    offsets = [rng.uniform(-1, 1) * geo.out * 0.5 for _ in range(bands)]
    for i in range(n := _n(1.1)):
        t = i / (n - 1)
        decay = max(0.0, 1 - t * 1.3)
        c = geo.blank()
        for b in range(bands):
            y0, y1 = int(b * bh), int((b + 1) * bh) if b < bands - 1 else geo.rh
            band = geo.raster.crop((0, y0, geo.rw, y1))
            jitter = math.sin(t * 50 + b) * decay * geo.out * 0.02
            dx = offsets[b] * decay + jitter
            flick = min(1.0, 0.55 + 0.45 * t) if decay > 0.05 else 1.0
            c.alpha_composite(_mul_alpha(band, flick), (geo.cx_off + int(dx), geo.cy_off + y0))
        geo.with_outline(c)
        out.append(c)
    for _ in range(_n(1.0)):
        c = geo.blank()
        c.paste(geo.raster, (geo.cx_off, geo.cy_off), geo.raster)
        geo.with_outline(c)
        out.append(c)
    for i in range(n := _n(1.3)):
        t = i / (n - 1)
        block = max(1, int(1 + t * 14))
        w2, h2 = max(1, geo.rw // block), max(1, geo.rh // block)
        small = geo.raster.resize((w2, h2), Image.NEAREST).resize((geo.rw, geo.rh), Image.NEAREST)
        a = max(0.0, 1 - max(0.0, t - 0.15) / 0.85)
        small = _mul_alpha(small, a)
        c = geo.blank()
        c.paste(small, (geo.cx_off, geo.cy_off), small)
        geo.with_outline(c)
        out.append(c)
    out += [geo.blank()] * _n(0.35)
    return out


# -- style: blur into focus --------------------------------------------------

def _cycle_focus(geo: _Geometry, rng: random.Random) -> list[Image.Image]:
    out = []
    for i in range(n := _n(1.1)):
        p = _smoothstep(i / (n - 1))
        c = geo.blank()
        geo.with_outline(c, geo.outline_sweep_mask(p))
        out.append(c)
    for i in range(n := _n(1.4)):
        t = i / (n - 1)
        blur = (1 - t) * 6.0
        alpha = 0.35 + 0.65 * _smoothstep(t)
        img = _mul_alpha(geo.raster, alpha)
        if blur > 0.15:
            img = img.filter(ImageFilter.GaussianBlur(blur))
        c = geo.blank()
        c.paste(img, (geo.cx_off, geo.cy_off), img)
        geo.with_outline(c)
        out.append(c)
    glow = geo.raster.filter(ImageFilter.GaussianBlur(5))
    for i in range(n := _n(1.3)):
        t = i / (n - 1)
        glow_a = 0.18 + 0.18 * (0.5 + 0.5 * math.sin(t * 2 * math.pi))
        c = geo.blank()
        c.paste(_mul_alpha(glow, glow_a), (geo.cx_off, geo.cy_off), _mul_alpha(glow, glow_a))
        c.alpha_composite(geo.raster, (geo.cx_off, geo.cy_off))
        geo.with_outline(c)
        out.append(c)
    for i in range(n := _n(1.2)):
        t = i / (n - 1)
        blur = t * 7.0
        alpha = max(0.0, 1 - t)
        img = _mul_alpha(geo.raster, alpha)
        if blur > 0.15:
            img = img.filter(ImageFilter.GaussianBlur(blur))
        c = geo.blank()
        c.paste(img, (geo.cx_off, geo.cy_off), img)
        geo.with_outline(c)
        out.append(c)
    out += [geo.blank()] * _n(0.35)
    return out


_BUILDERS = {
    "dots": _cycle_dots,
    "scanline": _cycle_scanline,
    "pop": _cycle_pop,
    "glitch": _cycle_glitch,
    "focus": _cycle_focus,
}


def static_frame(size: int, opacity: float = 1.0) -> Image.Image:
    """The badge with no motion at all: outline plus the mark, settled."""
    geo = _Geometry(max(16, size))
    c = geo.blank()
    c.paste(geo.raster, (geo.cx_off, geo.cy_off), geo.raster)
    geo.with_outline(c)
    return _mul_alpha(c, opacity)


def placement(position: str, offset_pct: tuple[float, float], size: int,
              screen: tuple[int, int]) -> tuple[int, int]:
    """Top-left (x, y) to place a `size`x`size` badge image on `screen`."""
    pos = normalise_position(position)
    return _anchor_xy(pos, offset_pct, (size, size), screen)


class BrandAnimator:
    """Plays one style's cycle at a time, choosing the next at random from
    whichever styles are enabled, each time the current one finishes.

    Frame lists are built once per (style, size) and cached on the instance;
    ``frame(now)`` is just an index lookup plus, when ``opacity`` < 1, a
    cheap alpha multiply, so it is fine to call every render-loop tick.
    """

    def __init__(self, styles: list[str], size: int, opacity: float = 1.0,
                 seed: int | None = None):
        self.styles = [s for s in styles if s in _BUILDERS] or list(BRAND_STYLE_NAMES)
        self.size = max(16, int(size))
        self.opacity = max(0.0, min(1.0, opacity))
        self._geo = _Geometry(self.size)
        self._rng = random.Random(seed)
        self._cache: dict[str, list[Image.Image]] = {}
        self._style: str | None = None
        self._frames: list[Image.Image] = []
        self._started_at = 0.0
        self._last_idx = -1

    def _frames_for(self, style: str) -> list[Image.Image]:
        cached = self._cache.get(style)
        if cached is None:
            cached = _BUILDERS[style](self._geo, self._rng)
            self._cache[style] = cached
        return cached

    def _advance_style(self, now: float) -> None:
        choices = self.styles
        if len(choices) > 1 and self._style in choices:
            choices = [s for s in choices if s != self._style] or self.styles
        self._style = self._rng.choice(choices)
        self._frames = self._frames_for(self._style)
        self._started_at = now
        self._last_idx = -1

    def frame(self, now: float) -> tuple[Image.Image | None, bool]:
        """The image to show right now, and whether it differs from the
        last call (so the caller knows whether a redraw is actually
        needed)."""
        if not self.styles:
            return None, False
        if not self._frames or (now - self._started_at) >= len(self._frames) / _FPS:
            self._advance_style(now)
        idx = min(len(self._frames) - 1,
                  int((now - self._started_at) * _FPS))
        changed = idx != self._last_idx
        self._last_idx = idx
        image = self._frames[idx]
        if self.opacity < 0.999:
            image = _mul_alpha(image, self.opacity)
        return image, changed

    def placement(self, position: str, offset_pct: tuple[float, float],
                  screen: tuple[int, int]) -> tuple[int, int]:
        return placement(position, offset_pct, self.size, screen)
