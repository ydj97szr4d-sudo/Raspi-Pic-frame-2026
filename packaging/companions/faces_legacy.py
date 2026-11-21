#!/usr/bin/env python3
# =============================================================================
#  moode-nowplaying  (moode-mods) - original 30 Oct 2026 app. clockweather.py imports its 12 clock faces.
#
#  "Now playing" screen for a small SPI touch display (e.g. 3.5" 480x320) on a
#  moOde audio player. Draws straight to the Linux framebuffer, so no desktop
#  or browser is needed and CPU use stays low.
#
#  Shows: album art, title, artist, album, audio format and source (MPD,
#  radio, AirPlay, Spotify, Squeezelite, ...). Idle: a clock.
#  Touch: previous / play-pause / next and volume -/+ (for moOde's own player).
#
#  Data comes from moOde's /var/local/www/currentsong.txt
#  (Configure -> Audio -> MPD Options -> Metadata file: ON).
#  Controls use moOde's local command API (http://localhost/command/?cmd=...).
#
#  Usage:
#    moode-nowplaying.py              run the display (normally via systemd)
#    moode-nowplaying.py --test       show a test pattern, then exit
#    moode-nowplaying.py --calibrate  calibrate the touchscreen (tap 3 targets)
#
#  Settings: /etc/moode-nowplaying.json (created by --calibrate)
# =============================================================================

import argparse
import glob
import io
import json
import math
import os
import select
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

CURRENTSONG = '/var/local/www/currentsong.txt'
CONFIG_FILE = '/etc/moode-nowplaying.json'
API = 'http://localhost/command/?cmd='
WEB_ROOT = 'http://localhost/'

DEFAULTS = {
    'fb_device': 'auto',          # 'auto' picks the SPI panel's framebuffer
    'touch': None,                # filled by --calibrate
    'volume_step': 5,
    'idle_clock': True,           # show a clock when nothing is playing
    'clock_style': 'analog',      # kept for older settings files
    'face': 'purple',             # current clock face (set by swiping): dark, light-minimal, light, digital, wide, purple
    'faces': None,                # which faces to swipe through, in order (None = all)
    'clock_24h': False,           # digital face: 24-hour time
    'night_mode': 'auto',         # 'auto' (between night_start and night_end), 'on' or 'off'
    'night_start': '22:00',
    'night_end': '06:30',
    'night_style': 'dim',         # 'dim' (dark faces, lower brightness) or 'red' (dim red, easy on the eyes)
    'night_brightness': 0.45,     # 0.1 - 1.0: how bright the clock is at night
    'clock_face': 'dark',         # analog face: 'dark' or 'light'
    'clock_numerals': 'all',      # analog numerals: 'all', 'quarters' (12/3/6/9) or 'none'
    'clock_seconds': True,        # analog: show the seconds hand
    'date_font': 'auto',          # clock date font file, or 'auto' (Nimbus Sans, a Helvetica-style font)
    'date_font_index': 0,         # face number inside a .ttc font collection
    'idle_black_after_min': 0,    # go dark after this many idle minutes (0 = never)
    'progress_interval_s': 2,     # how often to redraw the progress bar
}

# Colours
BG = (12, 12, 16)
FG = (240, 240, 240)
DIM = (150, 150, 160)
FAINT = (70, 70, 80)
ACCENT = (64, 160, 255)
BUTTON = (34, 34, 44)

FONT_REG = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def log(msg):
    print(msg, flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f'Warning: could not read {CONFIG_FILE}: {e}')
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    log(f'Saved {CONFIG_FILE}')


def font(path, size, index=0):
    try:
        return ImageFont.truetype(path, size, index=index)
    except OSError:
        return ImageFont.load_default(size=size)


# Helvetica-style fonts for the clock's date, best first. Nimbus Sans is URW's
# free Helvetica-compatible design (Debian package fonts-urw-base35).
DATE_FONT_CANDIDATES = [
    '/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf',
    '/usr/share/fonts/truetype/urw-base35/NimbusSans-Bold.ttf',
    '/usr/share/fonts/type1/urw-base35/NimbusSans-Bold.t1',
    FONT_BOLD,
]


def date_font_path(setting):
    """'auto' picks the best installed Helvetica-style font; otherwise use the given file."""
    if setting and setting != 'auto':
        if os.path.exists(setting):
            return setting
        log(f'date_font {setting} not found, using the default')
    for path in DATE_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return FONT_BOLD


# -----------------------------------------------------------------------------
#  Framebuffer
# -----------------------------------------------------------------------------
class Framebuffer:
    def __init__(self, device='auto'):
        self.path = self._find(device)
        name = os.path.basename(self.path)
        sysfs = f'/sys/class/graphics/{name}'
        self.bpp = int(open(f'{sysfs}/bits_per_pixel').read())
        self.width, self.height = (int(v) for v in open(f'{sysfs}/virtual_size').read().split(','))
        try:
            self.stride = int(open(f'{sysfs}/stride').read())
        except (FileNotFoundError, ValueError):
            self.stride = self.width * self.bpp // 8
        if self.bpp not in (16, 32):
            raise RuntimeError(f'Unsupported framebuffer depth {self.bpp} bpp')
        self.fd = os.open(self.path, os.O_RDWR)
        log(f'Framebuffer {self.path}: {self.width}x{self.height}, {self.bpp} bpp')

    @staticmethod
    def _find(device):
        if device != 'auto':
            return device
        for sysfs in sorted(glob.glob('/sys/class/graphics/fb*')):
            try:
                name = open(f'{sysfs}/name').read().strip().lower()
            except OSError:
                continue
            if any(k in name for k in ('ili9486', 'mipi-dbi', 'panel', 'fb_ili')):
                return '/dev/' + os.path.basename(sysfs)
        if os.path.exists('/dev/fb0'):
            return '/dev/fb0'
        raise RuntimeError('No framebuffer found. Is the screen driver (dtoverlay=piscreen,drm) loaded?')

    def _encode(self, img):
        img = img.convert('RGB')
        if self.bpp == 32:
            return img.tobytes('raw', 'BGRX')
        # RGB565 little-endian, built from 8-bit channel ops (fast, exact)
        r, g, b = img.split()
        hi = ImageChops.add(r.point(lambda v: v & 0xF8), g.point(lambda v: v >> 5))
        lo = ImageChops.add(g.point(lambda v: ((v >> 2) & 7) << 5), b.point(lambda v: v >> 3))
        return Image.merge('LA', (lo, hi)).tobytes()

    def show(self, img, y0=0, y1=None):
        """Write rows y0..y1 of img (full width) to the screen."""
        y1 = self.height if y1 is None else y1
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        data = self._encode(img.crop((0, y0, self.width, y1)))
        row_bytes = self.width * self.bpp // 8
        if self.stride == row_bytes:
            os.pwrite(self.fd, data, y0 * self.stride)
        else:
            for i, y in enumerate(range(y0, y1)):
                os.pwrite(self.fd, data[i * row_bytes:(i + 1) * row_bytes], y * self.stride)


# -----------------------------------------------------------------------------
#  Touchscreen (raw evdev, no extra packages)
# -----------------------------------------------------------------------------
EV_KEY, EV_ABS = 1, 3
ABS_X, ABS_Y = 0, 1
BTN_TOUCH = 0x14a
EVENT_FMT = 'llHHi'
EVENT_SIZE = struct.calcsize(EVENT_FMT)
SWIPE_MIN = 60          # pixels (at 480x320) of vertical movement that count as a swipe


def find_touch_device():
    for path in sorted(glob.glob('/sys/class/input/event*/device/name')):
        try:
            name = open(path).read().strip()
        except OSError:
            continue
        if 'ADS7846' in name or 'touch' in name.lower():
            return '/dev/input/' + path.split('/')[4]
    return None


class Touch:
    def __init__(self, calib):
        self.calib = calib
        self.dev = find_touch_device()
        self.fd = os.open(self.dev, os.O_RDONLY | os.O_NONBLOCK) if self.dev else None
        self.raw_x = self.raw_y = None
        self.down = False
        self.start = None
        if self.dev:
            log(f'Touchscreen {self.dev}' + ('' if calib else ' (not calibrated: run --calibrate)'))

    def fileno(self):
        return self.fd

    def read_gestures(self):
        """Return a list of (start_raw, end_raw) for each finished touch."""
        out = []
        if self.fd is None:
            return out
        try:
            data = os.read(self.fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return out
        for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, i)
            if etype == EV_ABS and code == ABS_X:
                self.raw_x = value
            elif etype == EV_ABS and code == ABS_Y:
                self.raw_y = value
            elif etype == EV_KEY and code == BTN_TOUCH:
                if value == 1:
                    self.down = True
                    self.start = None
                elif self.down:
                    self.down = False
                    if self.raw_x is not None and self.raw_y is not None:
                        end = (self.raw_x, self.raw_y)
                        out.append((self.start or end, end))
            elif etype == 0 and self.down and self.start is None \
                    and self.raw_x is not None and self.raw_y is not None:
                self.start = (self.raw_x, self.raw_y)   # first full position of this touch
        return out

    def to_screen(self, raw, w, h):
        """Map a raw (x, y) to screen pixels using the calibration."""
        if not self.calib:
            return None
        c = self.calib
        rx = raw[0] if c['x_axis'] == 'x' else raw[1]
        ry = raw[1] if c['y_axis'] == 'y' else raw[0]
        sx = c['sx0'] + (rx - c['rx0']) * (c['sx1'] - c['sx0']) / (c['rx1'] - c['rx0'])
        sy = c['sy0'] + (ry - c['ry0']) * (c['sy1'] - c['sy0']) / (c['ry1'] - c['ry0'])
        return max(0, min(w - 1, int(sx))), max(0, min(h - 1, int(sy)))


# -----------------------------------------------------------------------------
#  moOde data and commands
# -----------------------------------------------------------------------------
def read_currentsong():
    data = {}
    try:
        with open(CURRENTSONG, encoding='utf-8', errors='replace') as f:
            for line in f:
                if '=' in line:
                    k, v = line.rstrip('\n').split('=', 1)
                    data[k] = v
    except FileNotFoundError:
        pass
    return data


def moode_cmd(cmd):
    try:
        url = API + urllib.parse.quote(cmd)
        with urllib.request.urlopen(url, timeout=3) as r:
            r.read()
    except Exception as e:
        log(f'Command "{cmd}" failed: {e}')


