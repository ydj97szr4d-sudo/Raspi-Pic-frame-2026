# The interface's icon

The mark is an eagle in flight, white/grey, on the app's own dark panel
grey, `#201e1d` (`--panel` in `style.css`) — not the accent red; the red was
a one-night mistake (see history on this file) that got caught and reverted.

## The master

There is no vector source for the eagle itself -- it has only ever existed
as raster art (the artist's original PNG is lost; what the repo has is the
composited icons, recovered pixel-for-pixel from git history when the
background briefly went red). `favicon.svg` and `icon-maskable.svg` are
each an `<svg>` wrapping a base64 PNG, not real vector drawings, and
`favicon-512x512.png` (plain) and `icon-maskable-512x512.png` (inset to the
safe zone) are the closest things to masters: every other raster is a
resize of one of those two, recomposited onto grey.

If the eagle art itself ever needs to change, it has to be redrawn or
resupplied as a new transparent PNG -- there is no geometry here to tweak,
unlike a true vector mark.

Tokens: ground `#201e1d`, page ink `#201e1d`, ground (light theme)
`#f3f2f2`.

## The files

All in `src/picframe3/web/`, served by name because `web/` is mounted at the
server root:

| File | For |
|---|---|
| `favicon.svg` | current browsers, and the master |
| `favicon.ico` | 16/32/48 px, for browsers that still ask for a `.ico` |
| `apple-touch-icon.png` | 180 px, iOS home screen (iOS rounds the corners itself) |
| `favicon-192x192.png`, `favicon-512x512.png` | Android home screen, referenced by the manifest |
| `icon-maskable-512x512.png`, `icon-maskable.svg` | Android, cropped to the launcher's shape |
| `manifest.webmanifest` | names the app, points at the icons, `display: standalone` |

`index.html` carries four `<link>` lines for these, all relative: the
interface is opened by IP, by hostname and by `.local` name alike.
`tools/check_package_data.py` lists every one of them, so a wheel that
forgets an icon fails the check instead of shipping a frame with a blank tab.

The two `theme-color` metas in `index.html` are *not* the icon's grey on
purpose: they follow the page background per theme, which is what the
browser's own chrome should match.

## Regenerating the rasters

Start from the two true-alpha masters -- a transparent-background PNG of
the eagle (plain) and one inset to the maskable safe zone -- and composite
each onto solid grey at whatever size is needed. With Pillow:

```python
from PIL import Image

GREY = (32, 30, 29)  # #201e1d

def on_grey(rgba):
    bg = Image.new("RGBA", rgba.size, GREY + (255,))
    return Image.alpha_composite(bg, rgba).convert("RGB")

plain = Image.open("plain_master.png").convert("RGBA")      # transparent eagle
maskable = Image.open("maskable_master.png").convert("RGBA")  # inset to the safe zone

out = "src/picframe3/web/"
on_grey(plain).save(out + "favicon-512x512.png", optimize=True)
on_grey(plain.resize((192, 192), Image.LANCZOS)).save(out + "favicon-192x192.png", optimize=True)
on_grey(plain.resize((180, 180), Image.LANCZOS)).save(out + "apple-touch-icon.png", optimize=True)
on_grey(maskable).save(out + "icon-maskable-512x512.png", optimize=True)
on_grey(plain.resize((48, 48), Image.LANCZOS)).save(
    out + "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
```

Then wrap `favicon-512x512.png` and `icon-maskable-512x512.png` each in a
one-line `<svg viewBox="0 0 512 512"><image .../></svg>` (base64-encoded)
to produce `favicon.svg` and `icon-maskable.svg`.

If starting instead from an already-composited-on-some-other-color PNG (as
happened recovering these from git history after the red mistake), the
alpha can be recovered algebraically rather than guessed, *provided* the
foreground art is pure grayscale and the background is a known solid
color: for background `(br, bg, bb)` and an observed pixel `(r, g, b)`,
`1 - alpha = (r - g) / (br - bg)` (pick whichever channel pair differs
most for numerical stability), then the original grayscale value falls out
of the standard over-compositing equation. This reproduces the exact
original pixels and anti-aliasing rather than resampling from a
differently-sized source.
