#!/usr/bin/env python3
"""Render one thumbnail per clock face into src/picframe3/web/faces/<id>.png.

The settings page's face picker shows these. They come from the very renderer the frame
uses (packaging/companions/faces.py), at a fixed, pleasant time (10:08:36) and a fixed
date, so the files only change when a face does -- re-run this after touching faces.py:

    python3 tools/gen_face_thumbs.py
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packaging", "companions"))
import faces  # noqa: E402

OUT = os.path.join(ROOT, "src", "picframe3", "web", "faces")
W, H = 480, 320          # the design size the faces are laid out for; stored at 2/3 for the page


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    now = datetime.datetime(2026, 11, 21, 10, 8, 36)
    from PIL import Image

    for fid in faces.FACES:
        img = faces.render_face(fid, W, H, now)
        img = img.resize((W * 2 // 3, H * 2 // 3), Image.LANCZOS)
        img.save(os.path.join(OUT, fid + ".png"), optimize=True)
        print("wrote", fid)


if __name__ == "__main__":
    main()
