#!/usr/bin/env bash
# Installs the three companion services that make this "your own" picture
# frame rather than stock PicFrame3:
#
#   dht-mqtt       DHT11 (+ optional outdoor Pico W, PM2.5 sensor) -> MQTT ->
#                  a weather/AQI line drawn under the clock, with its own
#                  idle-countdown dot grid (see src/picframe3/uischema.py,
#                  section "overlay")
#   motion-display HC-SR501 PIR -> blanks the screen after no motion, wakes
#                  it (and the TV, over CEC) when motion returns (section
#                  "motion")
#   video-proxy    keeps every video in the library within what a Pi 4 can
#                  actually play smoothly, in place (section "video_proxy")
#
# Run this AFTER packaging/install.sh has installed picframe3 itself, as
# your normal user (not with sudo) from inside this source tree:
#
#   bash packaging/install_companions.sh
#
# Safe to re-run: it keeps your previous answers (in
# ~/.config/picframe3/companions.env) and just rewrites the three scripts
# and systemd units from this source tree, then restarts the services. Pass
# COMPANIONS_SETUP=1 to be asked the questions again.
#
# All per-device answers can also be set as environment variables up front,
# e.g. for an unattended install:
#   OWM_KEY=xxxx LAT=22.72 LON=88.48 PIR_PIN=17 CEC_DEVICE=/dev/cec0 \
#     PHYS_ADDR=1.0.0.0 SCREEN_W=3840 SCREEN_H=2160 \
#     bash packaging/install_companions.sh

set -euo pipefail

if [ "$EUID" -eq 0 ]; then
  echo "Run this as your normal user, not root/sudo. It uses sudo where needed."
  exit 1
fi

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPANIONS_DIR="$HERE/companions"
[ -f "$COMPANIONS_DIR/dht_mqtt.py" ] || die "run this from inside the source tree (packaging/companions/*.py not found)"

VENV="$HOME/myenv"
ANSWERS_FILE="$HOME/.config/picframe3/companions.env"

bold "picframe3 companion services installer"
echo "   dht-mqtt, motion-display, video-proxy"
echo

# ----------------------------------------------------------- 0. the answers
step "0/6  Settings"
mkdir -p "$(dirname "$ANSWERS_FILE")"
UPDATE=0
if [ -f "$ANSWERS_FILE" ]; then
  UPDATE=1
  # shellcheck disable=SC1090
  source "$ANSWERS_FILE"
fi

ask() {
  # ask VAR "prompt" default
  local var="$1" prompt="$2" default="$3" current
  current="$(eval "echo \${$var:-}")"
  if [ -n "$current" ]; then
    return   # already set by an earlier run, or by the environment
  fi
  if [ -n "${COMPANIONS_YES:-}" ]; then
    eval "$var=\"$default\""
    return
  fi
  if [ -r /dev/tty ]; then
    read -r -p "   $prompt [$default]: " answer < /dev/tty || answer=""
  else
    answer=""
  fi
  eval "$var=\"${answer:-$default}\""
}

if [ "$UPDATE" -eq 1 ] && [ -z "${COMPANIONS_SETUP:-}" ]; then
  ok "using answers from a previous run ($ANSWERS_FILE)"
  echo "   set COMPANIONS_SETUP=1 to be asked again, or edit that file directly."
else
  echo "   One-time questions (defaults shown in brackets). Press Enter to accept."
  if [ -z "${OWM_KEY:-}" ]; then
    if [ -r /dev/tty ]; then
      read -r -p "   OpenWeather API key (openweathermap.org -> API keys), required: " OWM_KEY < /dev/tty || OWM_KEY=""
    fi
    [ -n "$OWM_KEY" ] || die "Set your OpenWeather key first:  OWM_KEY=xxxx bash packaging/install_companions.sh"
  fi
  ask LAT "Latitude for the OpenWeather fallback" "22.72"
  ask LON "Longitude for the OpenWeather fallback" "88.48"
  ask PIR_PIN "PIR sensor GPIO (BCM numbering)" "17"
  ask CEC_DEVICE "CEC device node" "/dev/cec0"
  ask PHYS_ADDR "TV's HDMI CEC physical address (cec-ctl -d \$CEC_DEVICE --to 0 --give-physical-addr to check)" "1.0.0.0"
  ask SCREEN_W "Screen width in pixels (fbset -s)" "3840"
  ask SCREEN_H "Screen height in pixels (fbset -s)" "2160"
fi

cat > "$ANSWERS_FILE" <<EOF
OWM_KEY=$OWM_KEY
LAT=$LAT
LON=$LON
PIR_PIN=$PIR_PIN
CEC_DEVICE=$CEC_DEVICE
PHYS_ADDR=$PHYS_ADDR
SCREEN_W=$SCREEN_W
SCREEN_H=$SCREEN_H
EOF
ok "answers saved to $ANSWERS_FILE"

# ------------------------------------------------------- 1. system packages
step "1/6  System packages"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  swig python3-dev python3-venv build-essential liblgpio-dev \
  mosquitto mosquitto-clients v4l-utils ffmpeg
ok "system packages present"

# -------------------------------------------------------------- 2. mosquitto
step "2/6  Mosquitto broker"
# Mosquitto 2.x only accepts local connections by default; opened here for an
# optional outdoor Pico W publishing over the LAN. Some Raspberry Pi OS images
# already ship a "listener 1883 / allow_anonymous true" pair directly inside
# mosquitto.conf -- adding a second one via conf.d/ would make Mosquitto
# refuse to start (two listeners on the same port), so only add our own file
# if the main config doesn't already declare a listener.
if sudo grep -q "^listener" /etc/mosquitto/mosquitto.conf 2>/dev/null; then
  ok "mosquitto.conf already has a listener; leaving it as-is"
  sudo rm -f /etc/mosquitto/conf.d/lan.conf
