"""Finds where a face is in a photograph, so cropping can keep it on screen.

Detection only -- this answers *where in the picture a head-shaped region is*,
never *whose*.  It keeps nothing beyond the one bounding box written back to
the index (``library.db.Library.set_face_box``), and the image never leaves
the frame or touches the network: no faces are ever uploaded anywhere, only
(once, at install time) a detector *model* is downloaded, same as a font or a
font-fallback would be.

Two detectors, tried in this order:

* **YuNet** (``cv2.FaceDetectorYN``), OpenCV's own small deep-learning face
  detector, if its model file has been fetched (see
  ``packaging/fetch_face_model.sh``, run by install.sh/update.sh). It finds a
  face turned to the side, tilted, or small in a group photo -- everything a
  Haar cascade is known to miss -- for a model under 100 KB and a cost per
  photo still measured in tens of milliseconds on a Pi.
* **Haar cascade** (``haarcascade_frontalface_default.xml``), OpenCV's bundled
  twenty-year-old detector, as a fallback needing no download at all: a frame
  offline at install time, or one where the fetch failed, still gets *some*
  face-aware cropping rather than none.

Both need ``opencv-python-headless`` (the ``picframe3[faces]`` extra). Without
it, :func:`available` is False and :func:`detect_face_box` always returns
``None`` -- the frame falls back to the fixed centering it always used,
rather than failing to start.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)

#: Where install.sh/update.sh (via fetch_face_model.sh) puts the YuNet model,
#: if it managed to fetch one. Checked for at runtime, not assumed: a frame
#: that was offline at install time, or on a release that predates this,
#: simply never finds this file and uses the Haar cascade instead.
MODEL_PATH = os.path.expanduser(
    "~/.local/share/picframe3/models/face_detection_yunet_2023mar_int8.onnx")

_detector = None       # cv2.FaceDetectorYN, once loaded
_detector_size = None  # (w, h) the detector was last sized for
_cascade = None        # cv2.CascadeClassifier, the Haar fallback
_backend = None        # "yunet" | "haar" | None
_resolved = False

#: A face search is run on a copy no larger than this on its longest side.
#: Shrinking first is what keeps a 48-megapixel phone photo and a scanned
#: postcard the same cost to search -- both detectors find just as many faces
#: on a 640px copy as on the original, and testing a panorama at full size
#: bought nothing but a much slower backfill.
_DETECT_SIZE = 640

#: Both detectors' boxes run roughly eyebrows-to-chin (YuNet's a little
#: tighter than Haar's) and stop well short of the hairline on every side.
#: "The whole face, with hair" needs more headroom above than below, and a
#: little on each side -- these came from looking at actual crops, not from
#: anything principled in either detector.
_PAD_TOP = 0.55
_PAD_SIDE = 0.25
_PAD_BOTTOM = 0.15

#: If the faces found between them already span this much of the photo on
#: either axis, no crop window could keep them all in better than the centre
#: already does -- so the anchor is dropped rather than used for nothing.
_MAX_SPAN = 0.92

#: YuNet's own confidence/overlap settings -- the opencv_zoo demo's defaults,
#: which is also what they were tuned against.
_YUNET_SCORE_THRESHOLD = 0.6
_YUNET_NMS_THRESHOLD = 0.3
_YUNET_TOP_K = 5000


def available() -> bool:
    """Whether a usable detector (either one) was found.  Safe to call often."""
    return _resolve() is not None


def backend() -> str | None:
    """"yunet", "haar", or ``None`` -- which detector is actually in use,
    for the ``picframe3 doctor`` report and nothing else."""
    _resolve()
    return _backend


def _resolve():
    global _detector, _cascade, _backend, _resolved
    if _resolved:
        return _detector or _cascade
    _resolved = True
    try:
        import cv2  # type: ignore
    except ImportError:
        _log.info("opencv is not installed; face-aware cropping is off "
                   "(pip install 'picframe3[faces]' to turn it on)")
        return None

    if os.path.exists(MODEL_PATH):
        try:
            with _quiet_cv2_logging(cv2):
                detector = cv2.FaceDetectorYN_create(
                    MODEL_PATH, "", (320, 320),
                    score_threshold=_YUNET_SCORE_THRESHOLD,
                    nms_threshold=_YUNET_NMS_THRESHOLD,
                    top_k=_YUNET_TOP_K,
                )
            _detector = detector
            _backend = "yunet"
            return _detector
        except Exception as exc:  # pragma: no cover - a corrupt/partial download
            _log.warning("could not load the YuNet face model at %s (%s); "
                          "falling back to OpenCV's bundled Haar cascade", MODEL_PATH, exc)

    try:
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(path)
        if cascade.empty():
            raise RuntimeError(f"empty cascade from {path}")
    except Exception as exc:  # pragma: no cover - a broken/partial opencv install
        _log.warning("could not load OpenCV's bundled face cascade (%s); "
                      "face-aware cropping is off", exc)
        return None
    _cascade = cascade
    _backend = "haar"
    return _cascade


def _quiet_cv2_logging(cv2):
    """``cv2.FaceDetectorYN_create`` logs a harmless "Targets are not
    supported by the new graph engine" warning on some OpenCV builds, straight
    to stderr rather than through Python logging -- which would otherwise
    land in the systemd journal on every single frame boot. A no-op context
    manager on any OpenCV build old enough not to have the logging knob."""
    import contextlib

    @contextlib.contextmanager
    def _quiet():
        try:
            level = cv2.utils.logging.getLogLevel()
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        except Exception:
            yield
            return
        try:
            yield
        finally:
            cv2.utils.logging.setLogLevel(level)

    return _quiet()


def detect_face_box(image) -> tuple[float, float, float, float] | None:
    """The region a crop must not cut into, to keep every face (and hair) in.

    ``image`` is a PIL image, already EXIF-oriented.  Returns
    ``(x0, y0, x1, y1)``, each a 0..1 fraction of the image, covering every
    face found (padded generously for hair) -- or ``None`` when no face was
    found, when neither detector is available, or when the faces found
    already span too much of the photo for a box to mean anything.

    Never raises: a photograph OpenCV cannot read (an odd colour mode, a
    truly enormous scan) just does not get a face box, same as a photo with
    nobody in it.
    """
    detector = _resolve()
    if detector is None:
        return None
    try:
        import numpy as np

        w, h = image.size
        if w <= 0 or h <= 0:
            return None
        scale = min(1.0, _DETECT_SIZE / max(w, h))
        if _backend == "yunet":
            small = image.convert("RGB")
            if scale < 1.0:
                small = small.resize(
                    (max(1, round(w * scale)), max(1, round(h * scale))))
            # cv2 wants BGR, same channel order cv2.imread would have given it;
            # ascontiguousarray because detect() hands this straight to native
            # code, which does not tolerate the view-not-copy a plain [::-1]
            # slice would otherwise be.
            arr = np.ascontiguousarray(np.array(small)[:, :, ::-1])
            sh, sw = arr.shape[:2]
            if sw <= 0 or sh <= 0:
                return None
            global _detector_size
            if _detector_size != (sw, sh):
                detector.setInputSize((sw, sh))
                _detector_size = (sw, sh)
            _, rows = detector.detect(arr)
            boxes = [] if rows is None else [
                (row[0], row[1], row[0] + row[2], row[1] + row[3]) for row in rows
            ]
        else:
            small = image.convert("L")
            if scale < 1.0:
                small = small.resize(
                    (max(1, round(w * scale)), max(1, round(h * scale))))
            gray = np.array(small)
            sh, sw = gray.shape[:2]
            if sw <= 0 or sh <= 0:
                return None
            faces = detector.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5,
                minSize=(max(20, sw // 20), max(20, sh // 20)),
            )
            boxes = [(fx, fy, fx + fw, fy + fh) for fx, fy, fw, fh in faces]

        if not boxes:
            return None

        x0, y0, x1, y1 = 1.0, 1.0, 0.0, 0.0
        for bx0, by0, bx1, by1 in boxes:
            fw, fh = bx1 - bx0, by1 - by0
            top = max(0.0, (by0 - fh * _PAD_TOP) / sh)
            bottom = min(1.0, (by1 + fh * _PAD_BOTTOM) / sh)
            left = max(0.0, (bx0 - fw * _PAD_SIDE) / sw)
            right = min(1.0, (bx1 + fw * _PAD_SIDE) / sw)
            x0, y0 = min(x0, left), min(y0, top)
            x1, y1 = max(x1, right), max(y1, bottom)

        if (x1 - x0) > _MAX_SPAN or (y1 - y0) > _MAX_SPAN:
            return None
        return (x0, y0, x1, y1)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("face detection failed: %s", exc)
        return None