def renderer_name(song):
    f = song.get('file', '')
    return f[:-len(' Active')] if f.endswith(' Active') else None


def is_radio(song):
    return song.get('file', '').startswith('http') and not song.get('duration')


class CoverCache:
    def __init__(self, size):
        self.size = size
        self.url = None
        self.img = None

    def get(self, coverurl):
        if not coverurl:
            return None
        if coverurl == self.url:
            return self.img
        self.url, self.img = coverurl, None
        url = coverurl if coverurl.startswith('http') else WEB_ROOT + urllib.parse.quote(coverurl.lstrip('/'), safe='/:?=&%')
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                img = Image.open(io.BytesIO(r.read())).convert('RGB')
            self.img = ImageOps.fit(img, (self.size, self.size), Image.LANCZOS)
        except Exception as e:
            log(f'Cover art not loaded ({e})')
        return self.img


# -----------------------------------------------------------------------------
#  Drawing
# -----------------------------------------------------------------------------
def wrap(draw, text, fnt, max_w, max_lines):
    words, lines, cur = text.split(), [], ''
    for word in words:
        trial = f'{cur} {word}'.strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
            while draw.textlength(cur, font=fnt) > max_w and len(cur) > 1:
                cur = cur[:-1]
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and ' '.join(lines) != ' '.join(words):
        last = lines[-1]
        while last and draw.textlength(last + '…', font=fnt) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + '…'
    return lines


def fit_line(draw, text, fnt, max_w):
    return wrap(draw, text, fnt, max_w, 1)[0] if text else ''


