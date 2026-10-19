"""Finds where a face is in a photograph, so cropping can keep it on screen.

Detection only -- this answers *where in the picture a head-shaped region is*,
never *whose*.  It keeps nothing beyond the one anchor point written back to
the index (``library.db.Library.set_face_anchor``), and the image never
leaves the frame or touches the network: this is OpenCV's bundled Haar
cascade (``haarcascade_frontalface_default.xml``), the same detector OpenCV
has shipped for twenty years, with no model download and no per-person data
of any kind, ever computed.

Cost is paid once per photograph, at scan time (see
``library.scanner.Scanner.backfill_face_anchors``), not on every slide -- a
cascade pass over a full-resolution holiday photo is not something to repeat
every few minutes forever, so the result is cached in the index and read back
from there when the picture is actually cropped for the screen (see
``media.prepare._fit_cover``).

Needs ``opencv-python-headless`` (the ``picframe3[faces]`` extra). Without it,
:func:`available` is False and :func:`detect_anchor` always returns ``None``
-- the frame falls back to the fixed centering it always used, rather than
failing to start.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

_cascade = None
_resolved = False

#: A face search is run on a copy no larger than this on its longest side.
#: Shrinking first is what keeps a 48-megapixel phone photo and a scanned
#: postcard the same cost to search -- the cascade finds just as many faces
#: on a 640px copy as on the original, and testing a panorama at full size
#: bought nothing but a much slower backfill.
_DETECT_SIZE = 640

#: Haar's frontal-face box runs roughly eyebrows-to-chin and stops well short
#: of the hairline on every side. "The whole face, with hair" needs more
#: headroom above than below, and a little on each side -- these came from
#: looking at actual crops, not from anything principled in the detector.
_PAD_TOP = 0.55
_PAD_SIDE = 0.25
_PAD_BOTTOM = 0.15

#: If the faces found between them already span this much of the photo on
#: either axis, no crop window could keep them all in better than the centre
#: already does -- so the anchor is dropped rather than used for nothing.
_MAX_SPAN = 0.92


def available() -> bool:
    """Whether a usable face cascade was found.  Safe to call often."""
    return _resolve() is not None


def _resolve():
    global _cascade, _resolved
    if _resolved:
        return _cascade
    _resolved = True
    try:
        import cv2  # type: ignore
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(path)
        if cascade.empty():
            raise RuntimeError(f"empty cascade from {path}")
    except ImportError:
        _log.info("opencv is not installed; face-aware cropping is off "
                   "(pip install 'picframe3[faces]' to turn it on)")
        return None
    except Exception as exc:  # pragma: no cover - a broken/partial opencv install
        _log.warning("could not load OpenCV's bundled face cascade (%s); "
                      "face-aware cropping is off", exc)
        return None
    _cascade = cascade
    return _cascade


def detect_anchor(image) -> tuple[float, float] | None:
    """Where a crop should be centred to keep every face (and its hair) in.

    ``image`` is a PIL image, already EXIF-oriented.  Returns an
    ``(x, y)`` pair, each a 0..1 fraction of the image -- the same shape
    ``PIL.ImageOps.fit``'s ``centering`` argument takes -- or ``None`` when no
    face was found, when OpenCV is not installed, or when the faces found
    already span too much of the photo for an anchor to change anything.

    Never raises: a photograph OpenCV cannot read (an odd colour mode, a
    truly enormous scan) just does not get a face anchor, same as a photo
    with nobody in it.
    """
    cascade = _resolve()
    if cascade is None:
        return None
    try:
        import numpy as np

        w, h = image.size
        if w <= 0 or h <= 0:
            return None
        scale = min(1.0, _DETECT_SIZE / max(w, h))
        small = image.convert("L")
        if scale < 1.0:
            small = small.resize(
                (max(1, round(w * scale)), max(1, round(h * scale)))
            )
        gray = np.array(small)
        sh, sw = gray.shape[:2]
        if sw <= 0 or sh <= 0:
            return None
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5,
            minSize=(max(20, sw // 20), max(20, sh // 20)),
        )
        if len(faces) == 0:
            return None

        x0, y0, x1, y1 = 1.0, 1.0, 0.0, 0.0
        for fx, fy, fw, fh in faces:
            top = max(0.0, (fy - fh * _PAD_TOP) / sh)
            bottom = min(1.0, (fy + fh * (1.0 + _PAD_BOTTOM)) / sh)
            left = max(0.0, (fx - fw * _PAD_SIDE) / sw)
            right = min(1.0, (fx + fw * (1.0 + _PAD_SIDE)) / sw)
            x0, y0 = min(x0, left), min(y0, top)
            x1, y1 = max(x1, right), max(y1, bottom)

        if (x1 - x0) > _MAX_SPAN or (y1 - y0) > _MAX_SPAN:
            return None
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("face detection failed: %s", exc)
        return None
