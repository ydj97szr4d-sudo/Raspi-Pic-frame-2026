"""The clock-face tile: the face menu, the renderer, the thumbnails and the settings stay in step."""
import datetime
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packaging", "companions"))

pytest.importorskip("numpy")
faces = pytest.importorskip("faces")

from picframe3.config import Config  # noqa: E402
from picframe3.gfx.facelist import FACE_IDS, FACE_LABELS  # noqa: E402

NOW = datetime.datetime(2026, 11, 21, 10, 8, 36)


def test_the_menu_matches_the_renderer():
    assert list(FACE_LABELS) == list(faces.FACES)
    assert {k: v["label"] for k, v in faces.FACES.items()} == FACE_LABELS


@pytest.mark.parametrize("face_id", FACE_IDS)
def test_every_face_draws_at_any_size(face_id):
    for w, h in ((480, 320), (240, 240), (600, 600)):
        img = faces.render_face(face_id, w, h, NOW)
        assert img.size == (w, h)
        assert img.getbbox()


def test_an_unknown_face_falls_back_instead_of_crashing():
    assert faces.render_face("no-such-face", 480, 320, NOW).size == (480, 320)


def test_options_override_the_preset():
    plain = faces.render_face("paper", 480, 320, NOW)
    dark = faces.render_face("paper", 480, 320, NOW, {"bg": [0, 0, 0], "bg2": [0, 0, 0], "fg": [255, 255, 255]})
    assert plain.getpixel((470, 10)) != dark.getpixel((470, 10))
    assert dark.getpixel((470, 10)) == (0, 0, 0)


def test_brightness_dims():
    full = faces.render_face("paper", 480, 320, NOW)
    dim = faces.render_face("paper", 480, 320, NOW, {"brightness": 50})
    assert sum(dim.getpixel((470, 10))) < sum(full.getpixel((470, 10)))


def test_the_banner_shows_the_text_it_is_given():
    a = faces.render_face("banner", 480, 320, NOW, {"banner_text": "Hello"})
    b = faces.render_face("banner", 480, 320, NOW, {"banner_text": "Goodbye, friends"})
    assert a.tobytes() != b.tobytes()


def test_every_face_has_a_thumbnail_for_the_settings_page():
    thumbs = os.path.join(ROOT, "src", "picframe3", "web", "faces")
    missing = [f for f in FACE_IDS if not os.path.exists(os.path.join(thumbs, f + ".png"))]
    assert not missing, f"run tools/gen_face_thumbs.py: {missing}"


def test_the_tile_is_off_by_default_and_owns_its_settings():
    cfg = Config()
    assert cfg.facetile.enabled is False
    assert cfg.facetile.face in FACE_IDS
    # the companion's built-in defaults must be the config's defaults
    import dataclasses
    src = open(os.path.join(ROOT, "packaging", "companions", "dht_mqtt.py"), encoding="utf-8").read()
    for f in dataclasses.fields(cfg.facetile):
        assert f'"{f.name}":' in src, f"dht_mqtt.py is missing the default for facetile.{f.name}"


def test_group_style_reaches_the_new_tiles():
    js = open(os.path.join(ROOT, "src", "picframe3", "web", "app.js"), encoding="utf-8").read()
    assert 'facetile: { label: "Clock face tile"' in js
    assert 'nowplaying: { label: "Now playing tile"' in js