class Screen:
    def __init__(self, w, h):
        self.w, self.h = w, h
        s = min(w / 480, h / 320)
        self.s = s
        self.art = int(232 * s)
        self.pad = int(10 * s)
        self.f_title = font(FONT_BOLD, int(24 * s))
        self.f_text = font(FONT_REG, int(17 * s))
        self.f_small = font(FONT_REG, int(13 * s))
        self.f_clock = font(FONT_BOLD, int(88 * s))
        self.f_date = font(FONT_REG, int(22 * s))
        self.progress_y = self.pad * 2 + self.art + int(6 * s)
        self.ctrl_y0 = self.progress_y + int(14 * s)
        self.ctrl_y1 = h - self.pad
        bw = int(76 * s)
        gap = int(8 * s)
        x = self.pad
        self.buttons = {}
        for name in ('prev', 'play', 'next'):
            self.buttons[name] = (x, self.ctrl_y0, x + bw, self.ctrl_y1)
            x += bw + gap
        vx1 = w - self.pad
        vbw = int(58 * s)
        self.buttons['vol_up'] = (vx1 - vbw, self.ctrl_y0, vx1, self.ctrl_y1)
        self.vol_text = (vx1 - vbw - int(70 * s), vx1 - vbw)
        self.buttons['vol_dn'] = (self.vol_text[0] - vbw, self.ctrl_y0, self.vol_text[0], self.ctrl_y1)

    def hit(self, pos):
        for name, (x0, y0, x1, y1) in self.buttons.items():
            if x0 <= pos[0] <= x1 and y0 <= pos[1] <= y1:
                return name
        return None

    def _icon(self, d, name, box, colour):
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        r = int(min(x1 - x0, y1 - y0) * 0.28)
        if name == 'play':
            d.polygon([(cx - r * 0.7, cy - r), (cx - r * 0.7, cy + r), (cx + r, cy)], fill=colour)
        elif name == 'pause':
            bw = r * 0.55
            d.rectangle([cx - r * 0.8, cy - r, cx - r * 0.8 + bw, cy + r], fill=colour)
            d.rectangle([cx + r * 0.8 - bw, cy - r, cx + r * 0.8, cy + r], fill=colour)
        elif name == 'stop':
            d.rectangle([cx - r * 0.8, cy - r * 0.8, cx + r * 0.8, cy + r * 0.8], fill=colour)
        elif name in ('next', 'prev'):
            sgn = 1 if name == 'next' else -1
            for off in (-0.55, 0.45):
                bx = cx + sgn * off * r
                d.polygon([(bx - sgn * r * 0.5, cy - r * 0.8), (bx - sgn * r * 0.5, cy + r * 0.8),
                           (bx + sgn * r * 0.5, cy)], fill=colour)
        elif name in ('vol_up', 'vol_dn'):
            t = max(3, int(r * 0.25))
            d.rectangle([cx - r * 0.8, cy - t // 2, cx + r * 0.8, cy + t // 2], fill=colour)
            if name == 'vol_up':
                d.rectangle([cx - t // 2, cy - r * 0.8, cx + t // 2, cy + r * 0.8], fill=colour)

    def _placeholder_art(self, d, x, y, label):
        a = self.art
        d.rounded_rectangle([x, y, x + a, y + a], radius=int(12 * self.s), fill=(30, 34, 46))
        cx, cy = x + a // 2, y + a // 2
        r = a // 6
        d.ellipse([cx - r - r // 2, cy + r // 2, cx - r // 2 + r // 2, cy + r + r // 2], fill=DIM)
        d.rectangle([cx - r // 2 + r // 4, cy - r * 1.4, cx - r // 2 + r // 2, cy + r], fill=DIM)
        d.polygon([(cx - r // 2 + r // 4, cy - r * 1.4), (cx + r, cy - r), (cx + r, cy - r * 0.6),
                   (cx - r // 2 + r // 2, cy - r)], fill=DIM)
        if label:
            tw = d.textlength(label, font=self.f_text)
            d.text((cx - tw / 2, y + a - int(36 * self.s)), label, font=self.f_text, fill=FG)

    def progress_fraction(self, song, snapshot_time):
        try:
            dur = float(song.get('duration') or 0)
            el = float(song.get('elapsed') or 0)
        except ValueError:
            return None
        if dur <= 0:
            return None
        if song.get('state') == 'play':
            el += time.monotonic() - snapshot_time
        return max(0.0, min(1.0, el / dur))

    def draw_progress(self, img, frac):
        d = ImageDraw.Draw(img)
        y = self.progress_y
        h = max(4, int(5 * self.s))
        d.rectangle([0, y - 2, self.w, y + h + 2], fill=BG)
        if frac is None:
            return
        x0, x1 = self.pad, self.w - self.pad
        d.rounded_rectangle([x0, y, x1, y + h], radius=h // 2, fill=FAINT)
        d.rounded_rectangle([x0, y, x0 + max(h, int((x1 - x0) * frac)), y + h], radius=h // 2, fill=ACCENT)

    def render_playing(self, song, cover, snapshot_time):
        img = Image.new('RGB', (self.w, self.h), BG)
        d = ImageDraw.Draw(img)
        p, a, s = self.pad, self.art, self.s
        renderer = renderer_name(song)
        radio = is_radio(song)

        # Album art
        if cover is not None:
            img.paste(cover, (p, p))
        else:
            self._placeholder_art(d, p, p, renderer or ('Radio' if radio else ''))

        # Text column
        tx = p * 2 + a
        tw = self.w - tx - p
        y = p
        title = song.get('title') or (renderer or os.path.basename(song.get('file', '')) or 'Unknown')
        for line in wrap(d, title, self.f_title, tw, 3):
            d.text((tx, y), line, font=self.f_title, fill=FG)
            y += int(29 * s)
        y += int(6 * s)
        artist = song.get('artist', '')
        if radio and not artist:
            artist = song.get('album', '')
        for line in wrap(d, artist, self.f_text, tw, 2):
            d.text((tx, y), line, font=self.f_text, fill=DIM)
            y += int(22 * s)
        album = song.get('album', '') if not radio else ''
        if album:
            y += int(2 * s)
            for line in wrap(d, album, self.f_text, tw, 2):
                d.text((tx, y), line, font=self.f_text, fill=DIM)
                y += int(22 * s)

        # Source and format, bottom of the text column
        source = renderer or ('Radio' if radio else 'Library')
        fmt = ' '.join(v for v in (song.get('encoded', ''), song.get('bitrate', '')) if v)
        if not fmt:
            out = song.get('outrate', '')
            fmt = '' if out in ('', 'Not playing') else out
        by = p + a - int(18 * s)
        d.text((tx, by), fit_line(d, fmt, self.f_small, tw), font=self.f_small, fill=DIM)
        badge_w = d.textlength(source, font=self.f_small) + int(14 * s)
        d.rounded_rectangle([tx, by - int(26 * s), tx + badge_w, by - int(6 * s)],
                            radius=int(6 * s), fill=ACCENT)
        d.text((tx + int(7 * s), by - int(24 * s)), source, font=self.f_small, fill=(255, 255, 255))

        # Controls: moOde's own player (MPD) can be controlled here. Renderers
        # (AirPlay, Spotify, Squeezelite, ...) are controlled from the sending app.
        if renderer is not None:
            hint = {'AirPlay': 'Control from your iPhone or Mac',
                    'Spotify': 'Control from the Spotify app',
                    'Squeezelite': 'Control from Music Assistant'}.get(renderer, f'Control from the {renderer} app')
            hw = d.textlength(hint, font=self.f_text)
            d.text(((self.w - hw) / 2, (self.ctrl_y0 + self.ctrl_y1) / 2 - int(10 * s)),
                   hint, font=self.f_text, fill=DIM)
            self.draw_progress(img, None)
            return img

        state = song.get('state', '')
        for name, box in self.buttons.items():
            d.rounded_rectangle(box, radius=int(10 * s), fill=BUTTON)
            icon = name
            if name == 'play':
                icon = ('stop' if radio else 'pause') if state == 'play' else 'play'
            self._icon(d, icon, box, FG)
        vol = song.get('volume', '')
        vol_label = ('Muted' if song.get('mute') == '1' else f'{vol}%') if vol != '' else ''
        if vol_label:
            vx0, vx1 = self.vol_text
            vw = d.textlength(vol_label, font=self.f_text)
            d.text(((vx0 + vx1 - vw) / 2, (self.ctrl_y0 + self.ctrl_y1) / 2 - int(10 * s)),
                   vol_label, font=self.f_text, fill=FG)

        self.draw_progress(img, self.progress_fraction(song, snapshot_time))
        return img

    def render_idle(self, black=False, cfg=None, readings=None, dots=False):
        cfg = cfg or DEFAULTS
        if black:
            return Image.new('RGB', (self.w, self.h), (0, 0, 0))
        if cfg.get('clock_style', 'analog') != 'text':
            name, faces = current_face(cfg)
            t = time.localtime()
            night = is_night(cfg, t)
            key = (name, night, cfg.get('date_font', 'auto'), int(cfg.get('date_font_index', 0)),
                   bool(cfg.get('clock_24h', False)), cfg.get('clock_numerals', 'all'))
            if getattr(self, '_faces', None) is None:
                self._faces = {}
            if key not in self._faces:
                self._faces[key] = make_face(name, self.w, self.h, cfg, night=night)
            face = self._faces[key]
            img = face.render(t, cfg.get('clock_seconds', True) and face.uses_seconds, readings or [])
            if dots:
                draw_page_dots(img, faces.index(name), len(faces))
            if night:
                img = night_filter(img, cfg)
            return img
        img = Image.new('RGB', (self.w, self.h), BG)
        d = ImageDraw.Draw(img)
        now = time.localtime()
        clock = time.strftime('%H:%M', now)
        date = time.strftime('%A, %d %B', now)
        cw = d.textlength(clock, font=self.f_clock)
        d.text(((self.w - cw) / 2, self.h * 0.22), clock, font=self.f_clock, fill=FG)
        dw = d.textlength(date, font=self.f_date)
        d.text(((self.w - dw) / 2, self.h * 0.22 + int(104 * self.s)), date, font=self.f_date, fill=DIM)
        host = socket.gethostname()
        hw = d.textlength(host, font=self.f_small)
        d.text(((self.w - hw) / 2, self.h - int(30 * self.s)), host, font=self.f_small, fill=FAINT)
        return img

    def render_test(self):
        img = Image.new('RGB', (self.w, self.h), (0, 0, 0))
        d = ImageDraw.Draw(img)
        bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
                (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
        bw = self.w / len(bars)
        for i, c in enumerate(bars):
            d.rectangle([int(i * bw), 0, int((i + 1) * bw), int(self.h * 0.6)], fill=c)
        d.rectangle([0, 0, self.w - 1, self.h - 1], outline=(255, 0, 0), width=3)
        d.text((self.pad, self.h * 0.66), 'moode-nowplaying test', font=self.f_title, fill=FG)
        d.text((self.pad, self.h * 0.66 + 34 * self.s), 'TOP-LEFT is here', font=self.f_text, fill=DIM)
        d.text((self.pad, self.h * 0.66 + 60 * self.s),
               'Bars: white yellow cyan green magenta red blue black', font=self.f_small, fill=DIM)
        return img


# -----------------------------------------------------------------------------
#  Analogue clock (bedside style: dial on the left, date on the right)
# -----------------------------------------------------------------------------
SS = 3  # supersampling factor for smooth, antialiased edges


class FaceBase:
    WEEKDAY = (255, 82, 72)      # coral weekday
    MONTH = (142, 142, 147)      # soft grey month
    DAY = (245, 245, 247)
    uses_seconds = True

    def _fonts(self, h, date_font, date_font_index, reading=25, label=17):
        s = h / 320
        self.date_font = date_font_path(date_font)
        self.date_font_index = date_font_index
        self.f_reading = font(self.date_font, int(reading * s), date_font_index)
        self.f_reading_label = font(self.date_font, int(label * s), date_font_index)

    def _reading(self, d, x, y, row, kind, label, text, colour, s):
        """One sensor row: a small icon, then the value."""
        ic = int(22 * s)
        cy = y + row / 2
        icol = colour or self.MONTH
        if kind == 'temperature':
            bw = ic * 0.32
            d.rounded_rectangle([x + ic / 2 - bw / 2, cy - ic / 2, x + ic / 2 + bw / 2, cy + ic * 0.2],
                                radius=bw / 2, outline=icol, width=max(2, int(2 * s)))
            d.ellipse([x + ic / 2 - ic * 0.26, cy + ic * 0.08, x + ic / 2 + ic * 0.26, cy + ic * 0.6], fill=icol)
        elif kind == 'humidity':
            d.polygon([(x + ic / 2, cy - ic * 0.55), (x + ic * 0.12, cy + ic * 0.1), (x + ic * 0.88, cy + ic * 0.1)],
                      fill=icol)
            d.ellipse([x + ic * 0.12, cy - ic * 0.25, x + ic * 0.88, cy + ic * 0.5], fill=icol)
        else:
            d.ellipse([x + ic * 0.15, cy - ic * 0.35, x + ic * 0.85, cy + ic * 0.35], fill=icol)
        tx = x + ic + int(10 * s)
        if label:
            lb = d.textbbox((0, 0), label, font=self.f_reading_label)
            d.text((tx, cy - (lb[3] + lb[1]) / 2), label, font=self.f_reading_label, fill=self.MONTH)
            tx += lb[2] + int(7 * s)
        vb = d.textbbox((0, 0), text, font=self.f_reading)
        d.text((tx, cy - (vb[3] + vb[1]) / 2), text, font=self.f_reading,
               fill=colour or self.DAY)


class AnalogClock(FaceBase):
    FACES = {
        # face colour, ring, major tick, minor tick, numerals, hour/min hands, centre
        'dark':  dict(bg=(0, 0, 0), face=(0, 0, 0), ring=None, major=(235, 235, 240),
                      minor=(95, 95, 102), num=(245, 245, 247), hand=(245, 245, 247),
                      cap=(0, 0, 0)),
        'light': dict(bg=(0, 0, 0), face=(250, 250, 250), ring=(225, 225, 228),
                      major=(28, 28, 30), minor=(170, 170, 175), num=(28, 28, 30),
                      hand=(28, 28, 30), cap=(250, 250, 250)),
    }
    SECOND = (255, 149, 0)       # warm orange seconds hand

    def __init__(self, w, h, face='dark', numerals='all', date_font='auto', date_font_index=0):
        self.w, self.h = w, h
        self.face = face if face in self.FACES else 'dark'
        self.numerals = numerals if numerals in ('all', 'quarters', 'none') else 'all'
        self.c = self.FACES[self.face]
        self.size = min(h, int(w * 0.62)) - int(h * 0.06)      # dial diameter
        self.dial_x = int((w * 0.58 - self.size) / 2) + int(w * 0.02)
        self.dial_y = (h - self.size) // 2
        self.dial = self._make_dial()
        s = h / 320
        self._fonts(h, date_font, date_font_index)
        self.f_week = font(self.date_font, int(40 * s), date_font_index)
        self.f_day = font(self.date_font, int(124 * s), date_font_index)
        self.f_day_small = font(self.date_font, int(76 * s), date_font_index)

    # The static dial is drawn once at high resolution and scaled down.
    def _make_dial(self):
        n = self.size * SS
        img = Image.new('RGB', (n, n), self.c['bg'])
        d = ImageDraw.Draw(img)
        r = n / 2
        if self.face == 'light':
            d.ellipse([0, 0, n - 1, n - 1], fill=self.c['face'])
            d.ellipse([0, 0, n - 1, n - 1], outline=self.c['ring'], width=int(n * 0.012))
        inset = n * (0.035 if self.face == 'light' else 0.01)
        for i in range(60):
            a = math.radians(i * 6)
            major = i % 5 == 0
            l0 = r - inset - (n * 0.058 if major else n * 0.026)
            l1 = r - inset
            wdt = int(n * (0.013 if major else 0.005))
            x0, y0 = r + l0 * math.sin(a), r - l0 * math.cos(a)
            x1, y1 = r + l1 * math.sin(a), r - l1 * math.cos(a)
            d.line([x0, y0, x1, y1], fill=self.c['major' if major else 'minor'], width=wdt)
        quarters = self.numerals == 'quarters'
        fnum = font(FONT_REG, int(n * (0.15 if quarters else 0.105)))
        rn = r - inset - n * (0.17 if quarters else 0.165)
        hours = [] if self.numerals == 'none' else ((12, 3, 6, 9) if quarters else range(1, 13))
        for hnum in hours:
            a = math.radians(hnum * 30)
            cx, cy = r + rn * math.sin(a), r - rn * math.cos(a)
            txt = str(hnum)
            box = d.textbbox((0, 0), txt, font=fnum)
            tw, th = box[2] - box[0], box[3] - box[1]
            d.text((cx - tw / 2 - box[0], cy - th / 2 - box[1]), txt, font=fnum, fill=self.c['num'])
        return img.resize((self.size, self.size), Image.LANCZOS)

    @staticmethod
    def _bar(d, r, angle, start, end, width, colour):
        """Rounded bar along a hand angle, from start to end (as fractions of r)."""
        a = math.radians(angle)
        sx, sy = math.sin(a), -math.cos(a)
        x0, y0 = r + start * r * sx, r + start * r * sy
        x1, y1 = r + end * r * sx, r + end * r * sy
        d.line([x0, y0, x1, y1], fill=colour, width=int(width))
        for x, y in ((x0, y0), (x1, y1)):
            d.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=colour)

    def _hands(self, t, seconds):
        n = self.size * SS
        r = n / 2
        layer = Image.new('RGBA', (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        sec = t.tm_sec
        minute = t.tm_min + sec / 60
        hour = (t.tm_hour % 12) + minute / 60
        hand = self.c['hand'] + (255,)
        # Hour and minute hands: thin neck from the centre, then a wide rounded bar
        for angle, length in ((hour * 30, 0.50), (minute * 6, 0.80)):
            self._bar(d, r, angle, 0.0, 0.16, n * 0.016, hand)
            self._bar(d, r, angle, 0.16, length, n * 0.042, hand)
        d.ellipse([r - n * 0.028, r - n * 0.028, r + n * 0.028, r + n * 0.028], fill=hand)
        if seconds:
            sc = self.SECOND + (255,)
            self._bar(d, r, sec * 6, -0.16, 0.90, n * 0.009, sc)
            d.ellipse([r - n * 0.022, r - n * 0.022, r + n * 0.022, r + n * 0.022], fill=sc)
        cap = self.c['cap'] + (255,)
        d.ellipse([r - n * 0.009, r - n * 0.009, r + n * 0.009, r + n * 0.009], fill=cap)
        return layer.resize((self.size, self.size), Image.LANCZOS)

    def render(self, t, seconds=True, readings=()):
        img = Image.new('RGB', (self.w, self.h), self.c['bg'])
        dial = self.dial.copy()
        hands = self._hands(t, seconds)
        dial.paste(hands, (0, 0), hands)
        img.paste(dial, (self.dial_x, self.dial_y))

        # Date panel (with sensor readings underneath, if any)
        d = ImageDraw.Draw(img)
        s = self.h / 320
        px0 = self.dial_x + self.size + int(self.w * 0.03)
        pw = self.w - px0 - int(self.w * 0.03)
        weekday = time.strftime('%a', t)
        month = time.strftime('%b', t)
        day = str(t.tm_mday)
        compact = bool(readings)
        f_day = self.f_day_small if compact else self.f_day
        ww = d.textlength(weekday + ' ', font=self.f_week)
        mw = d.textlength(month, font=self.f_week)
        top = self.h * (0.07 if compact else 0.25)
        x = px0 + (pw - ww - mw) / 2
        d.text((x, top), weekday + ' ', font=self.f_week, fill=self.WEEKDAY)
        d.text((x + ww, top), month, font=self.f_week, fill=self.MONTH)
        box = d.textbbox((0, 0), day, font=f_day)
        dw = box[2] - box[0]
        day_y = top + self.h * 0.13
        d.text((px0 + (pw - dw) / 2 - box[0], day_y), day, font=f_day, fill=self.DAY)

        if readings:
            y = self.h * 0.52
            row = min(self.h * 0.14, (self.h * 0.95 - y) / len(readings))
            for kind, label, text, colour in readings:
                self._reading(d, px0, y, row, kind, label, text, colour, s)
                y += row
        return img

class DigitalFace(FaceBase):
    """Large warm digital time, date and readings in a column on the right."""
    TIME = (255, 159, 10)
    uses_seconds = False

    def __init__(self, w, h, date_font='auto', date_font_index=0, h24=False):
        self.w, self.h, self.h24 = w, h, h24
        s = h / 320
        self._fonts(h, date_font, date_font_index, reading=21, label=14)
        self.f_date = font(self.date_font, int(26 * s), date_font_index)
        self.col = int(w * 0.27)
        self.pad = int(10 * s)
        # Size the time font once, using '8's so the size never jumps between minutes
        d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        max_w, max_h = w - self.col - self.pad * 3, h * 0.72
        self.f_time = {}
        for sample in ('8:88', '88:88'):
            size = int(300 * s)
            while size > 20:
                f = font(self.date_font, size, date_font_index)
                b = d.textbbox((0, 0), sample, font=f)
                if b[2] - b[0] <= max_w and b[3] - b[1] <= max_h:
                    break
                size -= 4
            self.f_time[len(sample)] = f

    def render(self, t, seconds=False, readings=()):
        img = Image.new('RGB', (self.w, self.h), (0, 0, 0))
        d = ImageDraw.Draw(img)
        s = self.h / 320
        txt = time.strftime('%H:%M', t) if self.h24 else f'{(t.tm_hour % 12) or 12}:{t.tm_min:02d}'
        f = self.f_time[5 if len(txt) == 5 else 4]
        b = d.textbbox((0, 0), txt, font=f)
        area_w = self.w - self.col - self.pad
        d.text((self.pad + (area_w - (b[2] - b[0])) / 2 - b[0], (self.h - (b[3] - b[1])) / 2 - b[1]),
               txt, font=f, fill=self.TIME)
        x = self.w - self.col
        wd = time.strftime('%a', t).upper() + ' '
        d.text((x, self.h * 0.1), wd, font=self.f_date, fill=self.TIME)
        d.text((x + d.textlength(wd, font=self.f_date), self.h * 0.1), str(t.tm_mday), font=self.f_date, fill=self.DAY)
        if readings:
            y = self.h * 0.26
            row = min(self.h * 0.15, (self.h * 0.92 - y) / len(readings))
            for kind, label, text, colour in readings:
                self._reading(d, x, y, row, kind, label, text, colour, s * 0.85)
                y += row
        return img


class WideFace(FaceBase):
    """Ticks radiating out to the edges of the whole screen, 12/3/6/9, date by the 3."""
    ACCENT = (110, 220, 150)     # mint seconds hand and date

    def __init__(self, w, h, date_font='auto', date_font_index=0):
        self.w, self.h = w, h
        s = h / 320
        self._fonts(h, date_font, date_font_index, reading=20, label=14)
        self.f_date = font(self.date_font, int(19 * s), date_font_index)
        self.R = int(h * 0.44)                      # hand reach
        self.dial = self._make_dial(s)

    def _make_dial(self, s):
        n_w, n_h = self.w * SS, self.h * SS
        img = Image.new('RGB', (n_w, n_h), (0, 0, 0))
        d = ImageDraw.Draw(img)
        cx, cy = n_w / 2, n_h / 2
        m = 5 * s * SS
        f_num = font(self.date_font, int(40 * s * SS), self.date_font_index)
        for i in range(60):
            a = math.radians(i * 6)
            dx, dy = math.sin(a), -math.cos(a)
            tx = ((n_w - m - cx) / dx) if dx > 1e-9 else ((m - cx) / dx if dx < -1e-9 else float('inf'))
            ty = ((n_h - m - cy) / dy) if dy > 1e-9 else ((m - cy) / dy if dy < -1e-9 else float('inf'))
            t = min(tx, ty)
            major = i % 5 == 0
            length = (24 if major else 11) * s * SS
            d.line([cx + (t - length) * dx, cy + (t - length) * dy, cx + t * dx, cy + t * dy],
                   fill=(235, 235, 240) if major else (95, 95, 102), width=int((3.2 if major else 1.4) * s * SS))
            if i % 15 == 0:
                label = str(12 if i == 0 else i // 5)
                tn = t - length - 30 * s * SS
                b = d.textbbox((0, 0), label, font=f_num)
                d.text((cx + tn * dx - (b[2] - b[0]) / 2 - b[0], cy + tn * dy - (b[3] - b[1]) / 2 - b[1]),
                       label, font=f_num, fill=(245, 245, 247))
        return img.resize((self.w, self.h), Image.LANCZOS)

    def render(self, t, seconds=True, readings=()):
        compact = bool(readings)
        scale = 0.66 if compact else 1.0
        dw, dh = int(self.w * scale), int(self.h * scale)
        left, top = (0, (self.h - dh) // 2) if compact else (0, 0)
        img = Image.new('RGB', (self.w, self.h), (0, 0, 0))
        img.paste(self.dial.resize((dw, dh), Image.LANCZOS) if compact else self.dial, (left, top))
        d = ImageDraw.Draw(img)
        s = self.h / 320
        cx, cy = left + (self.w / 2) * scale, top + (self.h / 2) * scale
        # Date between the centre and the 3.
        date = time.strftime('%a', t).upper() + f' {t.tm_mday}'
        date_font = font(self.date_font, max(8, int(19 * s * scale)), self.date_font_index)
        b = d.textbbox((0, 0), date, font=date_font)
        d.text((cx + self.w * 0.20 * scale - (b[2] - b[0]) / 2 - b[0], cy - (b[3] + b[1]) / 2), date,
               font=date_font, fill=self.ACCENT)
        # Hands, drawn in a square around the centre at high resolution
        n = 2 * self.R * SS
        r = n / 2
        layer = Image.new('RGBA', (n, n), (0, 0, 0, 0))
        hd = ImageDraw.Draw(layer)
        sec = t.tm_sec
        minute = t.tm_min + sec / 60
        hour = (t.tm_hour % 12) + minute / 60
        white = (245, 245, 247, 255)
        for angle, length in ((hour * 30, 0.52), (minute * 6, 0.92)):
            AnalogClock._bar(hd, r, angle, 0.0, 0.12, 3.2 * s * SS, white)
            AnalogClock._bar(hd, r, angle, 0.12, length, 8.5 * s * SS, white)
        hd.ellipse([r - 6 * s * SS, r - 6 * s * SS, r + 6 * s * SS, r + 6 * s * SS], fill=white)
        if seconds:
            acc = self.ACCENT + (255,)
            AnalogClock._bar(hd, r, sec * 6, -0.14, 1.0, 1.9 * s * SS, acc)
            hd.ellipse([r - 4.5 * s * SS, r - 4.5 * s * SS, r + 4.5 * s * SS, r + 4.5 * s * SS], fill=acc)
        hd.ellipse([r - 2 * s * SS, r - 2 * s * SS, r + 2 * s * SS, r + 2 * s * SS], fill=(0, 0, 0, 255))
        hand_size = int(2 * self.R * scale)
        hands = layer.resize((hand_size, hand_size), Image.LANCZOS)
        img.paste(hands, (int(cx - hand_size / 2), int(cy - hand_size / 2)), hands)
        if readings:
            row = min(38 * s, (self.h - 24 * s) / len(readings))
            y = (self.h - row * len(readings)) / 2
            for kind, label, text, colour in readings:
                self._reading(d, dw + 8 * s, y, row, kind, label, text, colour, s * 0.85)
                y += row
        return img


class PurpleDialFace(FaceBase):
    """Dark full-screen dial with a lavender date and seconds hand."""
    PURPLE = (174, 76, 255)

    def __init__(self, w, h, date_font='auto', date_font_index=0):
        self.w, self.h = w, h
        self.s = h / 320
        self.cx, self.cy = w / 2, h / 2
        self.R = min(h * 0.47, w * 0.47)
        self._fonts(h, date_font, date_font_index)
        self.f_hour = font(self.date_font, int(36 * self.s * SS), date_font_index)
        self.f_track = font(self.date_font, int(10 * self.s * SS), date_font_index)
        self.f_date = font(self.date_font, int(18 * self.s), date_font_index)
        self.dial = self._make_dial()

    def _make_dial(self):
        n = int(self.R * 2 * SS)
        r = n / 2
        img = Image.new('RGB', (n, n), (12, 12, 13))
        d = ImageDraw.Draw(img)
        for i in range(60):
            a = math.radians(i * 6)
            major = i % 5 == 0
            outer = r * 0.96
            inner = outer - (n * (0.047 if major else 0.021))
            x0, y0 = r + inner * math.sin(a), r - inner * math.cos(a)
            x1, y1 = r + outer * math.sin(a), r - outer * math.cos(a)
            width = max(1, int(n * (0.008 if major else 0.003)))
            d.line([x0, y0, x1, y1], fill=(190, 191, 197), width=width)
        for i in range(12):
            value = 60 if i == 0 else i * 5
            a = math.radians(i * 30)
            rr = r * 1.065
            txt = f'{value:02d}'
            b = d.textbbox((0, 0), txt, font=self.f_track)
            d.text((r + rr * math.sin(a) - (b[2]-b[0])/2 - b[0], r - rr * math.cos(a) - (b[3]-b[1])/2 - b[1]),
                   txt, font=self.f_track, fill=(225, 225, 230))
        for hnum in range(1, 13):
            a = math.radians(hnum * 30)
            rr = r * 0.72
            txt = str(hnum)
            b = d.textbbox((0, 0), txt, font=self.f_hour)
            d.text((r + rr * math.sin(a) - (b[2]-b[0])/2 - b[0], r - rr * math.cos(a) - (b[3]-b[1])/2 - b[1]),
                   txt, font=self.f_hour, fill=(250, 250, 252))
        return img.resize((int(self.R * 2), int(self.R * 2)), Image.LANCZOS)

    def render(self, t, seconds=True, readings=()):
        img = Image.new('RGB', (self.w, self.h), (12, 12, 13))
        compact = bool(readings)
        scale = 0.76 if compact else 1.0
        dial_size = int(self.R * 2 * scale)
        cx, cy = (self.w * 0.255, self.h / 2) if compact else (self.cx, self.cy)
        x, y = int(cx - dial_size / 2), int(cy - dial_size / 2)
        dial = self.dial.resize((dial_size, dial_size), Image.LANCZOS) if compact else self.dial
        img.paste(dial, (x, y))
        s = self.s
        n = int(self.R * 2 * SS)
        r = n / 2
        layer = Image.new('RGBA', (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        minute = t.tm_min + t.tm_sec / 60
        hour = (t.tm_hour % 12) + minute / 60
        white = (250, 250, 252, 255)
        for angle, length in ((hour * 30, 0.49), (minute * 6, 0.79)):
            a = math.radians(angle)
            ux, uy = math.sin(a), -math.cos(a)
            # thin stem then rounded thick bar
            AnalogClock._bar(d, r, angle, 0.0, 0.15, n * 0.012, white)
            AnalogClock._bar(d, r, angle, 0.15, length, n * 0.036, white)
        if seconds:
            purple = self.PURPLE + (255,)
            AnalogClock._bar(d, r, t.tm_sec * 6, -0.20, 0.91, n * 0.006, purple)
        d.ellipse([r - n * 0.030, r - n * 0.030, r + n * 0.030, r + n * 0.030], fill=self.PURPLE + (255,))
        d.ellipse([r - n * 0.014, r - n * 0.014, r + n * 0.014, r + n * 0.014], fill=(12, 12, 13, 255))
        layer = layer.resize((dial_size, dial_size), Image.LANCZOS)
        img.paste(layer, (x, y), layer)
        dd = ImageDraw.Draw(img)
        date = str(t.tm_mday)
        date_font = font(self.date_font, max(8, int(18 * s * scale)), self.date_font_index)
        b = dd.textbbox((0, 0), date, font=date_font)
        dx = cx + self.R * scale * 0.48
        dd.text((dx - (b[2]-b[0])/2 - b[0], cy - (b[3]+b[1])/2), date, font=date_font, fill=self.PURPLE)
        if readings:
            row = min(38 * s, (self.h - 24 * s) / len(readings))
            yy = (self.h - row * len(readings)) / 2
            for kind, label, text, colour in readings:
                self._reading(dd, int(self.w * 0.55), yy, row, kind, label, text, colour, s * 0.85)
                yy += row
        return img


class ReferenceFace(FaceBase):
    """Additional live faces based on the supplied watch-face references."""
    ORANGE = (255, 145, 24)
    MINT = (156, 210, 184)
    uses_seconds = True

    def __init__(self, w, h, style, date_font='auto', date_font_index=0, h24=False):
        self.w, self.h, self.style, self.h24 = w, h, style, h24
        self.s = h / 320
        self._fonts(h, date_font, date_font_index, reading=16, label=12)
        self._render_font = self.date_font
        self.uses_seconds = style != 'digital-orange'

    @staticmethod
    def _centered_text(d, xy, text, f, fill):
        b = d.textbbox((0, 0), text, font=f)
        d.text((xy[0] - (b[2]-b[0])/2 - b[0], xy[1] - (b[3]+b[1])/2 - b[1]), text, font=f, fill=fill)

    def _analog(self, t, readings, style, seconds):
        s, w, h = self.s, self.w, self.h
        bg = (9, 10, 11)
        if style == 'teal':
            bg = (11, 67, 69)
        img = Image.new('RGB', (w, h), bg)
        square = style in ('teal', 'white-square')
        compact = bool(readings)
        cx, cy = (w * 0.26 if compact else w / 2), h / 2
        r = min(h * (0.33 if compact else (0.425 if square else 0.41)),
                w * (0.23 if compact else (0.30 if square else 0.45)))
        if square:
            side = min(h * (0.76 if compact else 0.98), w * (0.52 if compact else 0.72))
            box = [cx - side/2, cy - side/2, cx + side/2, cy + side/2]
            d = ImageDraw.Draw(img)
            if style == 'teal':
                d.rounded_rectangle(box, radius=int(36*s), fill=(46, 103, 105), outline=(103, 157, 158), width=max(1, int(2*s)))
                face = (250, 250, 250)
                num = (22, 23, 25)
                hand = (27, 28, 30)
                minor, major = (175, 177, 180), (82, 84, 87)
            else:
                d.rounded_rectangle(box, radius=int(36*s), fill=(250, 250, 250))
                face = (250, 250, 250)
                num = (60, 60, 63)
                hand = (24, 25, 27)
                minor, major = (185, 187, 190), (75, 77, 80)
            d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=face)
        else:
            num, hand = (246, 246, 249), (249, 249, 251)
            minor, major = (83, 85, 90), (205, 207, 211)
        d = ImageDraw.Draw(img)
        # Ticks around the dial; the white square reference emphasizes quarters.
        for i in range(60):
            a = math.radians(i * 6)
            emphasized = i % (15 if style == 'white-square' else 5) == 0
            outer = r * 0.91
            inner = outer - r * (0.15 if emphasized else 0.075)
            p0 = (cx + inner*math.sin(a), cy - inner*math.cos(a))
            p1 = (cx + outer*math.sin(a), cy - outer*math.cos(a))
            d.line([p0, p1], fill=major if emphasized else minor, width=max(1, int((3.2 if emphasized else 1.5)*s)))
        quarters_only = style == 'white-square'
        nums = (12, 3, 6, 9) if quarters_only else range(1, 13)
        fn = font(self._render_font, int((30 if quarters_only else 22)*s), self.date_font_index)
        for hn in nums:
            a = math.radians(hn * 30)
            rr = r * (0.67 if quarters_only else 0.72)
            self._centered_text(d, (cx + rr*math.sin(a), cy - rr*math.cos(a)), str(hn), fn, num)
        # Dynamic hour and minute hands. The orange sweep hand matches refs 1 and 3.
        minute = t.tm_min + t.tm_sec / 60
        hour = (t.tm_hour % 12) + minute / 60
        for angle, length, width in ((hour*30, 0.51, 7), (minute*6, 0.73, 9)):
            a = math.radians(angle)
            p1 = (cx + length*r*math.sin(a), cy - length*r*math.cos(a))
            d.line([(cx, cy), p1], fill=hand, width=max(2, int(width*s)))
            rr = width*s/2
            d.ellipse([p1[0]-rr, p1[1]-rr, p1[0]+rr, p1[1]+rr], fill=hand)
        if seconds:
            a = math.radians(t.tm_sec * 6)
            p1 = (cx + r*0.88*math.sin(a), cy - r*0.88*math.cos(a))
            d.line([(cx, cy), p1], fill=self.ORANGE, width=max(1, int(1.8*s)))
        d.ellipse([cx-5*s, cy-5*s, cx+5*s, cy+5*s], fill=self.ORANGE)
        d.ellipse([cx-2*s, cy-2*s, cx+2*s, cy+2*s], fill=bg if style != 'white-square' else (250,250,250))
        if readings:
            row = min(38 * s, (h - 24 * s) / len(readings))
            yy = (h - row * len(readings)) / 2
            for kind, label, text, colour in readings:
                self._reading(d, int(w * 0.55), yy, row, kind, label, text, colour, s * 0.88)
                yy += row
        return img

    def render(self, t, seconds=True, readings=()):
        if self.style in ('teal', 'white-square'):
            return self._analog(t, readings, self.style, seconds)
        if self.style == 'calendar':
            img = Image.new('RGB', (self.w, self.h), (0, 0, 0))
            s = self.s
            cx, cy = self.h * 0.36, self.h * 0.51
            r = self.h * 0.34
            d = ImageDraw.Draw(img)
            for i in range(60):
                a = math.radians(i*6)
                major = i % 5 == 0
                r1, r2 = r*(0.84 if major else 0.93), r*0.99
                d.line([(cx+r1*math.sin(a),cy-r1*math.cos(a)),(cx+r2*math.sin(a),cy-r2*math.cos(a))],
                       fill=(225,225,230) if major else (85,86,91), width=max(1,int((2.2 if major else 1.2)*s)))
            for hn in (12,3,6,9):
                a=math.radians(hn*30); rr=r*0.70
                self._centered_text(d,(cx+rr*math.sin(a),cy-rr*math.cos(a)),str(hn),font(self._render_font,int(20*s),self.date_font_index),(248,248,250))
            minute=t.tm_min+t.tm_sec/60; hour=(t.tm_hour%12)+minute/60
            for angle,length,width in ((hour*30,.48,5),(minute*6,.70,5)):
                a=math.radians(angle); p=(cx+length*r*math.sin(a),cy-length*r*math.cos(a))
                d.line([(cx,cy),p],fill=(245,245,248),width=max(2,int(width*s)))
            if seconds:
                a=math.radians(t.tm_sec*6); p=(cx+r*.88*math.sin(a),cy-r*.88*math.cos(a))
                d.line([(cx,cy),p],fill=(205,185,160),width=max(1,int(s)))
            d.ellipse([cx-4*s,cy-4*s,cx+4*s,cy+4*s],outline=(225,210,170),width=max(1,int(s*2)))
            x=self.h*(.72 if readings else .75)
            d.text((x,self.h*.22),time.strftime('%a',t),font=font(self._render_font,int(22*s),self.date_font_index),fill=(235,80,92))
            month=time.strftime('%b',t)
            tw=d.textlength(month,font=font(self._render_font,int(22*s),self.date_font_index))
            d.text((x+tw+6*s,self.h*.22),month,font=font(self._render_font,int(22*s),self.date_font_index),fill=(185,187,194))
            day=str(t.tm_mday); fd=font(self._render_font,int((76 if readings else 105)*s),self.date_font_index)
            d.text((x,self.h*(.36 if readings else .37)),day,font=fd,fill=(250,250,252))
            if readings:
                row = min(34 * s, (self.h * 0.31) / len(readings))
                yy = self.h * 0.66
                for kind, label, text, colour in readings:
                    self._reading(d, int(self.h * 0.72), yy, row, kind, label, text, colour, s * 0.88)
                    yy += row
            return img
        if self.style == 'digital-orange':
            img=Image.new('RGB',(self.w,self.h),(0,0,0)); d=ImageDraw.Draw(img); s=self.s
            label=(time.strftime('%H:%M', t) if self.h24 else
                   f'{(t.tm_hour % 12) or 12}:{t.tm_min:02d}')
            left=font(self._render_font,int(110*s),self.date_font_index)
            while d.textlength(label,font=left)>self.w*(.66 if readings else .76) and left.size>48*s:
                left=font(self._render_font,int(left.size-2*s),self.date_font_index)
            d.text((self.w*.04,self.h*.18),label,font=left,fill=(255,166,105))
            right=self.w*(.71 if readings else .80)
            f=font(self._render_font,int(14*s),self.date_font_index)
            d.text((right,self.h*.19),time.strftime('%a',t).upper(),font=f,fill=(215,177,156))
            d.text((right,self.h*.27),str(t.tm_mday),font=f,fill=(235,235,238))
            if readings:
                row = min(34 * s, (self.h * 0.34) / len(readings))
                yy = self.h * 0.39
                for kind, label, text, colour in readings:
                    self._reading(d, int(right), yy, row, kind, label, text, colour, s * 0.78)
                    yy += row
            return img
        # Minimal wide dial with cardinal numerals and contrasting quarter marks.
        img=Image.new('RGB',(self.w,self.h),(0,0,0)); d=ImageDraw.Draw(img)
        compact=bool(readings)
        cx,cy=(self.w*.255 if compact else self.w/2),self.h/2
        r=min(self.h*(.34 if compact else .43),self.w*(.23 if compact else .31)); s=self.s
        for i in range(60):
            a=math.radians(i*6); major=i%5==0; quarter=i%15==0
            o=r*.97; inn=o-(.17 if quarter else (.095 if major else .045))*r
            d.line([(cx+inn*math.sin(a),cy-inn*math.cos(a)),(cx+o*math.sin(a),cy-o*math.cos(a))],
                   fill=(225,226,230) if quarter else ((155,158,164) if major else (62,64,70)),
                   width=max(1,int((2.5 if quarter else 1.3)*s)))
        for hn in (12,3,6,9):
            a=math.radians(hn*30); rr=r*.71
            self._centered_text(d,(cx+rr*math.sin(a),cy-rr*math.cos(a)),str(hn),font(self._render_font,int(24*s),self.date_font_index),(245,245,248))
        minute=t.tm_min+t.tm_sec/60; hour=(t.tm_hour%12)+minute/60
        for angle,length,width in ((hour*30,.48,5),(minute*6,.75,6)):
            a=math.radians(angle); p=(cx+length*r*math.sin(a),cy-length*r*math.cos(a))
            d.line([(cx,cy),p],fill=(248,248,250),width=max(2,int(width*s)))
        if seconds:
            a=math.radians(t.tm_sec*6); p=(cx+r*.92*math.sin(a),cy-r*.92*math.cos(a))
            d.line([(cx,cy),p],fill=self.MINT,width=max(1,int(s)))
        d.ellipse([cx-4*s,cy-4*s,cx+4*s,cy+4*s],fill=self.MINT)
        d.text((cx+r*.49,cy-8*s),f'{time.strftime("%a",t).upper()} {t.tm_mday}',font=font(self._render_font,int(12*s),self.date_font_index),fill=self.MINT)
        if readings:
            row=min(38*s,(self.h-24*s)/len(readings)); yy=(self.h-row*len(readings))/2
            for kind,label,text,colour in readings:
                self._reading(d,int(self.w*.55),yy,row,kind,label,text,colour,s*.85)
                yy+=row
        return img


class GradientFace(FaceBase):
    """Original design: soft glow, slim batons, tapered hands, large indoor temperature."""
    HAND = (242, 237, 228)       # warm white
    ACCENT = (61, 214, 198)      # teal seconds hand
    GREY = (150, 152, 160)

    def __init__(self, w, h, date_font='auto', date_font_index=0):
        self.w, self.h = w, h
        s = h / 320
        self.s = s
        self._fonts(h, date_font, date_font_index, reading=19, label=15)
        di = date_font_index
        self.f_weekday = font(self.date_font, int(19 * s), di)
        self.f_date = font(self.date_font, int(27 * s), di)
        self.f_big = font(self.date_font, int(64 * s), di)
        self.f_small = font(self.date_font, int(18 * s), di)
        self.f_pill = font(self.date_font, int(16 * s), di)
        self.size = int(h * 0.90)
        self.dx = int(w * 0.035)
        self.dy = (h - self.size) // 2
        self.bg = self._make_background()

    def _make_background(self):
        n = self.size * SS
        # Soft radial glow behind the dial
        glow = Image.radial_gradient('L').resize((self.w, self.h), Image.BICUBIC)
        glow = ImageOps.invert(glow)
        bg = ImageOps.colorize(glow, black=(0, 0, 0), white=(34, 44, 66))
        mask = Image.new('L', (self.w, self.h), 0)
        cx, cy = self.dx + self.size / 2, self.dy + self.size / 2
        rr = self.size * 0.62
        ImageDraw.Draw(mask).ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(self.size * 0.18))
        img = Image.composite(bg, Image.new('RGB', (self.w, self.h), (0, 0, 0)), mask)
        # Dial markers drawn at high resolution
        layer = Image.new('RGBA', (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        r = n / 2
        for i in range(60):
            a = math.radians(i * 6)
            sx, sy = math.sin(a), -math.cos(a)
            if i % 5 == 0:
                for off in ((-0.012, 0.012) if i == 0 else (0,)):
                    ax = math.radians(i * 6) + off
                    bx, by = math.sin(ax), -math.cos(ax)
                    width = n * 0.018
                    p0 = (r + bx * r * 0.80, r + by * r * 0.80)
                    p1 = (r + bx * r * 0.93, r + by * r * 0.93)
                    d.line([p0, p1], fill=(235, 232, 225, 255), width=int(width))
                    for (x, y) in (p0, p1):
                        d.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=(235, 232, 225, 255))
            else:
                x, y = r + sx * r * 0.925, r + sy * r * 0.925
                rad = n * 0.0045
                d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(120, 124, 136, 255))
        layer = layer.resize((self.size, self.size), Image.LANCZOS)
        img.paste(layer, (self.dx, self.dy), layer)
        return img

    @staticmethod
    def _tapered(d, r, angle, length, w0, w1, colour, tail=0.0):
        a = math.radians(angle)
        sx, sy = math.sin(a), -math.cos(a)
        px, py = -sy, sx                       # perpendicular
        base = (r - sx * r * tail, r - sy * r * tail)
        tip = (r + sx * r * length, r + sy * r * length)
        pts = [(base[0] + px * w0 / 2, base[1] + py * w0 / 2), (tip[0] + px * w1 / 2, tip[1] + py * w1 / 2),
               (tip[0] - px * w1 / 2, tip[1] - py * w1 / 2), (base[0] - px * w0 / 2, base[1] - py * w0 / 2)]
        d.polygon(pts, fill=colour)
        d.ellipse([tip[0] - w1 / 2, tip[1] - w1 / 2, tip[0] + w1 / 2, tip[1] + w1 / 2], fill=colour)

    def render(self, t, seconds=True, readings=()):
        img = self.bg.copy()
        n = self.size * SS
        r = n / 2
        layer = Image.new('RGBA', (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        sec = t.tm_sec
        minute = t.tm_min + sec / 60
        hour = (t.tm_hour % 12) + minute / 60
        hand = self.HAND + (255,)
        self._tapered(d, r, hour * 30, 0.50, n * 0.040, n * 0.016, hand, tail=0.06)
        self._tapered(d, r, minute * 6, 0.78, n * 0.032, n * 0.011, hand, tail=0.06)
        d.ellipse([r - n * 0.026, r - n * 0.026, r + n * 0.026, r + n * 0.026], fill=hand)
        if seconds:
            acc = self.ACCENT + (255,)
            a = math.radians(sec * 6)
            sx, sy = math.sin(a), -math.cos(a)
            d.line([r - sx * r * 0.20, r - sy * r * 0.20, r + sx * r * 0.86, r + sy * r * 0.86],
                   fill=acc, width=int(n * 0.007))
            cwx, cwy = r - sx * r * 0.20, r - sy * r * 0.20
            d.ellipse([cwx - n * 0.016, cwy - n * 0.016, cwx + n * 0.016, cwy + n * 0.016], fill=acc)
            d.ellipse([r - n * 0.017, r - n * 0.017, r + n * 0.017, r + n * 0.017], fill=acc)
        d.ellipse([r - n * 0.007, r - n * 0.007, r + n * 0.007, r + n * 0.007], fill=(20, 24, 34, 255))
        layer = layer.resize((self.size, self.size), Image.LANCZOS)
        img.paste(layer, (self.dx, self.dy), layer)

        # Side panel
        dd = ImageDraw.Draw(img)
        s = self.s
        x = self.dx + self.size + int(self.w * 0.04)
        y = self.h * 0.10
        dd.text((x, y), time.strftime('%A', t).upper(), font=self.f_weekday, fill=self.ACCENT)
        dd.text((x, y + 24 * s), f'{t.tm_mday} {time.strftime("%B", t)}', font=self.f_date, fill=self.HAND)

        temps = [rd for rd in readings if rd[0] == 'temperature']
        hums = [rd for rd in readings if rd[0] == 'humidity']
        aqis = [rd for rd in readings if rd[0] == 'aqi']
        panel_w = self.w - x - 8 * s
        yy = self.h * 0.36
        if temps:
            kind, label, text, colour = temps[0]
            big = text.replace('°C', '°').replace('°F', '°')
            fb = self.f_big
            size = int(64 * s)
            while dd.textlength(big, font=fb) > panel_w and size > 30:
                size -= 2
                fb = font(self.date_font, size, self.date_font_index)
            dd.text((x, yy), big, font=fb, fill=colour or self.HAND)
            yy += 74 * s
            if label:
                dd.text((x, yy - 6 * s), label.upper(), font=self.f_pill, fill=self.GREY)
                yy += 16 * s
        small = []
        for kind, label, text, colour in temps[1:2] + hums[:1]:
            small.append((f'{label} ' if label else '') + text.replace('°C', '°'))
        if small:
            dd.text((x, yy), '   '.join(small), font=self.f_small, fill=self.GREY)
            yy += 30 * s
        if aqis:
            kind, label, text, colour = aqis[0]
            try:
                cat = naqi_category(float(text.split()[-1]))
            except ValueError:
                cat = ''
            pill = f'{text}  ·  {cat}' if cat else text
            below = ''
            if dd.textlength(pill, font=self.f_pill) + 20 * s > panel_w:
                pill, below = text, cat
            b = dd.textbbox((0, 0), pill, font=self.f_pill)
            pw, ph = b[2] - b[0] + 20 * s, b[3] - b[1] + 12 * s
            fill = colour or (70, 72, 80)
            dd.rounded_rectangle([x, yy, x + pw, yy + ph], radius=ph / 2, fill=fill)
            lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
            dd.text((x + 10 * s - b[0], yy + 6 * s - b[1]), pill, font=self.f_pill,
                    fill=(15, 15, 18) if lum > 140 else (245, 245, 247))
            if below:
                dd.text((x + 2 * s, yy + ph + 5 * s), below, font=self.f_pill, fill=fill)
        return img


def naqi_category(value):
    for limit, name in ((50, 'Good'), (100, 'Satisfactory'), (200, 'Moderate'),
                        (300, 'Poor'), (400, 'Very poor')):
        if value <= limit:
            return name
    return 'Severe'


# Clock faces, in swipe order
FACE_NAMES = ['dark', 'light-minimal', 'light', 'digital', 'wide', 'purple', 'teal',
              'white-square', 'calendar', 'digital-orange', 'wide-minimal', 'gradient']


def make_face(name, w, h, cfg, night=False):
    df, di = cfg.get('date_font', 'auto'), int(cfg.get('date_font_index', 0))
    if name == 'light-minimal':
        return AnalogClock(w, h, 'dark' if night else 'light', 'quarters', df, di)
    if name == 'light':
        return AnalogClock(w, h, 'dark' if night else 'light', 'all', df, di)
    if name == 'digital':
        return DigitalFace(w, h, df, di, bool(cfg.get('clock_24h', False)))
    if name == 'wide':
        return WideFace(w, h, df, di)
    if name == 'purple':
        return PurpleDialFace(w, h, df, di)
    if name in ('teal', 'white-square', 'calendar', 'digital-orange', 'wide-minimal'):
        return ReferenceFace(w, h, name, df, di, bool(cfg.get('clock_24h', False)))
    if name == 'gradient':
        return GradientFace(w, h, df, di)
    return AnalogClock(w, h, 'dark', cfg.get('clock_numerals', 'all'), df, di)


def current_face(cfg):
    faces = [f for f in (cfg.get('faces') or FACE_NAMES) if f in FACE_NAMES] or FACE_NAMES
    face = cfg.get('face')
    if face not in faces:
        # Older settings: clock_face / clock_numerals
        if cfg.get('clock_style') == 'digital':
            face = 'digital'
        elif cfg.get('clock_face') == 'light':
            face = 'light-minimal' if cfg.get('clock_numerals') == 'quarters' else 'light'
        else:
            face = 'dark'
        face = face if face in faces else faces[0]
    return face, faces


def _minutes(hhmm, default):
    try:
        h, m = (int(v) for v in str(hhmm).split(':'))
        return (h % 24) * 60 + (m % 60)
    except ValueError:
        return default


def is_night(cfg, t):
    mode = str(cfg.get('night_mode', 'auto')).lower()
    if mode in ('on', 'true', '1'):
        return True
    if mode in ('off', 'false', '0'):
        return False
    start = _minutes(cfg.get('night_start', '22:00'), 22 * 60)
    end = _minutes(cfg.get('night_end', '06:30'), 6 * 60 + 30)
    now = t.tm_hour * 60 + t.tm_min
    if start == end:
        return False
    return (start <= now < end) if start < end else (now >= start or now < end)


def night_filter(img, cfg):
    """Dim the clock for the night (light faces are already swapped for dark ones)."""
    level = max(0.1, min(1.0, float(cfg.get('night_brightness', 0.45))))
    if str(cfg.get('night_style', 'dim')).lower() == 'red':
        img = ImageOps.colorize(img.convert('L'), black=(0, 0, 0), white=(255, 52, 36))
    return ImageEnhance.Brightness(img).enhance(level)


def draw_page_dots(img, index, count):
    d = ImageDraw.Draw(img)
    s = img.height / 320
    r, gap = 4 * s, 14 * s
    total = (count - 1) * gap
    cx, cy = img.width / 2, img.height - 14 * s
    d.rounded_rectangle([cx - total / 2 - 10 * s, cy - 9 * s, cx + total / 2 + 10 * s, cy + 9 * s],
                        radius=9 * s, fill=(30, 30, 34))
    for i in range(count):
        x = cx - total / 2 + i * gap
        d.ellipse([x - r, cy - r, x + r, cy + r], fill=(245, 245, 247) if i == index else (100, 100, 106))


def save_setting(key, value):
    """Update one key in the settings file, keeping everything else as it is."""
    try:
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        data[key] = value
        tmp = CONFIG_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        log(f'Could not save {key}: {e}')


# -----------------------------------------------------------------------------
#  MQTT sensor readings (temperature, humidity, AQI) from a Mosquitto broker
# -----------------------------------------------------------------------------
# AQI colour bands. 'IN' = India's National AQI (CPCB), 'US' = US EPA.
AQI_BANDS = {
    'IN': [(50, (0, 176, 80)), (100, (146, 208, 80)), (200, (255, 214, 0)),
           (300, (255, 140, 0)), (400, (255, 59, 48)), (10 ** 9, (165, 30, 60))],
    'US': [(50, (0, 200, 83)), (100, (255, 214, 0)), (150, (255, 140, 0)),
           (200, (255, 59, 48)), (300, (163, 73, 164)), (10 ** 9, (126, 0, 35))],
}


def aqi_colour(value, scale='IN'):
    for limit, colour in AQI_BANDS.get(scale, AQI_BANDS['IN']):
        if value <= limit:
            return colour
    return None


# India National AQI breakpoints (CPCB): (concentration µg/m³, AQI) pairs, linear in between.
# The official index uses 24-hour averages; from a live sensor this is an instantaneous estimate.
NAQI_PM25 = [(0, 0), (30, 50), (60, 100), (90, 200), (120, 300), (250, 400), (380, 500)]
NAQI_PM10 = [(0, 0), (50, 50), (100, 100), (250, 200), (350, 300), (430, 400), (510, 500)]


def _subindex(c, table):
    if c is None:
        return None
    c = max(0.0, c)
    for (c0, i0), (c1, i1) in zip(table, table[1:]):
        if c <= c1:
            return i0 + (c - c0) * (i1 - i0) / (c1 - c0)
    return 500.0


def naqi(pm25=None, pm10=None):
    """Indian AQI from PM2.5 and/or PM10: the higher of the two sub-indices."""
    subs = [v for v in (_subindex(pm25, NAQI_PM25), _subindex(pm10, NAQI_PM10)) if v is not None]
    return max(subs) if subs else None


def json_path(obj, path):
    for part in path.split('.'):
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


class Sensors:
    """Subscribes to MQTT topics and keeps the latest values.

    Config ("mqtt" in /etc/moode-nowplaying.json):
      {"host": "192.168.1.50", "port": 1883, "username": "", "password": "",
       "aqi_scale": "IN", "stale_after_min": 30,
       "sensors": [
         {"type": "temperature", "topic": "home/living/temperature", "json_key": "", "unit": "°C", "decimals": 1},
         {"type": "humidity",    "topic": "home/living/humidity",    "json_key": "", "unit": "%",  "decimals": 0},
         {"type": "aqi",         "topic": "home/living/aqi",         "json_key": "", "unit": "",   "decimals": 0}]}
    "json_key" picks a field from a JSON payload, e.g. "temperature" or "sensor.aqi".
    "label" is shown before the value, e.g. "In" / "Out".
    For an AQI calculated from a particulate sensor, give "pm25_key" and/or "pm10_key"
    instead of "json_key", e.g. {"type": "aqi", "topic": "home/pms5003/state",
    "pm25_key": "pm2_5", "pm10_key": "pm10"}.
    """

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.values = {}          # index -> (float value, monotonic time)
        self.version = 0
        self.client = None
        if not self.cfg.get('host') or not self.cfg.get('sensors'):
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log('MQTT: python3-paho-mqtt is not installed; sensor readings disabled')
            return
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'moode-nowplaying-{socket.gethostname()}')
        except AttributeError:  # paho-mqtt 1.x
            self.client = mqtt.Client(client_id=f'moode-nowplaying-{socket.gethostname()}')
        if self.cfg.get('username'):
            self.client.username_pw_set(self.cfg['username'], self.cfg.get('password') or None)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        try:
            self.client.connect_async(self.cfg['host'], int(self.cfg.get('port', 1883)), keepalive=60)
            self.client.loop_start()
            log(f"MQTT: connecting to {self.cfg['host']}:{self.cfg.get('port', 1883)}")
        except Exception as e:
            log(f'MQTT: could not start ({e})')
            self.client = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = (reason_code == 0) if isinstance(reason_code, int) else not getattr(reason_code, 'is_failure', False)
        if not ok:
            log(f'MQTT: connection refused ({reason_code}). Check username/password.')
            return
        log('MQTT: connected')
        for topic in {s['topic'] for s in self.cfg['sensors'] if s.get('topic')}:
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode('utf-8', errors='replace').strip()
        for i, s in enumerate(self.cfg['sensors']):
            if s.get('topic') != msg.topic:
                continue
            try:
                if s.get('pm25_key') or s.get('pm10_key'):
                    obj = json.loads(payload)
                    pm25 = float(json_path(obj, s['pm25_key'])) if s.get('pm25_key') else None
                    pm10 = float(json_path(obj, s['pm10_key'])) if s.get('pm10_key') else None
                    value = naqi(pm25, pm10)
                    if value is None:
                        continue
                else:
                    raw = payload
                    if s.get('json_key'):
                        raw = json_path(json.loads(payload), s['json_key'])
                    value = float(raw)
            except Exception:
                continue
            old = self.values.get(i, (None, 0))[0]
            self.values[i] = (value, time.monotonic())
            if old is None or round(old, int(s.get('decimals', 1))) != round(value, int(s.get('decimals', 1))):
                self.version += 1

    def readings(self):
        """List of (kind, label, text, colour) for display. Stale values are greyed."""
        out = []
        stale_s = float(self.cfg.get('stale_after_min', 30)) * 60
        for i, s in enumerate(self.cfg.get('sensors', [])):
            kind = s.get('type', 'other')
            label = s.get('label', '')
            if i not in self.values:
                out.append((kind, label, '--', (90, 90, 96)))
                continue
            value, ts = self.values[i]
            dec = int(s.get('decimals', 0 if kind in ('humidity', 'aqi') else 1))
            num = f'{value:.{dec}f}'
            text = f'AQI {num}' if kind == 'aqi' else f"{num}{s.get('unit', '')}"
            if time.monotonic() - ts > stale_s:
                colour = (90, 90, 96)
            elif kind == 'aqi':
                colour = aqi_colour(value, self.cfg.get('aqi_scale', 'IN'))
            else:
                colour = None
            out.append((kind, label, text, colour))
        return out


# -----------------------------------------------------------------------------
#  Calibration
# -----------------------------------------------------------------------------
def calibrate(fb, scr):
    dev = find_touch_device()
    if not dev:
        sys.exit('No touchscreen found.')
    fd = os.open(dev, os.O_RDONLY)
    margin = int(40 * scr.s)
    targets = [('top-left', (margin, margin)),
               ('top-right', (scr.w - margin, margin)),
               ('bottom-left', (margin, scr.h - margin))]
    raws = []
    for label, (tx, ty) in targets:
        img = Image.new('RGB', (scr.w, scr.h), BG)
        d = ImageDraw.Draw(img)
        r = int(16 * scr.s)
        d.line([tx - r, ty, tx + r, ty], fill=ACCENT, width=3)
        d.line([tx, ty - r, tx, ty + r], fill=ACCENT, width=3)
        d.ellipse([tx - 4, ty - 4, tx + 4, ty + 4], fill=FG)
        msg = f'Tap the {label} cross'
        mw = d.textlength(msg, font=scr.f_text)
        d.text(((scr.w - mw) / 2, scr.h / 2 - 12), msg, font=scr.f_text, fill=FG)
        fb.show(img)
        log(f'Tap the {label} target...')
        xs, ys, rx, ry, down = [], [], None, None, False
        while True:
            data = os.read(fd, EVENT_SIZE * 64)
            for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, et, code, val = struct.unpack_from(EVENT_FMT, data, i)
                if et == EV_ABS and code == ABS_X:
                    rx = val
                elif et == EV_ABS and code == ABS_Y:
                    ry = val
                elif et == EV_KEY and code == BTN_TOUCH:
                    down = val == 1
                    if not down and xs:
                        break
                if down and rx is not None and ry is not None and et == 0:
                    xs.append(rx)
                    ys.append(ry)
            if not down and xs:
                break
        xs.sort()
        ys.sort()
        raws.append((xs[len(xs) // 2], ys[len(ys) // 2]))
        log(f'  raw {raws[-1]}')
        time.sleep(0.4)
    os.close(fd)

    (tlx, tly), (trx, try_), (blx, bly) = raws
    # The raw axis that changes most between top-left and top-right is the screen X axis
    x_axis = 'x' if abs(trx - tlx) >= abs(try_ - tly) else 'y'
    y_axis = 'y' if x_axis == 'x' else 'x'
    pick = (lambda p, ax: p[0] if ax == 'x' else p[1])
    calib = {
        'x_axis': x_axis, 'y_axis': y_axis,
        'rx0': pick(raws[0], x_axis), 'rx1': pick(raws[1], x_axis),
        'sx0': targets[0][1][0], 'sx1': targets[1][1][0],
        'ry0': pick(raws[0], y_axis), 'ry1': pick(raws[2], y_axis),
        'sy0': targets[0][1][1], 'sy1': targets[2][1][1],
    }
    if calib['rx0'] == calib['rx1'] or calib['ry0'] == calib['ry1']:
        sys.exit('Calibration failed: taps were too close together. Run --calibrate again.')
    cfg = load_config()
    cfg['touch'] = calib
    save_config(cfg)
    img = scr.render_idle(cfg=dict(DEFAULTS, clock_style='digital'))
    ImageDraw.Draw(img).text((scr.pad, scr.pad), 'Calibration saved', font=scr.f_text, fill=ACCENT)
    fb.show(img)


# -----------------------------------------------------------------------------
#  Main loop
# -----------------------------------------------------------------------------
def run(cfg):
    fb = Framebuffer(cfg['fb_device'])
    scr = Screen(fb.width, fb.height)
    touch = Touch(cfg.get('touch'))
    covers = CoverCache(scr.art)
    sensors = Sensors(cfg.get('mqtt'))

    song, song_key, snapshot = {}, None, time.monotonic()
    mtime = None
    frame = None
    idle_since = None
    last_clock = None
    last_progress = 0.0
    force = True
    warned_meta = False
    dots_until = 0.0

    while True:
        # Touch
        wait = [touch] if touch.fd is not None else []
        ready, _, _ = select.select(wait, [], [], 0.25)
        if ready:
            for raw0, raw1 in touch.read_gestures():
                p0 = touch.to_screen(raw0, scr.w, scr.h)
                pos = touch.to_screen(raw1, scr.w, scr.h)
                if pos is None:
                    continue
                dx, dy = pos[0] - p0[0], pos[1] - p0[1]
                swipe = abs(dy) >= SWIPE_MIN * scr.s and abs(dy) > abs(dx) * 1.2
                if idle_since:
                    # Clock screen: swipe up = next face, swipe down = previous; any touch wakes it
                    idle_since = time.monotonic()
                    if swipe:
                        name, faces = current_face(cfg)
                        step_dir = 1 if dy < 0 else -1
                        cfg['face'] = faces[(faces.index(name) + step_dir) % len(faces)]
                        save_setting('face', cfg['face'])
                        dots_until = time.monotonic() + 2.0
                        log(f"Clock face: {cfg['face']}")
                    force = True
                    continue
                if swipe or abs(dx) > SWIPE_MIN * scr.s or renderer_name(song) is not None or frame is None:
                    continue
                button = scr.hit(pos)
                step = cfg['volume_step']
                action = {'prev': 'previous', 'next': 'next', 'play': 'toggle_play_pause',
                          'vol_up': f'set_volume -up {step}', 'vol_dn': f'set_volume -dn {step}'}.get(button)
                if action:
                    moode_cmd(action)
                    force = True

        # Metadata
        try:
            m = os.stat(CURRENTSONG).st_mtime
        except FileNotFoundError:
            m = None
            if not warned_meta:
                log(f'{CURRENTSONG} not found. Turn on Configure -> Audio -> MPD Options -> Metadata file.')
                warned_meta = True
        if m != mtime or force:
            mtime = m
            song = read_currentsong()
            snapshot = time.monotonic()

        renderer = renderer_name(song)
        playing = (song.get('state') in ('play', 'pause') and song.get('file')) or \
                  (renderer is not None and song.get('outrate', 'Not playing') != 'Not playing')

        now = time.monotonic()
        if playing:
            idle_since = None
            key = tuple(song.get(k, '') for k in
                        ('file', 'title', 'artist', 'album', 'coverurl', 'state', 'volume', 'mute',
                         'encoded', 'bitrate', 'outrate'))
            if key != song_key or force:
                song_key = key
                frame = scr.render_playing(song, covers.get(song.get('coverurl', '')), snapshot)
                fb.show(frame)
                last_progress = now
            elif song.get('state') == 'play' and now - last_progress >= cfg['progress_interval_s']:
                frac = scr.progress_fraction(song, snapshot)
                if frac is not None:
                    scr.draw_progress(frame, frac)
                    fb.show(frame, scr.progress_y - 2, scr.progress_y + int(8 * scr.s) + 3)
                last_progress = now
        else:
            if idle_since is None:
                idle_since = now
                song_key = None
                last_clock = None
            black_after = cfg['idle_black_after_min'] * 60
            black = (not cfg['idle_clock']) or (black_after and now - idle_since > black_after)
            face_name = current_face(cfg)[0]
            tick = '%H:%M:%S' if cfg['clock_seconds'] and face_name != 'digital' else '%H:%M'
            dots = now < dots_until
            clock_key = 'black' if black else (time.strftime(tick), sensors.version, face_name, dots)
            if clock_key != last_clock or force:
                last_clock = clock_key
                frame = None
                fb.show(scr.render_idle(black=black, cfg=cfg, readings=sensors.readings(), dots=dots))
        force = False


def main():
    ap = argparse.ArgumentParser(description='Now playing screen for moOde')
    ap.add_argument('--test', action='store_true', help='show a test pattern and exit')
    ap.add_argument('--calibrate', action='store_true', help='calibrate the touchscreen')
    args = ap.parse_args()
    cfg = load_config()
    if args.test or args.calibrate:
        fb = Framebuffer(cfg['fb_device'])
        scr = Screen(fb.width, fb.height)
        if args.test:
            fb.show(scr.render_test())
            log('Test pattern shown.')
        else:
            calibrate(fb, scr)
        return
    while True:
        try:
            run(cfg)
        except KeyboardInterrupt:
            return
        except Exception as e:
            log(f'Error: {e}. Restarting in 5 s.')
            time.sleep(5)


if __name__ == '__main__':
    main()
