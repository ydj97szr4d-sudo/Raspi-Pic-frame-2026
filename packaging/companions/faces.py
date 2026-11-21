"""Clock faces for the picture frame's "Face" tile.

These are the same faces the JBLPie's Acton LCD shows (the twelve originals from the
moode-nowplaying app plus Paper, Glass, Sunset, Stacked, Rugged, Night and Banner), drawn
here at any size into a transparent RGBA tile for dht_mqtt.py to composite onto the
overlay. Adapted from the JBLPie's clockweather.py: weather, sensors, Wi-Fi and the audio
meter are gone (the frame already has its own weather widgets) and the Banner lettering is
drawn still, not animated, because the overlay is redrawn only every few seconds.

Public API:
    FACES                              {id: {"label": ..., "o": {...preset overrides...}}}
    render_face(face_id, w, h, now, opts) -> PIL RGB image (w x h)

`opts` (all optional) overrides the preset: clock_24h, show_seconds, show_date, style,
bg, bg2, fg, accent (RGB lists), analog_thickness, analog_markings, analog_numerals,
brightness (20-100), banner_text, banner_font, banner_size, banner_color, font_path.
"""
import datetime
import math
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

CFG = {
    "style": "flat", "clock_24h": False, "show_seconds": False, "face": "analog", "dial": "analog",
    "mono": False, "weekday": None, "font_family": "sans", "clock_font": "sans",
    "bg": [8, 10, 16], "bg2": [26, 30, 52], "fg": [232, 234, 237], "accent": [29, 185, 84],
    "weather_blue": [120, 180, 255], "brightness": 100, "show_date": False, "show_weather": False,
    "analog_thickness": 100, "analog_markings": "full", "analog_numerals": True,
    "banner_text": "Welcome Home", "banner_font": "auto", "banner_size": 100, "banner_color": [214, 170, 72],
    "units": "C",
}
_BASE = dict(CFG)
sensors = {"ok": True}                      # the Rugged dial's status lights (antenna = sensors live)


def audio_running():                        # ... and "audio playing"; the frame passes it in as opts["playing"]
    return bool(CFG.get("_playing"))

_SHORT = {"sans": "DejaVu Sans", "serif": "DejaVu Serif", "mono": "DejaVu Sans Mono",
          "condensed": "DejaVu Sans Condensed", "noto": "Noto Sans", "myriad": "Myriad Pro"}

_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

_famcache = {}

_fcache = {}

_SEG = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd",
        "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcfgd", "-": "g", " ": ""}

RUGGED_LIGHT = {"until": 0.0}

_CURSIVE = ["Amplify", "Z003", "URW Chancery L", "TeX Gyre Chorus", "Dancing Script", "Great Vibes", "Pacifico", "Lobster"]

_banner_cache = {"mesh": {}, "font": None, "text": {}}

_legacy = {"mod": None, "err": None, "faces": {}}

def col(key):
    v = CFG.get(key) or [255, 255, 255]
    return (int(v[0]), int(v[1]), int(v[2]))

def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def family_file(fam, bold):
    """Resolve a family name to (file, face-index) via fontconfig. Falls back to DejaVu."""
    fam = _SHORT.get(fam, fam or "DejaVu Sans")
    key = (fam, bool(bold))
    if key in _famcache:
        return _famcache[key]
    pat = "%s:style=%s" % (fam, "Bold" if bold else "Regular")
    file, idx = (_DEJAVU, 0)
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}\t%{index}", pat],
                             capture_output=True, text=True, timeout=4).stdout.strip()
        if "\t" in out:
            f, i = out.split("\t", 1)
            if f:
                file, idx = f, int(i or 0)
    except Exception:
        pass
    _famcache[key] = (file, idx)
    return file, idx

def font(size, bold=False, fam=None):
    size = max(6, int(size))
    fam = fam or CFG.get("font_family", "sans")
    if CFG.get("_font_path") and fam == CFG.get("font_family", "sans"):
        k2 = (size, bold, "path:" + CFG["_font_path"])
        if k2 not in _fcache:
            try: _fcache[k2] = ImageFont.truetype(CFG["_font_path"], size)
            except Exception: _fcache[k2] = None
        if _fcache[k2] is not None: return _fcache[k2]
    key = (size, bold, fam)
    if key in _fcache:
        return _fcache[key]
    file, idx = family_file(fam, bold)
    try:
        f = ImageFont.truetype(file, size, index=idx)
    except Exception:
        try:
            f = ImageFont.truetype(_DEJAVU, size)
        except Exception:
            f = ImageFont.load_default()
    _fcache[key] = f
    return f

def tw_(dr, s, fnt):
    b = dr.textbbox((0, 0), s, font=fnt)
    return b[2] - b[0], b[3] - b[1]

def fit_text(dr, s, fnt, maxw):
    """Trim s with a trailing ellipsis until it fits in maxw pixels."""
    if tw_(dr, s, fnt)[0] <= maxw:
        return s
    while s and tw_(dr, s + "…", fnt)[0] > maxw:
        s = s[:-1]
    return (s + "…") if s else s

def clock_strings(now):
    if CFG.get("clock_24h"):
        t = now.strftime("%H:%M:%S") if CFG.get("show_seconds") else now.strftime("%H:%M")
        return t, ""
    core = "%I:%M:%S" if CFG.get("show_seconds") else "%I:%M"
    return now.strftime(core).lstrip("0"), now.strftime("%p").lower()

def glass_background(w, h):
    """Gradient + two soft colour blooms - gives the frosted panel something to refract."""
    bg = _gradient(w, h, col("bg"), col("bg2"))
    blob = Image.new("RGB", (w, h), (0, 0, 0))
    bd = ImageDraw.Draw(blob)
    bd.ellipse([-w*0.15, -h*0.25, w*0.5, h*0.55], fill=col("accent"))
    bd.ellipse([w*0.55, h*0.5, w*1.15, h*1.3], fill=col("weather_blue"))
    blob = blob.filter(ImageFilter.GaussianBlur(int(w * 0.11)))
    return Image.blend(bg, blob, 0.30)

