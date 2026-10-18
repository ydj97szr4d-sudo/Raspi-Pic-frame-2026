#!/usr/bin/env python3
"""Motion-triggered display control for PicFrame3.

How it works
------------
- A PIR motion sensor on a GPIO pin is polled in a loop.
- When no motion has been seen for the configured idle timeout, this
  publishes "off" to PicFrame3's own MQTT display topic. PicFrame3 reacts by
  setting renderer.brightness = 0.0 (the picture goes solid black). This
  does NOT put the TV into HDMI standby by itself -- it relies on the TV's
  own "no signal / power-save" feature to notice the now-static black frame
  and power itself off after its own timeout (confirmed ~30s on this TV).
- As soon as motion is detected again, this publishes "on" to the same
  topic (so PicFrame3 restores brightness immediately) AND sends a CEC
  wake sequence (--active-source then --image-view-on) so the TV itself
  turns back on, since a TV that's fully powered off won't resume just
  because HDMI brightness changes.
- CEC's logical-address claim (--playback) does not survive a Pi reboot,
  so it's re-issued once at startup here.
- Also writes a plain count (0-N) to COUNTDOWN_FILE, showing roughly how
  much idle time is left before the display blanks. One count disappears
  every idle_timeout/dot_count seconds (not a flat 60s -- this way every
  step drains exactly in step with the real countdown, whatever the idle
  timeout is set to, instead of leaving a mismatched gap at the end).
  dht_mqtt.py reads this file and draws it as a dot grid directly next to
  the weather text, as part of its own overlay image (see dht_mqtt.py for
  why: it renders that whole line itself now, bypassing PicFrame3's native
  clock-text font, which turned out to have very sparse glyph coverage).

Settings (config.yaml, read live -- no restart needed)
--------------------------------------------------------
- motion.idle_timeout:  seconds of no motion before publishing "off"
                        (section "Motion" on the web Settings page)
- overlay.dot_count:    how many dots the countdown file counts down from;
                        kept in step with dht_mqtt.py's own grid, which
                        reads the same setting (section "Weather overlay")
Both are re-read from config.yaml every SETTINGS_RELOAD_INTERVAL seconds, so
a change made on the Settings page reaches this already-running service
without a restart.

Deploy notes
------------
- Requires gpiozero, paho-mqtt, PyYAML and cec-ctl (v4l-utils) on the Pi.
- PIR_PIN, CEC_DEVICE, and PHYS_ADDR below must match your wiring/TV.
- Run under systemd (service: motion-display) with Restart=always.
"""
import datetime
import math
import os
import subprocess
import time


def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

import paho.mqtt.client as mqtt
from gpiozero import MotionSensor

# ---- settings: edit these for your setup ----
PIR_PIN = __PIR_PIN__
POLL_INTERVAL = 1           # seconds between checks in the main loop
SETTINGS_RELOAD_INTERVAL = 15   # seconds between re-reads of config.yaml

BROKER = "localhost"
BROKER_PORT = 1883
DISPLAY_TOPIC = "picframe/picframe/display/set"

CEC_DEVICE = "__CEC_DEVICE__"
PHYS_ADDR = "__PHYS_ADDR__"       # this TV's HDMI physical address

COUNTDOWN_FILE = os.path.expanduser("~/display_countdown.txt")
CONFIG_PATH = os.path.expanduser("~/.config/picframe3/config.yaml")

#: Used whenever config.yaml can't be read, or doesn't set these yet.
DEFAULT_SETTINGS = {
    "idle_timeout": 900.0,   # 15 min
    "dot_count": 16,         # 4x4 grid; dht_mqtt.py does the actual drawing
}
# -----------------------------------------------

CEC_CLAIM_CMD = ["cec-ctl", "-d", CEC_DEVICE, "--playback"]
CEC_ACTIVE_SOURCE_CMD = ["cec-ctl", "-d", CEC_DEVICE, "--to", "0",
                         "--active-source", f"phys-addr={PHYS_ADDR}"]
CEC_WAKE_CMD = ["cec-ctl", "-d", CEC_DEVICE, "--to", "0", "--image-view-on"]


