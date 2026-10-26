#!/usr/bin/env bash
#
# Fetches the YuNet face-detection model media/face.py looks for, but only if
# OpenCV is actually installed in the frame's venv -- a plain install that
# never opted into the `faces` extra has nothing to run this model WITH, so
# it is not asked to pay for the download either.
#
#   bash packaging/fetch_face_model.sh [venv-bin-dir]
#
# Called by install.sh after the venv is built, with the venv's own bin/
# directory; run standalone it defaults to the normal install location.
#
# Safe to re-run, and never fails the caller: a file already there with the
# right checksum is left alone, a frame that is offline right now keeps
# whatever detector it already had (the bundled Haar cascade needs no
# download at all -- see media/face.py), and either way install.sh's own
# steps carry on.
set -uo pipefail   # not -e: every failure here is handled explicitly, on purpose

VENV_BIN="${1:-$HOME/.local/share/picframe3/venv/bin}"
MODEL_DIR="$HOME/.local/share/picframe3/models"
MODEL_FILE="$MODEL_DIR/face_detection_yunet_2023mar_int8.onnx"
MODEL_SHA256="321aa5a6afabf7ecc46a3d06bfab2b579dc96eb5c3be7edd365fa04502ad9294"

# Two independent sources for the same file (both resolve the same Git-LFS
# object from opencv/opencv_zoo), tried in order -- so one of them being slow
# or briefly unreachable does not mean no model at all. Plain HTTPS GETs:
# no git-lfs client needed on the Pi.
MODEL_URLS=(
  "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar_int8.onnx"
  "https://huggingface.co/opencv/opencv_zoo/resolve/main/models/face_detection_yunet/face_detection_yunet_2023mar_int8.onnx"
)

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
  fi
}

if [ ! -x "$VENV_BIN/python" ] || ! "$VENV_BIN/python" -c "import cv2" >/dev/null 2>&1; then
  exit 0
fi

if [ -f "$MODEL_FILE" ] && [ "$(sha256_of "$MODEL_FILE")" = "$MODEL_SHA256" ]; then
  echo "   face detection model already present (YuNet)"
  exit 0
fi

mkdir -p "$MODEL_DIR"
TMP="$(mktemp "$MODEL_DIR/.download.XXXXXX" 2>/dev/null)" || exit 0
for url in "${MODEL_URLS[@]}"; do
  if curl -fsSL --connect-timeout 10 --max-time 60 -o "$TMP" "$url" 2>/dev/null \
     && [ "$(sha256_of "$TMP")" = "$MODEL_SHA256" ]; then
    mv "$TMP" "$MODEL_FILE"
    echo "   face detection model downloaded (YuNet -- catches far more faces than"
    echo "   the older bundled detector: angled, small, and partly-turned ones too)"
    exit 0
  fi
done
rm -f "$TMP"
echo "   could not fetch the face detection model (offline right now, or both"
echo "   sources unreachable) -- face-aware cropping still works with OpenCV's"
echo "   older bundled detector; re-run this installer later to try again"
exit 0