def frost(base, box, radius, tint=(255, 255, 255), tint_a=0.12, blur=10):
    """Frost a rounded region: blur what's behind it, add a light tint, soft border + top sheen."""
    x0, y0, x1, y1 = [int(v) for v in box]
    region = base.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(blur))
    region = Image.blend(region, Image.new("RGB", region.size, tint), tint_a)
    # top-down sheen (brighter at the top edge)
    sh = np.linspace(0.16, 0.0, region.size[1], dtype=np.float32)[:, None, None]
    ra = np.asarray(region, dtype=np.float32)
    ra = ra + (255 - ra) * sh
    region = Image.fromarray(np.clip(ra, 0, 255).astype(np.uint8), "RGB")
    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, x1 - x0 - 1, y1 - y0 - 1], radius=radius, fill=255)
    base.paste(region, (x0, y0), mask)
    ImageDraw.Draw(base, "RGBA").rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=radius,
                                                   outline=(255, 255, 255, 80), width=2)

def gradient_background(w, h):
    """Diagonal two-colour gradient with a soft warm bloom (the "Gradient" face)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    t = np.clip((xx / w) * 0.55 + (yy / h) * 0.75, 0, 1)[:, :, None] / 1.3
    a = np.array(col("bg"), np.float32); b = np.array(col("bg2"), np.float32)
    img = Image.fromarray((a * (1 - t) + b * t).astype(np.uint8), "RGB")
    blob = Image.new("RGB", (w, h), (0, 0, 0))
    ImageDraw.Draw(blob).ellipse([w * 0.45, -h * 0.3, w * 1.2, h * 0.6], fill=col("accent"))
    return Image.blend(img, blob.filter(ImageFilter.GaussianBlur(int(w * 0.12))), 0.22)

def _gradient(w, h, c1, c2):
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    a = np.array(c1, dtype=np.float32); b = np.array(c2, dtype=np.float32)
    col_h = (a * (1 - t) + b * t).astype(np.uint8)           # h x 3
    return Image.fromarray(np.repeat(col_h[:, None, :], w, axis=1), "RGB")

def draw_analog(dr, cx, cy, R, now, fill_dial=True):
    dial = col("bg"); ink = col("fg"); acc = col("accent")
    track = mix(col("bg"), col("fg"), 0.30)
    hourc = mix(col("bg"), col("fg"), 0.82)
    thick = max(50, min(200, int(CFG.get("analog_thickness", 100)))) / 100.0
    markings = CFG.get("analog_markings", "full")
    fam = CFG.get("clock_font", "sans")

    if fill_dial:
        dr.ellipse([cx - R, cy - R, cx + R, cy + R], fill=dial)

    # tick marks
    if markings != "none":
        for i in range(60):
            is_hour = (i % 5 == 0)
            if not is_hour and markings in ("hours", "minimal"):
                continue
            if markings == "minimal" and i % 15 != 0:   # only 12/3/6/9
                continue
            a = math.radians(i * 6 - 90)
            outer = R * 0.95
            if is_hour:
                inner = R * 0.80; w = max(2, int(R * 0.022 * thick)); c = hourc
            else:
                inner = R * 0.89; w = max(1, int(R * 0.010)); c = track
            dr.line([cx + inner * math.cos(a), cy + inner * math.sin(a),
                     cx + outer * math.cos(a), cy + outer * math.sin(a)], fill=c, width=w)

    # numerals 12 / 3 / 6 / 9
    if CFG.get("analog_numerals", True):
        nf = font(int(R * 0.20), True, fam)
        rad = R * 0.635
        for num, deg in ((12, -90), (3, 0), (6, 90), (9, 180)):
            a = math.radians(deg)
            s = str(num); tw, th = tw_(dr, s, nf)
            tx = cx + rad * math.cos(a); ty = cy + rad * math.sin(a)
            bb = dr.textbbox((0, 0), s, font=nf)
            dr.text((tx - tw / 2 - bb[0], ty - th / 2 - bb[1]), s, fill=ink, font=nf)

    # hands: hour (short, broad), minute (long), second (slender, accent)
    h12 = (now.hour % 12) + now.minute / 60.0
    _hand(dr, cx, cy, h12 * 30.0, R * 0.52, R * 0.055 * thick, ink, back=R * 0.10)
    _hand(dr, cx, cy, (now.minute + now.second / 60.0) * 6.0, R * 0.78, R * 0.038 * thick, ink, back=R * 0.12)
    _hand(dr, cx, cy, now.second * 6.0, R * 0.84, max(2, R * 0.016 * thick), acc, back=R * 0.18)

    # central pivot: dark center inside an accent ring
    pr = max(5, int(R * 0.065))
    dr.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], outline=acc, width=max(2, int(R * 0.022)))
    ir = int(pr * 0.55)
    dr.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], fill=ink)

def _hand(dr, cx, cy, deg, length, width, color, back=0.0):
    a = math.radians(deg - 90)
    ex, ey = cx + length * math.cos(a), cy + length * math.sin(a)
    bx, by = cx - back * math.cos(a), cy - back * math.sin(a)
    dr.line([bx, by, ex, ey], fill=color, width=max(1, int(width)))
    r = max(1, int(width) / 2.0)                       # rounded ends
    dr.ellipse([ex - r, ey - r, ex + r, ey + r], fill=color)
    if back:
        dr.ellipse([bx - r, by - r, bx + r, by + r], fill=color)

def draw_7seg(dr, x, y, H, text, on, off, gap_ratio=0.28):
    """Seven-segment digits (with unlit 'ghost' segments, like a real LCD). Returns end x."""
    W = H * 0.52; t = max(2, H * 0.12); sk = H * 0.10          # width, stroke, italic skew
    def seg(x0, y0, name):
        def P(px, py):                                        # apply slant
            return (x0 + px + sk * (1 - py / H), y0 + py)
        h2 = H / 2; e = t / 2; g = max(1, t * 0.18)
        if name == "a": pts = [(e+g, 0), (W-e-g, 0), (W-t-g, t), (t+g, t)]
        elif name == "d": pts = [(t+g, H-t), (W-t-g, H-t), (W-e-g, H), (e+g, H)]
        elif name == "g": pts = [(e+g, h2), (t+g, h2-e), (W-t-g, h2-e), (W-e-g, h2), (W-t-g, h2+e), (t+g, h2+e)]
        elif name == "f": pts = [(0, e+g), (t, t+g), (t, h2-e-g), (0, h2-g)]
        elif name == "e": pts = [(0, h2+g), (t, h2+e+g), (t, H-t-g), (0, H-e-g)]
        elif name == "b": pts = [(W-t, t+g), (W, e+g), (W, h2-g), (W-t, h2-e-g)]
        else:             pts = [(W-t, h2+e+g), (W, h2+g), (W, H-e-g), (W-t, H-t-g)]   # c
        return [P(px, py) for px, py in pts]
    cx_ = x
    for ch in text:
        if ch == ":":
            r = t * 0.62
            for fy in (0.32, 0.70):
                px = cx_ + t * 0.5 + sk * (1 - fy); py = y + H * fy
                dr.rectangle([px - r, py - r, px + r, py + r], fill=on)
            cx_ += t * 1.8; continue
        lit = _SEG.get(ch, "")
        for sname in "abcdefg":
            dr.polygon(seg(cx_, y, sname), fill=on if sname in lit else off)
        cx_ += W + H * gap_ratio
    return cx_

def seg_width(H, text, gap_ratio=0.28):
    W = H * 0.52; t = max(2, H * 0.12)
    return sum((t * 1.8) if c == ":" else (W + H * gap_ratio) for c in text) - H * gap_ratio + H * 0.10

def draw_rugged_dial(img, dr, cx, cy, fr, now):
    light = time.time() < RUGGED_LIGHT["until"]
    amber = col("accent"); ink = (226, 230, 224); grey = (120, 126, 122); dark = (40, 44, 42)
    R = int(fr * 0.90); cy = cy - int(fr * 0.05)
    # case + bezel
    dr.ellipse([cx - R - 4, cy - R - 4, cx + R + 4, cy + R + 4], fill=(26, 29, 27), outline=(70, 76, 72), width=2)
    dr.ellipse([cx - R, cy - R, cx + R, cy + R], fill=(14, 16, 15))
    # outer ring: fine ticks every 5 deg, longer every 30
    for i in range(72):
        a = math.radians(i * 5 - 90); major = i % 6 == 0
        r0 = R * (0.86 if major else 0.91); r1 = R * 0.97
        dr.line([cx + r0 * math.cos(a), cy + r0 * math.sin(a), cx + r1 * math.cos(a), cy + r1 * math.sin(a)],
                fill=ink if major else grey, width=2 if major else 1)
    # compass points: bright amber markers + letters
    lf = font(int(R * 0.13), True, "DejaVu Sans Condensed")
    for k, (lab, deg) in enumerate((("N", 0), ("E", 90), ("S", 180), ("W", 270))):
        a = math.radians(deg - 90)
        tip = (cx + R * 0.985 * math.cos(a), cy + R * 0.985 * math.sin(a))
        bl = R * 0.84; wd = math.radians(5.5)
        p1 = (cx + bl * math.cos(a - wd), cy + bl * math.sin(a - wd))
        p2 = (cx + bl * math.cos(a + wd), cy + bl * math.sin(a + wd))
        dr.polygon([tip, p1, p2], fill=amber)
        lr = R * 0.73
        bb = dr.textbbox((0, 0), lab, font=lf)
        dr.text((cx + lr * math.cos(a) - (bb[2] - bb[0]) / 2 - bb[0], cy + lr * math.sin(a) - (bb[3] - bb[1]) / 2 - bb[1]),
                lab, fill=amber if lab == "N" else ink, font=lf)
    # inner seconds ring: 60 dashes, lit up to the current second
    for i in range(60):
        if i % 15 == 0:
            continue
        a = math.radians(i * 6 - 90); r0, r1 = R * 0.79, R * 0.835
        c = (amber if i <= now.second else dark) if i % 5 else ((ink if i <= now.second else grey))
        dr.line([cx + r0 * math.cos(a), cy + r0 * math.sin(a), cx + r1 * math.cos(a), cy + r1 * math.sin(a)],
                fill=c, width=2)
    # LCD window
    pw, ph = R * 1.20, R * 0.86
    box = [cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2]
    if light:                                   # backlight: blue-green glow, dark segments
        panel, on, off = (70, 190, 170), (8, 30, 26), (62, 172, 153)
    else:
        panel, on, off = (18, 26, 23), (150, 240, 210), (30, 44, 39)
    dr.rounded_rectangle(box, radius=int(R * 0.10), fill=panel, outline=(70, 80, 76), width=2)
    # top readout: day + date
    sf = font(int(R * 0.115), True, "DejaVu Sans Mono")
    top = now.strftime("%a").upper()[:2]
    dr.text((box[0] + R * 0.09, box[1] + R * 0.06), top, fill=on, font=sf)
    sH = R * 0.15
    ds = now.strftime("%m-%d")
    draw_7seg(dr, box[2] - R * 0.09 - seg_width(sH, ds, 0.22), box[1] + R * 0.065, sH, ds, on, off, 0.22)
    # main time
    if CFG.get("clock_24h"):
        hh = now.strftime("%H"); pm = ""
    else:
        hh = "%2d" % ((now.hour % 12) or 12); pm = "PM" if now.hour >= 12 else "AM"
    txt = hh + ":" + now.strftime("%M")
    H = R * 0.34
    tw = seg_width(H, txt)
    draw_7seg(dr, cx - tw / 2, cy - H * 0.42, H, txt, on, off)
    if pm:
        pf = font(int(R * 0.075), True, "DejaVu Sans Mono")
        dr.text((box[0] + R * 0.07, cy - H * 0.40), pm, fill=on, font=pf)
    # bottom readout: seconds + status icons
    s2 = now.strftime("%S"); bH = R * 0.15
    draw_7seg(dr, box[2] - R * 0.09 - seg_width(bH, s2, 0.22), box[3] - R * 0.06 - bH, bH, s2, on, off, 0.22)
    iy = box[3] - R * 0.06 - bH * 0.5; ix = box[0] + R * 0.10; u = R * 0.05
    def icon_on(flag): return on if flag else off
    # antenna = sensors live
    c1 = icon_on(sensors.get("ok"))
    dr.line([ix, iy + u, ix, iy - u], fill=c1, width=2)
    dr.polygon([(ix - u * 0.9, iy - u), (ix + u * 0.9, iy - u), (ix, iy)], outline=c1)
    # note = music
    ix += u * 3; c2 = icon_on(audio_running())
    dr.ellipse([ix - u * 0.6, iy + u * 0.2, ix + u * 0.4, iy + u * 1.0], fill=c2)
    dr.line([ix + u * 0.35, iy + u * 0.6, ix + u * 0.35, iy - u], fill=c2, width=2)
    dr.line([ix + u * 0.35, iy - u, ix + u * 1.0, iy - u * 0.6], fill=c2, width=2)
    # bell = night hours set
    ix += u * 3; c3 = icon_on(CFG.get("night_auto"))
    dr.chord([ix - u * 0.8, iy - u, ix + u * 0.8, iy + u * 1.4], 180, 360, fill=c3)
    dr.rectangle([ix - u * 0.8, iy + u * 0.2, ix + u * 0.8, iy + u * 0.55], fill=c3)
    # side "buttons" with labels, like the real thing
    bf = font(max(9, int(fr * 0.075)), True, "DejaVu Sans Condensed")
    for lab, deg, side in (("COMPASS", -50, -1), ("BARO", 50, 1), ("ALTI", -130, -1), ("MODE", 130, 1)):
        a = math.radians(deg - 90)
        bx, by = cx + (R + 5) * math.cos(a), cy + (R + 5) * math.sin(a)
        dr.rounded_rectangle([bx - 5, by - 7, bx + 5, by + 7], radius=3, fill=(60, 66, 62))
        bb = dr.textbbox((0, 0), lab, font=bf)
        tx = bx + (8 if side > 0 else -8 - (bb[2] - bb[0]))
        ty = by - (bb[3] - bb[1]) / 2 - bb[1] + (-12 if deg in (-50, 50) else 12)
        tx = max(cx - fr + 2, min(tx, cx + fr - (bb[2] - bb[0]) - 2))
        dr.text((tx, ty), lab, fill=amber if lab == "MODE" else (170, 176, 172), font=bf)
    # LIGHT button below the display
    lb = dr.textbbox((0, 0), "LIGHT", font=bf); lw = lb[2] - lb[0] + 16
    ly = cy + R + 8
    dr.rounded_rectangle([cx - lw / 2, ly, cx + lw / 2, ly + (lb[3] - lb[1]) + 8], radius=6,
                         fill=(70, 190, 170) if light else (34, 38, 36), outline=(90, 98, 94))
    dr.text((cx - (lb[2] - lb[0]) / 2 - lb[0], ly + 4 - lb[1]), "LIGHT", fill=(8, 30, 26) if light else ink, font=bf)
    return (cx - lw / 2 - 10, ly - 10, cx + lw / 2 + 10, ly + (lb[3] - lb[1]) + 18)

def draw_digital_dial(dr, cx, cy, fr, now, fill=True):
    """Digital time inside the same rounded-square frame the analog dial uses."""
    if fill:
        dr.rounded_rectangle([cx - fr + 6, cy - fr + 6, cx + fr - 6, cy + fr - 6],
                             radius=int(fr * 0.19), fill=mix(col("bg"), col("fg"), 0.05))
    fam = CFG.get("clock_font", "sans")
    if CFG.get("clock_24h"):
        hh, ap = now.strftime("%H"), ""
    else:
        hh, ap = now.strftime("%I").lstrip("0"), now.strftime("%p")
    mm = now.strftime("%M")
    # stacked hours over minutes, as large as the frame allows
    size = int(fr * 0.98)
    while size > 20:
        f = font(size, True, fam)
        if max(tw_(dr, "88", f)[0], tw_(dr, hh, f)[0]) <= fr * 1.55:
            break
        size -= 2
    f = font(size, True, fam)
    def ctext(t, y, fnt, c):
        bb = dr.textbbox((0, 0), t, font=fnt)
        dr.text((cx - (bb[2] - bb[0]) / 2 - bb[0], y - bb[1]), t, fill=c, font=fnt)
    bb = dr.textbbox((0, 0), "88", font=f); th = bb[3] - bb[1]
    gap = int(fr * 0.10)
    top = cy - th - gap // 2 - int(fr * 0.06)
    ctext(hh, top, f, col("fg"))
    ctext(mm, top + th + gap, f, col("accent"))
    # seconds progress ring along the bottom edge + am/pm
    sf = font(int(fr * 0.13), True)
    lab = ap if ap else ""
    if lab:
        ctext(lab, cy + fr - int(fr * 0.27), sf, mix(col("bg"), col("fg"), 0.55))
    y = cy + fr - int(fr * 0.09); x0 = cx - int(fr * 0.55); x1 = cx + int(fr * 0.55)
    dr.rounded_rectangle([x0, y - 2, x1, y + 2], radius=2, fill=mix(col("bg"), col("fg"), 0.18))
    dr.rounded_rectangle([x0, y - 2, x0 + int((x1 - x0) * now.second / 59.0), y + 2], radius=2, fill=col("accent"))

def render_analog(img, dr, w, h, now, playing, glass):
    """Analog face: rounded-square dial on the left, info column on the right."""
    fg = col("fg"); dim = mix(col("bg"), col("fg"), 0.45)
    if glass:
        dim = mix(col("fg"), (160, 170, 185), 0.5)
    R = int(h * 0.38)
    fr = int(R * 1.16)
    cx = fr + int(w * 0.012); cy = h // 2
    if not CFG.get("show_date") and not CFG.get("show_weather"):
        cx = w // 2                      # nothing beside the dial (the frame has its own calendar and weather)
    dial = CFG.get("dial") or "analog"
    if not dial.startswith("legacy:") and dial != "rugged":    # softly rounded square frame
        dr.rounded_rectangle([cx - fr, cy - fr, cx + fr, cy + fr], radius=int(fr * 0.22),
                             outline=mix(col("bg"), col("fg"), 0.22), width=2)
    if dial.startswith("legacy:"):
        name = dial.split(":", 1)[1]
        S = 2 * fr
        art = None
        if name in ("digital", "digital-orange"):
            draw_legacy_digital(dr, cx, cy, fr, now, name)
        else:
            try:
                art = legacy_dial(name, S, now)
            except Exception as e:
                sys.stderr.write("legacy face %s: %s\n" % (name, e))
        if art is not None:
            if name == "gradient":                 # feather the glow so the crop has no edge
                mk = Image.new("L", art.size, 0)
                e = int(S * 0.06)
                ImageDraw.Draw(mk).rounded_rectangle([e, e, S - e, S - e], radius=int(S * 0.2), fill=255)
                img.paste(art, (cx - fr, cy - fr), mk.filter(ImageFilter.GaussianBlur(e * 0.8)))
            else:
                img.paste(art, (cx - fr, cy - fr))
    elif dial == "digital":
        draw_digital_dial(dr, cx, cy, fr, now, fill=not glass)
    elif dial == "rugged":
        draw_rugged_dial(img, dr, cx, cy, fr, now)
    else:
        draw_analog(dr, cx, cy, R, now, fill_dial=not glass)

    # right-hand info column
    x = cx + fr + int(w * 0.035)
    maxw = w - x - 6
    if CFG.get("show_date"):
        wk = tuple(CFG["weekday"]) if CFG.get("weekday") else dim
        d0 = int(h * 0.05) if CFG.get("show_weather") else int(h * 0.30)
        dr.text((x, d0), now.strftime("%a"), fill=wk, font=font(int(h * 0.075), bool(CFG.get("weekday"))))
        dr.text((x, d0 + int(h * 0.08)), now.strftime("%d %b"), fill=fg, font=font(int(h * 0.11), True))
        if not CFG.get("show_weather"):
            dr.text((x, d0 + int(h * 0.21)), now.strftime("%Y"), fill=dim, font=font(int(h * 0.075)))
    if CFG.get("show_weather"):
        F = font(int(h * 0.085), True)          # one size/weight for every value
        HF = font(int(h * 0.055), True)         # section headers
        isz = int(h * 0.07)                      # icon box
        ioff = int(h * 0.017)                    # icon vertical nudge vs text
        gap = int(h * 0.03)
        rstep = int(h * 0.10); hstep = int(h * 0.068)
        y = int(h * 0.27) if CFG.get("show_date") else int(h * 0.08)
        deg = "°C" if CFG.get("units", "C") == "C" else "°F"
        blue = col("weather_blue"); acc = col("accent")
        hum_c = (80, 170, 255)
        if CFG.get("mono"):                     # Night face: everything in one dim hue
            hum_c = acc

        def num(v, fmt):
            return (fmt % round(v)) if v is not None else "--"

        def row(items):
            """items: list of (icon_fn, text, colour); drawn left-to-right on one line."""
            pad = int(h * 0.010)
            sz = int(h * 0.085); Fr = F
            while sz > int(h * 0.05):            # shrink this row only if it won't fit
                Fr = font(sz, True)
                need = sum(isz + pad + dr.textlength(t, font=Fr) for _, t, _ in items) + gap * (len(items) - 1)
                if x + need <= w - (int(min(w, h) * 0.06) if glass else 4):
                    break
                sz -= 1
            cx_ = x
            for ic, txt, c in items:
                ic(cx_, y + ioff)
                cx_ += isz + pad
                dr.text((cx_, y), txt, fill=c, font=Fr)
                cx_ += int(dr.textlength(txt, font=Fr)) + gap

        if CFG.get("weather_source") == "mqtt":
            stale = not sensors["ok"]
            vcol = dim if stale else fg
            aqi = sensors["aqi"] if sensors["aqi"] is not None else compute_aqi(sensors["pm25"], sensors["pm10"])
            dr.text((x, y), CFG.get("mqtt_label", "Indoor") or "Indoor", fill=dim, font=HF); y += hstep
            row([(lambda a, b: icon_thermo(dr, a, b, isz, acc), num(sensors["temp"], "%d" + deg), dim if stale else blue)]); y += rstep
            row([(lambda a, b: icon_drop(dr, a, b, isz, hum_c), num(sensors["hum"], "%d%%"), vcol)]); y += rstep
            mono = CFG.get("mono")
            row([(lambda a, b: (icon_aqi(dr, a, b, isz, aqi) if not mono else icon_aqi_mono(dr, a, b, isz, acc)),
                  num(aqi, "%d"),
                  (aqi_color(aqi) if (aqi is not None and not stale and not mono) else vcol))]); y += rstep + int(h * 0.01)
            dr.text((x, y), "Outdoor", fill=dim, font=HF); y += hstep
            ot = num(out_temp(), "%d°"); oh = num(out_hum(), "%d%%")
            row([(lambda a, b: icon_thermo(dr, a, b, isz, acc), ot, dim if stale else blue),
                 (lambda a, b: icon_drop(dr, a, b, isz, hum_c), oh, vcol)])
        else:
            dr.text((x, y), "Outdoor", fill=dim, font=HF); y += hstep
            if weather["ok"]:
                row([(lambda a, b: icon_thermo(dr, a, b, isz, acc), "%s%s" % (weather["temp"], deg), blue)]); y += rstep
                row([(lambda a, b: icon_drop(dr, a, b, isz, hum_c), "%s%%" % weather["hum"], fg)]); y += rstep
                dr.text((x, y), fit_text(dr, _WX.get(weather["code"], ""), F, maxw), fill=fg, font=F)
            else:
                dr.text((x, y), "weather…", fill=dim, font=F)
    if CFG.get("show_nowplaying") and playing and not CFG.get("show_weather"):
        artist, title = playing
        line = ("%s — %s" % (artist, title)) if artist else title
        sf = font(int(h * 0.045), True)
        line = fit_text(dr, "♪ " + line, sf, maxw)
        dr.text((x, int(h * 0.91)), line, fill=col("accent"), font=sf)
    return img

def legacy_dial(name, S, now):
    """Render one of the original faces and cut out its dial, scaled to S x S."""
    m = _legacy_mod()
    if m is None:
        return None
    tt = now.timetuple()
    W, H = 480, 320
    def face(w, h):
        k = (name, w, h, CFG.get("clock_24h", False))
        if k not in _legacy["faces"]:
            _legacy["faces"][k] = m.make_face(name, w, h, {"clock_24h": CFG.get("clock_24h", False)})
        return _legacy["faces"][k]
    if name in ("purple", "wide"):                       # these lay out cleanly on a square canvas
        return face(S, S).render(tt, True, ())
    if name in ("digital", "digital-orange"):
        return None                                      # drawn natively (see draw_legacy_digital)
    f = face(W, H); im = f.render(tt, f.uses_seconds, ())
    if name in ("dark", "light", "light-minimal"):
        box = (f.dial_x, f.dial_y, f.dial_x + f.size, f.dial_y + f.size)
    elif name in ("teal", "white-square"):
        side = min(H * 0.98, W * 0.72) + 4
        box = (W / 2 - side / 2, H / 2 - side / 2, W / 2 + side / 2, H / 2 + side / 2)
    elif name == "gradient":
        p = int(f.size * 0.03)
        box = (max(0, f.dx - p), max(0, f.dy - p), f.dx + f.size + p, f.dy + f.size + p)
    elif name == "calendar":
        cx, cy, r = H * 0.36, H * 0.51, H * 0.34 * 1.04
        box = (cx - r, cy - r, cx + r, cy + r)
    else:                                                # wide-minimal
        r = min(H * 0.43, W * 0.31) * 1.03
        box = (W / 2 - r, H / 2 - r, W / 2 + r, H / 2 + r)
    box = tuple(int(round(v)) for v in box)
    return im.crop(box).resize((S, S), Image.LANCZOS)

def draw_legacy_digital(dr, cx, cy, half, now, name):
    col_ = (255, 159, 10) if name == "digital" else (255, 166, 105)
    m = _legacy_mod()
    path = m.date_font_path("auto") if m else None
    if CFG.get("clock_24h"):
        label = now.strftime("%H:%M")
    else:
        label = "%d:%02d" % ((now.hour % 12) or 12, now.minute)
    size = int(half * 1.2)
    while size > 20:
        f = ImageFont.truetype(path, size) if path else font(size, True)
        bb = dr.textbbox((0, 0), "88:88" if len(label) == 5 else "8:88", font=f)
        if bb[2] - bb[0] <= half * 1.9:
            break
        size -= 2
    bb = dr.textbbox((0, 0), label, font=f)
    dr.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - (bb[3] - bb[1]) / 2 - bb[1]), label, fill=col_, font=f)

def _legacy_mod():
    if _legacy["mod"] is None and _legacy["err"] is None:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import faces_legacy as m
            _legacy["mod"] = m
        except Exception as e:
            _legacy["err"] = str(e); sys.stderr.write("legacy faces unavailable: %s\n" % e)
    return _legacy["mod"]

def _installed_families():
    if _banner_cache.get("fams") is None:
        fams = set()
        try:
            out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=8).stdout
            for ln in out.splitlines():
                for n in ln.split(","):
                    if n.strip(): fams.add(n.strip())
        except Exception:
            pass
        _banner_cache["fams"] = sorted(fams)
    return _banner_cache["fams"]

def _resolve_family(name):
    """Exact installed family, else the first one containing the name ('Amplify' -> 'Amplify Personal Use Only')."""
    n = name.lower(); fams = _installed_families()
    for f in fams:
        if f.lower() == n: return f
    for f in fams:
        if n in f.lower(): return f
    return None

def _banner_family():
    f = (CFG.get("banner_font") or "auto").strip()
    if f.lower() != "auto":
        return _resolve_family(f) or f
    if _banner_cache["font"]:
        return _banner_cache["font"]
    pick = "serif"
    for c in _CURSIVE:
        r = _resolve_family(c)
        if r:
            pick = r; break
    _banner_cache["font"] = pick
    return pick

def _wrap_words(dr, text, fnt, maxw):
    out = []
    for para in text.split("\n"):
        words = para.split(); cur = ""
        for wd in words:
            t = (cur + " " + wd).strip()
            if cur and tw_(dr, t, fnt)[0] > maxw:
                out.append(cur); cur = wd
            else:
                cur = t
        out.append(cur)
    return [l for l in out] or [""]

def mesh_background(w, h):
    """Dense black diamond weave (over/under strands) with a soft light pool, like woven grille cloth."""
    key = (w, h)
    if key in _banner_cache["mesh"]:
        return _banner_cache["mesh"][key]
    P = max(8.0, h * 0.030)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u = (xx + yy) / P; v = (xx - yy) / P
    t1 = u - np.floor(u); t2 = v - np.floor(v)
    s1 = np.sin(np.pi * t1) ** 0.8; s2 = np.sin(np.pi * t2) ** 0.8
    over = ((np.floor(u) + np.floor(v)).astype(np.int64) & 1) == 0
    H = np.where(over, s1 + 0.35 * s2 * (1 - s1), s2 + 0.35 * s1 * (1 - s2))
    lit = np.where(over, 0.65 + 0.7 * (1 - t1), 0.65 + 0.7 * t2)        # one-sided highlight on each strand
    lum = np.clip(H, 0, 1) ** 3.0 * lit
    pool = np.exp(-(((xx - w * 0.45) / (w * 0.75)) ** 2 + ((yy - h * 0.35) / (h * 0.9)) ** 2))
    lum = lum * (0.40 + 0.75 * pool)
    base = np.array([3, 3, 4], np.float32); hi = np.array([70, 72, 78], np.float32)
    arr = base + (hi - base) * np.clip(lum, 0, 1)[:, :, None]
    img = Image.fromarray(arr.astype(np.uint8), "RGB")
    _banner_cache["mesh"] = {key: img}
    return img

def banner_layout(w, ah):
    """(lines, font, size, colour) for the lettering: largest size that fits the text area."""
    text = str(CFG.get("banner_text") or "").strip("\n") or " "
    fam = _banner_family(); pct = max(40, min(100, int(CFG.get("banner_size", 100)))) / 100.0
    colr = tuple(int(c) for c in (CFG.get("banner_color") or [214, 170, 72]))
    probe = ImageDraw.Draw(Image.new("L", (4, 4)))
    maxw, maxh = int(w * 0.90), int(ah * 0.84)
    size = int(ah * 0.62); lines = [text]; fnt = font(size, False, fam)
    while size > 10:
        fnt = font(size, False, fam)
        lines = _wrap_words(probe, text, fnt, maxw)
        asc, desc = fnt.getmetrics(); lh = int((asc + desc) * 0.92)
        if len(lines) * lh <= maxh and all(tw_(probe, l, fnt)[0] <= maxw for l in lines):
            break
        size -= 2
    size = max(10, int(size * pct))
    return lines, font(size, False, fam), size, colr

def _gold(colr):
    return mix(colr, (255, 255, 255), 0.55), mix(colr, (0, 0, 0), 0.15)

def banner_text_layer(w, ah):
    """RGBA layer (w x ah) with the gold lettering, auto-sized to fill the area."""
    lines, fnt, size, colr = banner_layout(w, ah)
    key = (tuple(lines), size, colr, w, ah, _banner_family())
    if key in _banner_cache["text"]:
        return _banner_cache["text"][key]
    asc, desc = fnt.getmetrics(); lh = int((asc + desc) * 0.92)
    mask = Image.new("L", (w, ah), 0); md = ImageDraw.Draw(mask)
    y = (ah - lh * len(lines)) // 2
    for l in lines:
        bb = md.textbbox((0, 0), l, font=fnt)
        md.text(((w - (bb[2] - bb[0])) // 2 - bb[0], y), l, fill=255, font=fnt)
        y += lh
    top, bot = _gold(colr)
    g = np.linspace(0, 1, ah, dtype=np.float32)[:, None, None]
    grad = (np.array(top, np.float32) * (1 - g) + np.array(bot, np.float32) * g) * np.ones((1, w, 1), np.float32)
    layer = Image.new("RGBA", (w, ah), (0, 0, 0, 0))
    shadow = mask.filter(ImageFilter.GaussianBlur(max(2, size // 28)))
    sh = Image.new("RGBA", (w, ah), (0, 0, 0, 0)); sh.putalpha(shadow.point(lambda a: int(a * 0.7)))
    layer.alpha_composite(sh, (max(1, size // 40), max(2, size // 22)))
    gold = Image.fromarray(grad.astype(np.uint8), "RGB").convert("RGBA"); gold.putalpha(mask)
    layer.alpha_composite(gold)
    _banner_cache["text"] = {key: layer}
    return layer

FACES = {
    # --- the twelve faces from the original moode-nowplaying app (30 Oct), dial art reused as-is ---
    "dark":           {"label": "Dark",           "o": {"face": "analog", "dial": "legacy:dark"}},
    "light-minimal":  {"label": "Light minimal",  "o": {"face": "analog", "dial": "legacy:light-minimal"}},
    "light":          {"label": "Light",          "o": {"face": "analog", "dial": "legacy:light"}},
    "digital":        {"label": "Digital",        "o": {"face": "analog", "dial": "legacy:digital"}},
    "wide":           {"label": "Wide",           "o": {"face": "analog", "dial": "legacy:wide"}},
    "purple":         {"label": "Purple",         "o": {"face": "analog", "dial": "legacy:purple"}},
    "teal":           {"label": "Teal",           "o": {"face": "analog", "dial": "legacy:teal"}},
    "white-square":   {"label": "White square",   "o": {"face": "analog", "dial": "legacy:white-square"}},
    "calendar":       {"label": "Calendar",       "o": {"face": "analog", "dial": "legacy:calendar"}},
    "digital-orange": {"label": "Digital orange", "o": {"face": "analog", "dial": "legacy:digital-orange"}},
    "wide-minimal":   {"label": "Wide minimal",   "o": {"face": "analog", "dial": "legacy:wide-minimal"}},
    "gradient":       {"label": "Gradient",       "o": {"face": "analog", "dial": "legacy:gradient"}},
    # --- new ---
    "banner":   {"label": "Banner",   "o": {"face": "banner", "style": "flat",
                 "bg": [9, 9, 12], "bg2": [9, 9, 12], "fg": [236, 230, 214],
                 "accent": [214, 170, 72], "weather_blue": [226, 190, 110]}},
    "paper":    {"label": "Paper",    "o": {"face": "analog", "style": "flat",
                 "bg": [244, 240, 231], "bg2": [228, 222, 208], "fg": [50, 54, 60],
                 "accent": [232, 108, 28], "weather_blue": [36, 100, 186]}},
    "classic":  {"label": "Classic",  "o": {"face": "analog"}},
    "glass":    {"label": "Glass",    "o": {"face": "analog", "style": "glass",
                 "bg": [10, 14, 26], "bg2": [34, 46, 92], "fg": [240, 242, 246],
                 "accent": [29, 185, 84], "weather_blue": [120, 180, 255]}},
    "sunset":   {"label": "Sunset",   "o": {"face": "analog", "style": "gradient",
                 "bg": [36, 20, 92], "bg2": [236, 112, 86], "fg": [255, 255, 255],
                 "accent": [255, 214, 102], "weather_blue": [200, 228, 255]}},
    "stacked":  {"label": "Stacked",  "o": {"face": "analog", "dial": "digital"}},
    "rugged":   {"label": "Rugged",   "o": {"face": "analog", "dial": "rugged", "style": "flat",
                 "bg": [7, 9, 8], "bg2": [7, 9, 8], "fg": [232, 236, 230],
                 "accent": [255, 138, 20], "weather_blue": [120, 230, 200]}},
    "night":    {"label": "Night",    "o": {"face": "analog", "style": "flat", "mono": True,
                 "bg": [0, 0, 0], "bg2": [0, 0, 0], "fg": [210, 62, 42], "accent": [235, 80, 50],
                 "weather_blue": [225, 90, 60], "brightness": 60, "analog_markings": "hours"}},
}
_LEG_PAL = {
    "dark": ((0, 0, 0), (255, 149, 0)), "light-minimal": ((0, 0, 0), (255, 149, 0)),
    "light": ((0, 0, 0), (255, 149, 0)), "digital": ((0, 0, 0), (255, 159, 10)),
    "wide": ((0, 0, 0), (110, 220, 150)), "purple": ((12, 12, 13), (174, 76, 255)),
    "teal": ((11, 67, 69), (255, 145, 24)), "white-square": ((9, 10, 11), (255, 145, 24)),
    "calendar": ((0, 0, 0), (235, 80, 92)), "digital-orange": ((0, 0, 0), (255, 166, 105)),
    "wide-minimal": ((0, 0, 0), (156, 210, 184)), "gradient": ((0, 0, 0), (61, 214, 198)),
}

for _n, (_bg, _acc) in _LEG_PAL.items():
    FACES[_n]["o"].update(style="flat", bg=list(_bg), bg2=list(_bg), fg=[245, 245, 247],
                          accent=list(_acc), weather_blue=list(_acc), weekday=[255, 82, 72])


# ----------------------------------------------------------------------------------------
# Frame-specific: the Banner face without weather, and the one entry point.
def render_banner(w, h, now):
    """Gold cursive lettering (top 3/4) on a woven black mesh; time and date in the bottom strip."""
    top_h = int(h * 0.75)
    img = Image.new("RGB", (w, h), (8, 8, 10))
    img.paste(mesh_background(w, top_h), (0, 0))
    img.paste(Image.new("RGB", (w, h - top_h), (9, 9, 12)), (0, top_h))
    lay = banner_text_layer(w, top_h)
    img.paste(lay, (0, 0), lay)
    dr = ImageDraw.Draw(img)
    gold = tuple(int(c) for c in (CFG.get("banner_color") or [214, 170, 72]))
    dr.rectangle([0, top_h, w, top_h + 2], fill=mix((9, 9, 12), gold, 0.55))
    fg = col("fg")
    bh = h - top_h
    tstr, ampm = clock_strings(now)
    left = ("%s %s" % (tstr, ampm)).strip()
    f2 = font(int(bh * 0.46), True)
    pad = int(w * 0.04)
    y = top_h + (bh - tw_(dr, "0", f2)[1]) // 2 - 4
    lw = tw_(dr, left, f2)[0]
    dr.text(((w - lw) // 2, y), left, fill=fg, font=f2)
    return img


def _render(w, h, now):
    face = CFG.get("face")
    if face == "banner":
        return render_banner(w, h, now)
    if CFG.get("style") == "gradient":
        img = gradient_background(w, h)
        dr = ImageDraw.Draw(img)
        return render_analog(img, dr, w, h, now, None, True)
    glass = CFG.get("style") == "glass"
    if glass:
        img = glass_background(w, h)
        m = int(min(w, h) * 0.045)
        frost(img, (m, m, w - m, h - m), radius=int(min(w, h) * 0.085))
    else:
        img = Image.new("RGB", (w, h), col("bg"))
    return render_analog(img, ImageDraw.Draw(img), w, h, now, None, glass)


_OPT_KEYS = ("clock_24h", "show_seconds", "style", "bg", "bg2", "fg", "accent",
             "analog_thickness", "analog_markings", "analog_numerals", "brightness",
             "banner_text", "banner_font", "banner_size", "banner_color")


NO_SECONDS = {"digital", "digital-orange", "stacked", "banner"}   # these show minutes only
ASPECT = {"banner": 2 / 3}   # tile height / width; every other face is a square dial


def has_seconds(face_id):
    """True when the face has something that moves every second (a seconds hand or digits)."""
    return face_id not in NO_SECONDS


def aspect(face_id):
    return ASPECT.get(face_id, 1.0)


def render_face(face_id, w, h, now=None, opts=None):
    """One face as a w x h RGB image. `opts` override the preset (see module docstring)."""
    now = now or datetime.datetime.now()
    if face_id not in FACES:
        face_id = "dark"
    CFG.clear()
    CFG.update(_BASE)
    CFG.update(FACES[face_id]["o"])
    CFG.setdefault("dial", "analog")
    opts = opts or {}
    for k in _OPT_KEYS:                      # explicit options beat the preset
        if opts.get(k) is not None:
            CFG[k] = opts[k]
    CFG["_playing"] = bool(opts.get("playing"))
    if opts.get("font_path"):
        CFG["_font_path"] = opts["font_path"]
    img = _render(w, h, now)
    b = int(CFG.get("brightness", 100))
    if b < 100:
        img = Image.fromarray((np.asarray(img).astype(np.float32) * (b / 100.0)).astype(np.uint8))
    return img