else
  sudo tee /etc/mosquitto/conf.d/lan.conf >/dev/null <<'MQEOF'
listener 1883
allow_anonymous true
MQEOF
  ok "LAN listener configured"
fi
sudo systemctl enable mosquitto >/dev/null
sudo systemctl restart mosquitto
ok "mosquitto running"

# ------------------------------------------------------------------ 3. venv
step "3/6  Python virtual environment ($VENV)"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --progress-bar off --upgrade pip
"$VENV/bin/pip" install --progress-bar off \
  lgpio adafruit-circuitpython-dht paho-mqtt gpiozero pillow pyyaml
sudo usermod -aG gpio "$USER" || true
ok "companion venv ready"

# -------------------------------------------------- 4. the three scripts
step "4/6  Writing dht_mqtt.py, motion_display.py, video_proxy.py"
sed -e "s|__OWM_KEY__|$OWM_KEY|" -e "s|__LAT__|$LAT|" -e "s|__LON__|$LON|" \
    -e "s|__SCREEN_W__|$SCREEN_W|" -e "s|__SCREEN_H__|$SCREEN_H|" \
    "$COMPANIONS_DIR/dht_mqtt.py" > "$HOME/dht_mqtt.py"
sed -e "s|__PIR_PIN__|$PIR_PIN|" -e "s|__CEC_DEVICE__|$CEC_DEVICE|" \
    -e "s|__PHYS_ADDR__|$PHYS_ADDR|" \
    "$COMPANIONS_DIR/motion_display.py" > "$HOME/motion_display.py"
cp "$COMPANIONS_DIR/video_proxy.py" "$HOME/video_proxy.py"
chmod 755 "$HOME/dht_mqtt.py" "$HOME/motion_display.py" "$HOME/video_proxy.py"
ok "scripts written to $HOME"

# -------------------------------------------------- 5. systemd services
step "5/6  systemd services"
sudo tee /etc/systemd/system/dht-mqtt.service >/dev/null <<EOF
[Unit]
Description=DHT11 to MQTT publisher
After=network-online.target mosquitto.service
Wants=network-online.target mosquitto.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME
ExecStart=$VENV/bin/python -u $HOME/dht_mqtt.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/motion-display.service >/dev/null <<EOF
[Unit]
Description=PIR motion display control
After=network-online.target mosquitto.service picframe3@$USER.service
Wants=mosquitto.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME
ExecStart=$VENV/bin/python -u $HOME/motion_display.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/video-proxy.service >/dev/null <<EOF
[Unit]
Description=video-proxy: keeps PicFrame3's library videos within what a Pi 4 can play smoothly
After=network.target

[Service]
Type=simple
User=$USER
Group=$USER
ExecStart=$VENV/bin/python -u $HOME/video_proxy.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable dht-mqtt motion-display video-proxy >/dev/null
# "enable --now" only starts a stopped service; on a re-run the old process
# would otherwise keep running the previous version of the script forever.
# Explicitly restart all three so the files just written above take effect.
sudo systemctl restart dht-mqtt motion-display video-proxy
ok "dht-mqtt, motion-display, video-proxy enabled and (re)started"

# ----------------------------------------------------------------- 6. check
step "6/6  Checking"
sleep 2
for svc in dht-mqtt motion-display video-proxy; do
  if systemctl is-active --quiet "$svc"; then
    ok "$svc running"
  else
    warn "$svc did not come up -- journalctl -u $svc -n 40"
  fi
done

cat <<NOTES

============================================================
Done.

STILL TO DO BY HAND, once (PicFrame3 settings, web page on port 9000),
unless already set from an earlier run of this installer:
  - MQTT:  use_mqtt = on, server = 127.0.0.1, port = 1883,
           login/password empty, device_id = picframe
  - Clock: show the clock, and set viewer.clock_extra_file to
           $HOME/clock_extra.txt
  - Restart PicFrame3 after saving:
           sudo systemctl restart picframe3@$USER

The Motion / Video proxy / Weather overlay settings themselves now live on
PicFrame3's own web Settings page (sections "Motion", "Video proxy",
"Weather overlay") -- no need to touch this installer's answers again for
those; the companion services re-read config.yaml on their own.

OUTDOOR PICO W (optional):
  Copy pico_outdoor/main.py to the Pico as main.py, and set in it your Wi-Fi
  name/password and this Pi's IP address:  $(hostname -I | awk '{print $1}')
  Give the Pi a fixed IP (DHCP reservation in your router) so it never changes.
  Test from the Pi:  mosquitto_sub -h localhost -t "home/outdoor/#" -v

Checks:
  systemctl status dht-mqtt motion-display video-proxy
  mosquitto_sub -h localhost -t "home/#" -v
  journalctl -u dht-mqtt -u motion-display -u video-proxy -f

Wiring reminder:
  DHT11 data  -> physical pin 7  (GPIO 4), VCC 3.3V, GND
  PIR OUT     -> physical pin 11 (GPIO $PIR_PIN), VCC 5V, GND
  (PIR time dial at minimum, jumper on H)

If GPIO gives a permission error right after the first run, reboot once.

Re-run this installer any time to update: bash packaging/install_companions.sh
============================================================
NOTES
