"""A cheap fingerprint for "photos that look like this one".

Not face recognition, not an embedding model, nothing that needs a model
download or leaves the frame: a difference hash (dHash) computed with the
Pillow decode the frame already carries. Shrink a copy to 9x8 grey pixels,
write a 1 if a pixel is darker than its neighbour to the right, read the 64
bits off as one integer. Two photographs that are nearly the same picture --
a near-duplicate burst shot, the same scene cropped differently, a filtered
copy of the same file -- land a handful of bits apart; two unrelated
photographs land, on average, around half the bits apart. Counting how many
bits two hashes differ on (the Hamming distance) is therefore a workable
stand-in for "how alike do these look", without ever sending a single pixel
off the device.

This is deliberately not a face match and not a content classifier: it
cannot tell a beach from another beach's crowd, only that two files produce
almost the same small grid of light and dark. That is honest about what it
is finding, and it is also the whole reason it costs nothing per photograph
beyond a decode this frame already had to do somewhere (see
``library.scanner.Scanner.backfill_phashes``, run the same way face
detection and geocoding are -- a little at a time, in the background, never
holding up a scan).
"""

from __future__ import annotations

import logging

from PIL import Image

from .prepare import open_oriented

_log = logging.getLogger(__name__)

#: dHash's own geometry: one row wider than it is tall, because each row's
#: bit comes from comparing a pixel to the one beside it.
_HASH_W, _HASH_H = 9, 8

#: Hashes are kept as plain 64-bit integers in SQLite's signed INTEGER
#: column, so a bit pattern with the top bit set is stored as the negative
#: number two's-complement would read it as.  Both ends of a comparison are
#: masked back to unsigned before the XOR (see library.db's hamming64), so
#: this only matters here as the one place that conversion happens.
_UNSIGNED = (1 << 64) - 1
_SIGNED_CEILING = 1 << 63


def _to_signed64(value: int) -> int:
    value &= _UNSIGNED
    return value - (1 << 64) if value >= _SIGNED_CEILING else value


def compute(path: str) -> int | None:
    """The dHash of the picture at ``path``, or ``None`` if it can't be read.

    A video, a file that has gone missing between the scan and this call, or
    a format Pillow cannot open all land here rather than raising -- the
    caller (the backfill loop) records "checked, nothing found" either way,
    which is what stops a corrupt file from being retried every pass forever.
    """
    try:
        img = open_oriented(path, target=(_HASH_W, _HASH_H))
        if img is None:
            return None
        try:
            grey = img.convert("L").resize((_HASH_W, _HASH_H), Image.LANCZOS)
            pixels = grey.load()
            bits = 0
            for y in range(_HASH_H):
                for x in range(_HASH_W - 1):
                    bits <<= 1
                    if pixels[x, y] > pixels[x + 1, y]:
                        bits |= 1
            return _to_signed64(bits)
        finally:
            img.close()
    except Exception as exc:  # pragma: no cover - a bad file on disk
        _log.debug("phash failed for %s: %s", path, exc)
        return None


def hamming(a: int | None, b: int | None) -> int:
    """Bits that differ between two hashes, both read back as unsigned 64-bit.

    Exposed mainly so tests can check the maths without a database; the
    query path uses the SQL function registered under the same name in
    ``library.db`` (``connect()``), for the same reason -- a UDF written once
    and called from SQL is the only way to rank a few thousand rows by
    distance without pulling every hash into Python first.
    """
    if a is None or b is None:
        return 64
    return bin((a & _UNSIGNED) ^ (b & _UNSIGNED)).count("1")
