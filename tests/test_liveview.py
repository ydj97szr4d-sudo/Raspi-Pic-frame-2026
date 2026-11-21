"""The web page's Live view: a switch and a rate next to Screenshot, built on /api/screenshot."""
import os

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "picframe3", "web")


def _read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def test_the_page_offers_a_live_view_and_a_rate():
    html = _read("index.html")
    assert 'id="livebtn"' in html and 'id="live-rate"' in html
    assert 'id="shot"' in html          # the one-off Screenshot stays


def test_live_view_uses_the_existing_capture_and_stops_when_hidden():
    js = _read("app.js")
    assert js.count('"/api/screenshot"') == 1      # one capture path, shared by both buttons
    assert "visibilitychange" in js
    assert "Math.max(2," in js                     # never faster than every two seconds
