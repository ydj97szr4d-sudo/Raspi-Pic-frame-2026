"""Rotate and crop a photograph in place -- the two edits the library grid
offers, applied to the file on disk rather than kept as instructions for
something else to apply later.

Both operations bake the file's current EXIF orientation in first
(:func:`PIL.ImageOps.exif_transpose`), so the box a crop is given is always
in the photograph as it is actually displayed, not as the sensor happened to
have been held. The rest of the EXIF block -- when it was taken, the camera,
the GPS position -- rides along through Pillow's own transform methods and
is written back as-is; only the orientation tag is ever touched, because a
rotation this function just carried out as pixels is not a rotation the next
piece of software should be told to carry out again.

Nothing here decides whether to keep a backup first; that is
:mod:`picframe3.library.edits`' job, called by the HTTP layer before either
of these runs.
"""

from __future__ import annotations

import contextlib
import logging
import os

from PIL import Image, ImageOps

_log = logging.getLogger(__name__)

#: Re-encode quality for formats that have one (JPEG, WEBP). Matches the
#: quality this frame already re-encodes thumbnails and shrunk originals at
#: elsewhere (media.shrink, media.prepare), so an edited photo looks the same
#: as an unedited one shown beside it.
QUALITY = 92

#: Formats this editor will actually write back to. Anything else (HEIC
#: encoders are a system dependency Pillow does not carry by default, TIFF's
#: variety of internal layouts) is safer to refuse than to silently convert.
_WRITABLE = {"JPEG", "PNG", "WEBP", "BMP"}


class EditError(Exception):
    """A rotate/crop request that cannot be carried out -- always safe to
    show to whoever asked for the edit, never a reason to touch the file."""


def _load_baked(path: str) -> tuple[Image.Image, str]:
    img = Image.open(path)
    fmt = img.format or "JPEG"
    if fmt not in _WRITABLE:
        img.close()
        raise EditError(f"{fmt} photos cannot be edited here yet")
    img.load()
    oriented = ImageOps.exif_transpose(img)
    return (oriented if oriented is not None else img), fmt


def _save(img: Image.Image, path: str, fmt: str) -> None:
    kwargs: dict = {"exif": img.getexif()}
    if fmt == "JPEG":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        kwargs.update(quality=QUALITY, optimize=True)
    elif fmt == "WEBP":
        kwargs.update(quality=QUALITY)
    elif fmt == "PNG":
        kwargs.update(optimize=True)
    # Written to a temporary file in the same directory and swapped in with
    # os.replace -- both are on the same filesystem (same folder), so the
    # swap is atomic, and a crash or a full disk mid-save leaves the
    # original untouched instead of a half-written JPEG in its place.
    tmp = path + ".picframe3-edit-tmp"
    try:
        img.save(tmp, format=fmt, **kwargs)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.replace(tmp, path)


#: Clockwise quarter-turns (1, 2, 3) to the Transpose constant that produces
#: them.  Image.Transpose.ROTATE_90 rotates *counter*-clockwise -- confirmed
#: with a marker pixel, not assumed -- which is why one clockwise quarter-turn
#: is ROTATE_270, not ROTATE_90. A plain transpose (a pixel permutation) is
#: used rather than Image.rotate(), which resamples even for a right angle;
#: a 90-degree rotation has no reason to ever blur a single pixel.
_CLOCKWISE_TRANSPOSE = {
    1: Image.Transpose.ROTATE_270,
    2: Image.Transpose.ROTATE_180,
    3: Image.Transpose.ROTATE_90,
}


def rotate(path: str, degrees: int) -> None:
    """Rotate clockwise by *degrees*, which must be a multiple of 90.  A
    negative value (or one past 360) is fine -- only the remainder after
    dividing by 360 and reducing to a quarter-turn matters."""
    if degrees % 90 != 0:
        raise EditError("rotation must be a multiple of 90 degrees")
    turns = (degrees // 90) % 4
    img, fmt = _load_baked(path)
    try:
        if turns:
            img = img.transpose(_CLOCKWISE_TRANSPOSE[turns])
        _save(img, path, fmt)
    finally:
        img.close()


def crop(path: str, box: tuple[float, float, float, float]) -> None:
    """Crop to *box* -- (x0, y0, x1, y1), each a 0..1 fraction of the
    picture as it is actually displayed (after baking EXIF orientation)."""
    img, fmt = _load_baked(path)
    try:
        w, h = img.size
        x0, y0, x1, y1 = box
        left, right = sorted((round(x0 * w), round(x1 * w)))
        top, bottom = sorted((round(y0 * h), round(y1 * h)))
        left, right = max(0, left), min(w, right)
        top, bottom = max(0, top), min(h, bottom)
        if right - left < 8 or bottom - top < 8:
            raise EditError("that crop is too small")
        cropped = img.crop((left, top, right, bottom))
        _save(cropped, path, fmt)
    finally:
        img.close()
