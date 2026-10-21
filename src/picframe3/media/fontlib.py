"""A small, curated catalog of fonts each on-screen widget can pick from by
name, instead of typing a filesystem path -- a font *library* rather than
making every element's font setting a raw ``.ttf`` path to hunt down by
hand.

Covers the families picframe3 already knows how to find on its own (see
:data:`picframe3.gfx.textstyle.FONT_CANDIDATES`) plus their bold/italic
companions, wherever Debian ships those as separate files -- which is how
Pillow wants them: one font *file* per style, not one family with a
"bold" flag. Nothing here is bundled with picframe3 itself; each entry is
resolved against the handful of paths the matching Debian package actually
installs to, and an entry whose file isn't present on this machine simply
fails to resolve. The caller's job is to fall back to the frame's main font
when that happens, the same way an empty per-widget setting always has.
"""

from __future__ import annotations

import functools
import os

#: Display name -> candidate absolute paths, tried in order. Several paths
#: per entry because the same package has shipped its files under more than
#: one directory name across Debian releases (``liberation`` vs
#: ``liberation2``), and a frame should not have to care which one its own
#: release used.
_LIBRARY: dict[str, tuple[str, ...]] = {
    "DejaVu Sans": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
    "DejaVu Sans Bold": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    "DejaVu Sans Oblique": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ),
    "DejaVu Sans Condensed": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    ),
    "DejaVu Serif": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ),
    "DejaVu Serif Bold": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ),
    "DejaVu Sans Mono": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ),
    "Liberation Sans": (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ),
    "Liberation Sans Bold": (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ),
    "Liberation Sans Italic": (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Italic.ttf",
    ),
    "Liberation Serif": (
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    ),
    "Liberation Serif Bold": (
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf",
    ),
    "Liberation Mono": (
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    ),
    "Noto Sans": (
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ),
    "Noto Sans Bold": (
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ),
    "Noto Serif": (
        "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf",
    ),
    "FreeSans": (
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ),
}

#: Debian packages that put at least one entry above on disk.
#: fonts-dejavu-core is already a REQUIRED/OPTIONAL package in
#: packaging/install.sh for captions in general; fonts-liberation2 and
#: fonts-noto-core round the rest of the catalog out. This tuple is read by
#: install.sh (as plain text, grepped out -- that script runs before
#: picframe3 is even installed, so it cannot import this module) rather
#: than imported, so the two lists are kept in step by hand;
#: tests/test_fontlib.py checks every catalog entry names a path under one
#: of these package's own directories.
DEBIAN_PACKAGES: tuple[str, ...] = ("fonts-dejavu-core", "fonts-liberation2", "fonts-noto-core",
                                   "fonts-freefont-ttf")

#: "" first -- "use the frame's main font" -- then every catalog name in
#: alphabetical order, for a settings-page dropdown (see uischema.py's
#: CHOICES, used by every widget's new ``*_font`` setting).
CHOICES: tuple[str, ...] = ("",) + tuple(sorted(_LIBRARY))


@functools.lru_cache(maxsize=None)
def resolve(name: str | None) -> str | None:
    """The first installed file for *name*, or ``None`` if it is empty,
    unknown, or none of its candidate paths exist on this machine -- the
    caller's cue to fall back to the frame's main font, the same as an
    empty per-widget setting has always meant.

    Cached: this is called once per redraw for every widget with a font
    override, and a font package is not something that appears mid-session
    without a restart to pick it up anyway.
    """
    for path in _LIBRARY.get(name or "", ()):
        if os.path.exists(path):
            return path
    return None