def load_settings():
    """motion.idle_timeout and overlay.dot_count from config.yaml, merged
    over DEFAULT_SETTINGS so a config missing either key still works."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        import yaml
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError as e:
        log(f"could not read {CONFIG_PATH} (using defaults): {e}")
        return settings
    motion = cfg.get("motion") or {}
    overlay = cfg.get("overlay") or {}
    if "idle_timeout" in motion:
        settings["idle_timeout"] = float(motion["idle_timeout"])
    if "dot_count" in overlay:
        settings["dot_count"] = int(overlay["dot_count"])
    return settings


def cec_claim_playback():
    """Claim a CEC logical address. Must be redone every boot -- it does
    not persist across a Pi reboot even though the adapter itself does."""
    log("Claiming CEC logical address (--playback)")
    try:
        subprocess.run(CEC_CLAIM_CMD, check=False, timeout=10)
    except Exception as e:
        log(f"CEC claim failed (continuing anyway): {e}")


def wake_tv():
    """The proven two-step wake sequence for this TV: announce ourselves
    as the active source, then ask it to power on and switch to us."""
    log("Sending CEC wake sequence (active-source, image-view-on)")
    try:
        subprocess.run(CEC_ACTIVE_SOURCE_CMD, check=False, timeout=10)
        subprocess.run(CEC_WAKE_CMD, check=False, timeout=10)
    except Exception as e:
        log(f"CEC wake failed: {e}")


def dots_remaining(seconds_left: float, idle_timeout: float, dot_count: int) -> int:
    """How many of the dots should still be filled for this much time left.
    Ceiling, so a dot only disappears once its whole interval has fully
    elapsed, and the grid starts completely full right after motion."""
    seconds_left = max(0.0, seconds_left)
    dot_interval = idle_timeout / dot_count
    return max(0, min(dot_count, math.ceil(seconds_left / dot_interval)))


def write_countdown(filled: int, dot_count: int):
    """Writes the plain filled-count (0-dot_count) -- dht_mqtt.py reads this
    and draws the actual dot grid itself, as part of its own overlay image."""
    tmp = COUNTDOWN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(max(0, min(dot_count, filled))))
    os.replace(tmp, COUNTDOWN_FILE)


class FrameState:
    """Tracks whether the display is currently considered on/off from this
    script's point of view, and publishes/wakes only on actual transitions
    -- never repeats a command every poll just because motion continues
    or absence continues. Also maintains the dot-grid countdown file,
    writing it only when the visible dot count actually changes, and
    re-reads idle_timeout/dot_count from config.yaml periodically so a
    change made on the Settings page takes effect without a restart."""

    def __init__(self, client, pir=None):
        self.client = client
        self.pir = pir
        self.last_motion_time = time.monotonic()
        self.last_written_dots = None
        self.settings = load_settings()
        self._next_settings_reload = time.monotonic() + SETTINGS_RELOAD_INTERVAL
        cec_claim_playback()
        # We have no way to ask the TV its actual current power state (it
        # may be off from before this service last restarted, e.g. during
        # a deploy/test), so never assume "on" here -- always force a
        # resync wake at startup instead. A redundant wake on a TV that's
        # already on is harmless; silently leaving a genuinely-off TV off
        # until the next real motion->idle->motion cycle is not.
        log("Startup: forcing a resync wake (can't query the TV's real state)")
        self.display_on = False
        self.set_display(True)
        wake_tv()
        self.display_on = True
        self._update_countdown(time.monotonic())

    def set_display(self, on: bool):
        payload = "on" if on else "off"
        log(f"Publishing {DISPLAY_TOPIC} = {payload!r}")
        self.client.publish(DISPLAY_TOPIC, payload, retain=False)

    def _maybe_reload_settings(self, now):
        if now < self._next_settings_reload:
            return
        self._next_settings_reload = now + SETTINGS_RELOAD_INTERVAL
        new_settings = load_settings()
        if new_settings != self.settings:
            log(f"settings changed: {self.settings} -> {new_settings}")
            self.settings = new_settings

    def _update_countdown(self, now):
        dot_count = self.settings["dot_count"]
        if self.display_on:
            remaining = self.settings["idle_timeout"] - (now - self.last_motion_time)
            filled = dots_remaining(remaining, self.settings["idle_timeout"], dot_count)
        else:
            filled = 0   # screen is blank anyway; nothing to show
        if filled != self.last_written_dots:
            write_countdown(filled, dot_count)
            self.last_written_dots = filled

    def tick(self):
        self._maybe_reload_settings(time.monotonic())
        motion = self.pir.motion_detected if self.pir is not None else False
        now = time.monotonic()
        idle_timeout = self.settings["idle_timeout"]

        if motion:
            self.last_motion_time = now
            if not self.display_on:
                log("Motion detected after idle -- waking display")
                self.display_on = True
                self.set_display(True)
                wake_tv()
        else:
            if self.display_on and (now - self.last_motion_time) >= idle_timeout:
                log(f"No motion for {idle_timeout}s -- blanking display")
                self.display_on = False
                self.set_display(False)

        self._update_countdown(now)


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(BROKER, BROKER_PORT, keepalive=60)
    client.loop_start()

    pir = MotionSensor(PIR_PIN)
    state = FrameState(client, pir)

    try:
        while True:
            state.tick()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
